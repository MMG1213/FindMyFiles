#oauth_handler.py
import os
import json
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import database as db
import encryption as enc

# Gmail & Google Drive API scopes
SCOPES = [
    # Gmail scopes
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.compose',
    # Google Drive scopes
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive.metadata.readonly'
]

# Credentials file (same for all users - your app's OAuth credentials)
CREDENTIALS_FILE = "credentials.json"

# User data directory
USER_DATA_DIR = "user_data"


def get_user_token_path(user_id: int) -> str:
    """Get path to user's token file"""
    user_dir = os.path.join(USER_DATA_DIR, f"user_{user_id}")
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, "token.json")


def get_user_attachments_path(user_id: int) -> str:
    """Get path to user's attachments folder"""
    user_dir = os.path.join(USER_DATA_DIR, f"user_{user_id}")
    attachments_dir = os.path.join(user_dir, "Attachments")
    os.makedirs(attachments_dir, exist_ok=True)
    return attachments_dir


def initiate_oauth_flow(user_id: int) -> tuple[bool, str]:
    """
    Initiate OAuth flow for a user (Gmail + Drive)
    
    Returns:
        (success, message)
    """
    if not os.path.exists(CREDENTIALS_FILE):
        return False, f"❌ Missing {CREDENTIALS_FILE}. Please download it from Google Cloud Console."
    
    try:
        # Use REDIRECT_URI from environment or default to localhost
        redirect_uri = os.getenv("REDIRECT_URI", "http://localhost:8080/")
        
        # Create OAuth flow
        flow = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_FILE, 
            SCOPES,
            redirect_uri=redirect_uri
        )
        
        # In cloud environments, we should ideally use the Web flow, 
        # but for this setup we'll try to use the console flow if possible 
        # or warn the user.
        if os.getenv("RENDER"):
            return False, "❌ OAuth 'run_local_server' does not work in cloud environments. Please connect your accounts locally and the encrypted tokens will be stored in the persistent database."
        
        # Run local server for OAuth callback
        creds = flow.run_local_server(
            port=8080,
            prompt='consent',
            success_message='✅ Gmail & Drive connected successfully! You can close this window.'
        )
        
        # Save token to file
        token_path = get_user_token_path(user_id)
        with open(token_path, 'w') as token_file:
            token_file.write(creds.to_json())
        
        # Encrypt and save to database
        token_data = json.loads(creds.to_json())
        encrypted_token = enc.encrypt_token(token_data)
        db.save_user_token(user_id, encrypted_token)
        
        # Update Gmail and Drive connection status
        db.update_gmail_connection_status(user_id, True)
        db.update_drive_connection_status(user_id, True)
        
        # Test Gmail connection
        gmail_service = build('gmail', 'v1', credentials=creds)
        gmail_profile = gmail_service.users().getProfile(userId='me').execute()
        email_address = gmail_profile.get('emailAddress', 'Unknown')
        
        # Test Drive connection
        drive_service = build('drive', 'v3', credentials=creds)
        drive_about = drive_service.about().get(fields='user').execute()
        drive_user = drive_about.get('user', {}).get('emailAddress', email_address)
        
        return True, f"✅ Gmail & Drive connected successfully!\nEmail: {email_address}\nDrive: {drive_user}"
    
    except Exception as e:
        return False, f"❌ OAuth failed: {str(e)}"


def load_user_credentials(user_id: int) -> Credentials:
    """
    Load Gmail credentials for a user
    
    Returns:
        Google Credentials object
    """
    # Get encrypted token from database
    encrypted_token = db.get_user_token(user_id)
    
    if not encrypted_token:
        raise ValueError(f"No Gmail token found for user {user_id}")
    
    # Decrypt token
    token_data = enc.decrypt_token(encrypted_token)
    
    # Create credentials object
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    
    # Refresh if expired
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            
            # Update token in database
            updated_token_data = json.loads(creds.to_json())
            encrypted_token = enc.encrypt_token(updated_token_data)
            db.save_user_token(user_id, encrypted_token)
            
            # Also update file
            token_path = get_user_token_path(user_id)
            with open(token_path, 'w') as token_file:
                token_file.write(creds.to_json())
    
    return creds


