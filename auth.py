#auth.py
import bcrypt
import re
from typing import Optional, Tuple
import database as db

def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash"""
    return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))


def validate_username(username: str) -> Tuple[bool, str]:
    """
    Validate username
    
    Returns:
        (is_valid, error_message)
    """
    if len(username) < 3:
        return False, "Username must be at least 3 characters long"
    
    if len(username) > 30:
        return False, "Username must be at most 30 characters long"
    
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        return False, "Username can only contain letters, numbers, and underscores"
    
    # Check if username exists
    if db.get_user_by_username(username):
        return False, "Username already taken"
    
    return True, ""


def validate_email(email: str) -> Tuple[bool, str]:
    """
    Validate email address
    
    Returns:
        (is_valid, error_message)
    """
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    
    if not re.match(email_regex, email):
        return False, "Invalid email format"
    
    # Check if email exists
    if db.get_user_by_email(email):
        return False, "Email already registered"
    
    return True, ""


def validate_password(password: str) -> Tuple[bool, str]:
    """
    Validate password strength
    
    Returns:
        (is_valid, error_message)
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    
    if len(password) > 128:
        return False, "Password must be at most 128 characters long"
    
    # Check for at least one uppercase, one lowercase, one digit
    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"
    
    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"
    
    if not re.search(r'[0-9]', password):
        return False, "Password must contain at least one digit"
    
    return True, ""


def register_user(username: str, email: str, password: str) -> Tuple[bool, str, Optional[int]]:
    """
    Register a new user
    
    Returns:
        (success, message, user_id)
    """
    # Validate username
    valid, msg = validate_username(username)
    if not valid:
        return False, msg, None
    
    # Validate email
    valid, msg = validate_email(email)
    if not valid:
        return False, msg, None
    
    # Validate password
    valid, msg = validate_password(password)
    if not valid:
        return False, msg, None
    
    # Hash password
    password_hash = hash_password(password)
    
    # Create user
    user_id = db.create_user(username, email, password_hash)
    
    if user_id:
        return True, "User registered successfully!", user_id
    else:
        return False, "Failed to create user", None


def login_user(username: str, password: str) -> Tuple[bool, str, Optional[int]]:
    """
    Login a user
    
    Returns:
        (success, message, user_id)
    """
    # Get user by username
    user = db.get_user_by_username(username)
    
    if not user:
        return False, "Invalid username or password", None
    
    # Verify password
    if not verify_password(password, user['password_hash']):
        return False, "Invalid username or password", None
    
    # Check if Gmail and Drive are connected
    if not user['is_gmail_connected'] or not user['is_drive_connected']:
        return False, "Please complete Gmail & Drive setup first", None
    
    # Update last login
    db.update_last_login(user['id'])
    
    return True, "Login successful!", user['id']


def create_user_session(user_id: int) -> str:
    """
    Create a session for logged-in user
    
    Returns:
        session_token
    """
    return db.create_session(user_id, session_duration_hours=24)


def validate_user_session(session_token: str) -> Optional[int]:
    """
    Validate session token
    
    Returns:
        user_id if valid, None otherwise
    """
    return db.validate_session(session_token)


def logout_user(session_token: str):
    """Logout user by deleting session"""
    db.delete_session(session_token)


def get_user_info(user_id: int) -> Optional[dict]:
    """Get user information"""
    return db.get_user_by_id(user_id)


if __name__ == "__main__":
    # Test authentication
    print("Testing authentication...")
    
    # Initialize database
    db.initialize_database()
    
    # Test registration
    success, msg, user_id = register_user("testuser", "test@example.com", "TestPass123")
    print(f"Registration: {msg} (User ID: {user_id})")
    
    # Test login (should fail - no Gmail connected)
    success, msg, user_id = login_user("testuser", "TestPass123")
    print(f"Login: {msg}")
    
    # Simulate Gmail connection
    if user_id:
        db.update_gmail_connection_status(user_id, True)
        db.update_drive_connection_status(user_id, True)
        print("Gmail & Drive connected!")
    
    # Test login again
    success, msg, user_id = login_user("testuser", "TestPass123")
    print(f"Login: {msg}")
    
    if success:
        # Create session
        session_token = create_user_session(user_id)
        print(f"Session created: {session_token[:20]}...")
        
        # Validate session
        validated_user_id = validate_user_session(session_token)
        print(f"Session valid for user: {validated_user_id}")
        
        # Logout
        logout_user(session_token)
        print("Logged out!")
        
        # Validate again (should fail)
        validated_user_id = validate_user_session(session_token)
        print(f"Session after logout: {validated_user_id}")