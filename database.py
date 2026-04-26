#database.py
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from contextlib import contextmanager
import secrets

# Use /data/ directory for persistence on Render if it exists
if os.path.exists("/data"):
    DATABASE_PATH = "/data/email_assistant.db"
else:
    DATABASE_PATH = "email_assistant.db"

@contextmanager
def get_db_connection():
    """Context manager for database connections"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row  # Return rows as dictionaries
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def initialize_database():
    """Create all required tables"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_gmail_connected INTEGER DEFAULT 0,
                is_drive_connected INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        """)
        
        # User tokens (encrypted Gmail OAuth tokens)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                encrypted_token TEXT NOT NULL,
                token_created_at TIMESTAMP,
                token_updated_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # User sessions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_token TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # Chat history per user
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # Downloaded files per user
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # Search cache per user
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                cache_key TEXT NOT NULL,
                cache_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # Create indexes for performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON user_sessions(session_token)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user ON user_downloads(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cache_user ON search_cache(user_id)")
        
    print("✅ Database initialized successfully")


# ==================== USER OPERATIONS ====================

def create_user(username: str, email: str, password_hash: str) -> Optional[int]:
    """Create a new user"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                (username, email, password_hash)
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None  # User already exists


def get_user_by_username(username: str) -> Optional[Dict]:
    """Get user by username"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[Dict]:
    """Get user by email"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict]:
    """Get user by ID"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_last_login(user_id: int):
    """Update user's last login timestamp"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.now(), user_id)
        )


def update_gmail_connection_status(user_id: int, is_connected: bool):
    """Update Gmail connection status"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_gmail_connected = ? WHERE id = ?",
            (1 if is_connected else 0, user_id)
        )


def update_drive_connection_status(user_id: int, is_connected: bool):
    """Update Drive connection status"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_drive_connected = ? WHERE id = ?",
            (1 if is_connected else 0, user_id)
        )


# ==================== TOKEN OPERATIONS ====================

def save_user_token(user_id: int, encrypted_token: str):
    """Save or update user's encrypted Gmail token"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM user_tokens WHERE user_id = ?", (user_id,))
        exists = cursor.fetchone()
        
        if exists:
            cursor.execute("""
                UPDATE user_tokens 
                SET encrypted_token = ?, token_updated_at = ?
                WHERE user_id = ?
            """, (encrypted_token, datetime.now(), user_id))
        else:
            cursor.execute("""
                INSERT INTO user_tokens (user_id, encrypted_token, token_created_at, token_updated_at)
                VALUES (?, ?, ?, ?)
            """, (user_id, encrypted_token, datetime.now(), datetime.now()))


def get_user_token(user_id: int) -> Optional[str]:
    """Get user's encrypted Gmail token"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT encrypted_token FROM user_tokens WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row['encrypted_token'] if row else None


def delete_user_token(user_id: int):
    """Delete user's Gmail token"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_tokens WHERE user_id = ?", (user_id,))


# ==================== SESSION OPERATIONS ====================

def create_session(user_id: int, session_duration_hours: int = 24) -> str:
    """Create a new session for user"""
    session_token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=session_duration_hours)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_sessions (user_id, session_token, expires_at)
            VALUES (?, ?, ?)
        """, (user_id, session_token, expires_at))
    
    return session_token


def validate_session(session_token: str) -> Optional[int]:
    """Validate session and return user_id if valid"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, expires_at FROM user_sessions 
            WHERE session_token = ?
        """, (session_token,))
        row = cursor.fetchone()
        
        if not row:
            return None
        
        expires_at = datetime.fromisoformat(row['expires_at'])
        if datetime.now() > expires_at:
            # Session expired, delete it
            delete_session(session_token)
            return None
        
        return row['user_id']


def delete_session(session_token: str):
    """Delete a session (logout)"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_sessions WHERE session_token = ?", (session_token,))


def delete_user_sessions(user_id: int):
    """Delete all sessions for a user"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))


def cleanup_expired_sessions():
    """Remove all expired sessions"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_sessions WHERE expires_at < ?", (datetime.now(),))
        deleted = cursor.rowcount
        if deleted > 0:
            print(f"🧹 Cleaned up {deleted} expired sessions")


# ==================== CHAT HISTORY OPERATIONS ====================

def save_chat_message(user_id: int, role: str, content: str):
    """Save a chat message"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO chat_history (user_id, role, content)
            VALUES (?, ?, ?)
        """, (user_id, role, content))


def get_chat_history(user_id: int, limit: int = 100) -> List[Dict]:
    """Get user's chat history"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT role, content, timestamp 
            FROM chat_history 
            WHERE user_id = ?
            ORDER BY timestamp ASC
            LIMIT ?
        """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def clear_chat_history(user_id: int):
    """Clear user's chat history"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))


# ==================== FILE DOWNLOAD OPERATIONS ====================

def save_download_record(user_id: int, filename: str, file_path: str, file_size: int):
    """Record a file download"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_downloads (user_id, filename, file_path, file_size)
            VALUES (?, ?, ?, ?)
        """, (user_id, filename, file_path, file_size))


def get_user_downloads(user_id: int) -> List[Dict]:
    """Get user's download history"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT filename, file_path, file_size, downloaded_at
            FROM user_downloads
            WHERE user_id = ?
            ORDER BY downloaded_at DESC
        """, (user_id,))
        return [dict(row) for row in cursor.fetchall()]


def delete_download_record(user_id: int, file_path: str):
    """Delete a download record"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM user_downloads 
            WHERE user_id = ? AND file_path = ?
        """, (user_id, file_path))


# ==================== SEARCH CACHE OPERATIONS ====================

def save_search_cache(user_id: int, cache_key: str, cache_data: str):
    """Save search results cache"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Delete old cache for this key
        cursor.execute("DELETE FROM search_cache WHERE user_id = ? AND cache_key = ?", 
                      (user_id, cache_key))
        # Insert new cache
        cursor.execute("""
            INSERT INTO search_cache (user_id, cache_key, cache_data)
            VALUES (?, ?, ?)
        """, (user_id, cache_key, cache_data))


def get_search_cache(user_id: int, cache_key: str) -> Optional[str]:
    """Get cached search results"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cache_data FROM search_cache
            WHERE user_id = ? AND cache_key = ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (user_id, cache_key))
        row = cursor.fetchone()
        return row['cache_data'] if row else None


def clear_search_cache(user_id: int):
    """Clear user's search cache"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM search_cache WHERE user_id = ?", (user_id,))


# ==================== ADMIN/UTILITY OPERATIONS ====================

def get_user_stats(user_id: int) -> Dict:
    """Get statistics for a user"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Message count
        cursor.execute("SELECT COUNT(*) as count FROM chat_history WHERE user_id = ?", (user_id,))
        message_count = cursor.fetchone()['count']
        
        # Download count
        cursor.execute("SELECT COUNT(*) as count FROM user_downloads WHERE user_id = ?", (user_id,))
        download_count = cursor.fetchone()['count']
        
        # Active sessions
        cursor.execute("""
            SELECT COUNT(*) as count FROM user_sessions 
            WHERE user_id = ? AND expires_at > ?
        """, (user_id, datetime.now()))
        active_sessions = cursor.fetchone()['count']
        
        return {
            'message_count': message_count,
            'download_count': download_count,
            'active_sessions': active_sessions
        }


if __name__ == "__main__":
    # Initialize database when run directly
    initialize_database()
    print("Database setup complete!")