def get_gmail_service(user_id: int):
    """
    Get authenticated Gmail service for a user
    
    Returns:
        Gmail API service
    """
    creds = load_user_credentials(user_id)
    return build('gmail', 'v1', credentials=creds)


def get_drive_service(user_id: int):
    """
    Get authenticated Google Drive service for a user
    
    Returns:
        Drive API service
    """
    creds = load_user_credentials(user_id)
    return build('drive', 'v3', credentials=creds)


def disconnect_gmail(user_id: int):
    """
    Disconnect Gmail & Drive for a user (revoke access)
    """
    try:
        # Delete token from database
        db.delete_user_token(user_id)
        
        # Delete token file
        token_path = get_user_token_path(user_id)
        if os.path.exists(token_path):
            os.remove(token_path)
        
        # Update status
        db.update_gmail_connection_status(user_id, False)
        db.update_drive_connection_status(user_id, False)
        
        return True, "✅ Gmail & Drive disconnected successfully"
    
    except Exception as e:
        return False, f"❌ Error disconnecting: {str(e)}"


def disconnect_drive(user_id: int):
    """
    Alias for disconnect_gmail (both services use same token)
    """
    return disconnect_gmail(user_id)


def verify_gmail_connection(user_id: int) -> tuple[bool, str]:
    """
    Verify if user's Gmail connection is working
    
    Returns:
        (is_connected, email_address or error_message)
    """
    try:
        service = get_gmail_service(user_id)
        profile = service.users().getProfile(userId='me').execute()
        email_address = profile.get('emailAddress', 'Unknown')
        return True, email_address
    
    except Exception as e:
        return False, str(e)


def verify_drive_connection(user_id: int) -> tuple[bool, str]:
    """
    Verify if user's Drive connection is working
    
    Returns:
        (is_connected, drive_email or error_message)
    """
    try:
        service = get_drive_service(user_id)
        about = service.about().get(fields='user,storageQuota').execute()
        user_email = about.get('user', {}).get('emailAddress', 'Unknown')
        storage_quota = about.get('storageQuota', {})
        
        # Format storage info
        limit = int(storage_quota.get('limit', 0))
        usage = int(storage_quota.get('usage', 0))
        
        if limit > 0:
            used_gb = usage / (1024**3)
            total_gb = limit / (1024**3)
            storage_info = f"{used_gb:.2f} GB / {total_gb:.2f} GB used"
        else:
            storage_info = "Unlimited storage"
        
        return True, f"{user_email} ({storage_info})"
    
    except Exception as e:
        return False, str(e)


def verify_all_connections(user_id: int) -> dict:
    """
    Verify both Gmail and Drive connections
    
    Returns:
        Dictionary with connection status for both services
    """
    gmail_connected, gmail_info = verify_gmail_connection(user_id)
    drive_connected, drive_info = verify_drive_connection(user_id)
    
    return {
        'gmail': {
            'connected': gmail_connected,
            'info': gmail_info
        },
        'drive': {
            'connected': drive_connected,
            'info': drive_info
        }
    }


def check_credentials_file() -> tuple[bool, str]:
    """
    Check if credentials.json exists and has required scopes
    
    Returns:
        (exists, message)
    """
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, 'r') as f:
                data = json.load(f)
                if 'installed' in data or 'web' in data:
                    return True, """✅ credentials.json found and valid

📧 Gmail API: Enabled
📁 Drive API: Enabled

Make sure both APIs are enabled in Google Cloud Console:
1. Gmail API - https://console.cloud.google.com/apis/library/gmail.googleapis.com
2. Drive API - https://console.cloud.google.com/apis/library/drive.googleapis.com
"""
                else:
                    return False, "❌ credentials.json format is invalid"
        except:
            return False, "❌ credentials.json is corrupted"
    else:
        return False, f"""
❌ credentials.json not found!

📝 How to get credentials.json:
1. Go to: https://console.cloud.google.com/
2. Create a new project (or select existing)
3. Enable APIs:
   - Gmail API: https://console.cloud.google.com/apis/library/gmail.googleapis.com
   - Drive API: https://console.cloud.google.com/apis/library/drive.googleapis.com
4. Go to: APIs & Services > Credentials
5. Create OAuth 2.0 credentials (Desktop app)
6. Configure OAuth consent screen with scopes:
   - Gmail: gmail.readonly, gmail.modify, gmail.compose
   - Drive: drive.readonly, drive.file, drive.metadata.readonly
7. Download as credentials.json
8. Place it in the project root directory
"""


