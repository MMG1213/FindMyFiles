#encryption.py
from cryptography.fernet import Fernet
import os
import json

# Encryption key file
KEY_FILE = "encryption.key"

def generate_key():
    """Generate a new encryption key"""
    key = Fernet.generate_key()
    with open(KEY_FILE, 'wb') as f:
        f.write(key)
    print(f"✅ Encryption key generated: {KEY_FILE}")
    return key


def load_key():
    """Load the encryption key"""
    if not os.path.exists(KEY_FILE):
        print("⚠️  No encryption key found, generating new one...")
        return generate_key()
    
    with open(KEY_FILE, 'rb') as f:
        return f.read()


def get_cipher():
    """Get Fernet cipher instance"""
    key = load_key()
    return Fernet(key)


def encrypt_token(token_data: dict) -> str:
    """
    Encrypt Gmail token data
    
    Args:
        token_data: Dictionary containing OAuth2 token info
        
    Returns:
        Encrypted token as base64 string
    """
    cipher = get_cipher()
    token_json = json.dumps(token_data)
    encrypted = cipher.encrypt(token_json.encode())
    return encrypted.decode()


def decrypt_token(encrypted_token: str) -> dict:
    """
    Decrypt Gmail token data
    
    Args:
        encrypted_token: Encrypted token as base64 string
        
    Returns:
        Dictionary containing OAuth2 token info
    """
    cipher = get_cipher()
    decrypted = cipher.decrypt(encrypted_token.encode())
    return json.loads(decrypted.decode())


def encrypt_file(file_path: str) -> str:
    """
    Encrypt a file and return encrypted content as string
    
    Args:
        file_path: Path to file (e.g., token.json)
        
    Returns:
        Encrypted content as base64 string
    """
    with open(file_path, 'r') as f:
        data = json.load(f)
    
    return encrypt_token(data)


def decrypt_to_file(encrypted_token: str, output_path: str):
    """
    Decrypt token and save to file
    
    Args:
        encrypted_token: Encrypted token string
        output_path: Path to save decrypted token.json
    """
    data = decrypt_token(encrypted_token)
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)


if __name__ == "__main__":
    # Test encryption
    print("Testing encryption...")
    
    test_token = {
        "token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]
    }
    
    # Encrypt
    encrypted = encrypt_token(test_token)
    print(f"✅ Encrypted: {encrypted[:50]}...")
    
    # Decrypt
    decrypted = decrypt_token(encrypted)
    print(f"✅ Decrypted: {decrypted['token']}")
    
    print("Encryption test passed!")