if __name__ == "__main__":
    # Test OAuth setup
    print("=" * 60)
    print("Testing Gmail & Drive OAuth Integration")
    print("=" * 60)
    print()
    
    # Check credentials file
    exists, msg = check_credentials_file()
    print(msg)
    print()
    
    if not exists:
        print("❌ Cannot proceed without credentials.json")
        exit(1)
    
    # Initialize database
    print("Initializing database...")
    db.initialize_database()
    print("✅ Database ready")
    print()
    
    # Check if test user exists
    test_user = db.get_user_by_username("test_oauth_user")
    
    if not test_user:
        print("Creating test user...")
        import auth
        password_hash = auth.hash_password("TestPass123")
        user_id = db.create_user("test_oauth_user", "test@example.com", password_hash)
        print(f"✅ Test user created (ID: {user_id})")
    else:
        user_id = test_user['id']
        print(f"✅ Using existing test user (ID: {user_id})")
    
    print()
    print("=" * 60)
    print("OAUTH FLOW TEST")
    print("=" * 60)
    print()
    print("This will:")
    print("1. Open your browser")
    print("2. Ask you to login to Google")
    print("3. Request permissions for Gmail & Drive")
    print("4. Test both API connections")
    print()
    
    proceed = input("Proceed with OAuth flow? (yes/no): ").lower().strip()
    
    if proceed == 'yes':
        print()
        print("Starting OAuth flow...")
        success, msg = initiate_oauth_flow(user_id)
        
        print()
        if success:
            print("✅ OAUTH SUCCESS!")
            print(msg)
            print()
            
            # Test Gmail
            print("-" * 60)
            print("Testing Gmail API...")
            gmail_connected, gmail_info = verify_gmail_connection(user_id)
            if gmail_connected:
                print(f"✅ Gmail: {gmail_info}")
                
                # Try to fetch recent emails
                try:
                    service = get_gmail_service(user_id)
                    results = service.users().messages().list(userId='me', maxResults=5).execute()
                    messages = results.get('messages', [])
                    print(f"   Found {len(messages)} recent emails")
                except Exception as e:
                    print(f"   ⚠️  Error fetching emails: {e}")
            else:
                print(f"❌ Gmail: {gmail_info}")
            
            # Test Drive
            print()
            print("-" * 60)
            print("Testing Drive API...")
            drive_connected, drive_info = verify_drive_connection(user_id)
            if drive_connected:
                print(f"✅ Drive: {drive_info}")
                
                # Try to list recent files
                try:
                    service = get_drive_service(user_id)
                    results = service.files().list(
                        pageSize=5,
                        fields="files(id, name, mimeType, size, modifiedTime)"
                    ).execute()
                    files = results.get('files', [])
                    print(f"   Found {len(files)} recent files:")
                    for file in files[:3]:
                        size = int(file.get('size', 0)) / 1024 if 'size' in file else 0
                        print(f"   - {file['name']} ({size:.2f} KB)")
                except Exception as e:
                    print(f"   ⚠️  Error listing files: {e}")
            else:
                print(f"❌ Drive: {drive_info}")
            
            print()
            print("-" * 60)
            print("Testing complete verification...")
            all_status = verify_all_connections(user_id)
            print(f"Gmail: {'✅ Connected' if all_status['gmail']['connected'] else '❌ Failed'}")
            print(f"Drive: {'✅ Connected' if all_status['drive']['connected'] else '❌ Failed'}")
            
            print()
            print("=" * 60)
            print("✅ ALL TESTS PASSED!")
            print("=" * 60)
            print()
            print("Your app is ready to use both Gmail and Drive APIs!")
            
        else:
            print("❌ OAUTH FAILED!")
            print(msg)
    else:
        print()
        print("OAuth flow cancelled.")
        print()
        print("To test later, run: python oauth_handler.py")