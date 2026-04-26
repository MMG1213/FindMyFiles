# main_server.py
import os
import base64
import re
import json
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
from mcp.server.fastmcp import FastMCP
import string
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import database as db
import oauth_handler as oauth

# Create FastMCP server
mcp = FastMCP("Email_Drive_Agent")
mcp.settings.port = 9006

# Per-user service cache
gmail_services = {}
drive_services = {}
email_cache = {}
attachment_cache = {}

# Local search directories
LOCAL_SEARCH_DIRS = [
    str(Path.home() / "Documents"),
    str(Path.home() / "Downloads"),
    str(Path.home() / "Desktop")
]


def coerce_types(user_id=None, max_results=None, unread_only=None, file_number=None):
    """Helper function to coerce string parameters to correct types"""
    result = {}

    if user_id is not None:
        try:
            result['user_id'] = int(user_id)
            if result['user_id'] <= 0:
                result['user_id'] = None
        except (ValueError, TypeError):
            result['user_id'] = None

    if max_results is not None:
        try:
            result['max_results'] = int(max_results)
            if result['max_results'] <= 0:
                result['max_results'] = 10
        except (ValueError, TypeError):
            result['max_results'] = 10

    if unread_only is not None:
        if isinstance(unread_only, str):
            result['unread_only'] = unread_only.lower() in ('true', '1', 'yes')
        else:
            result['unread_only'] = bool(unread_only)

    if file_number is not None:
        try:
            result['file_number'] = int(file_number)
        except (ValueError, TypeError):
            result['file_number'] = None

    return result


def get_gmail_service(user_id):
    """Get or create Gmail service for user"""
    coerced = coerce_types(user_id=user_id)
    user_id = coerced.get('user_id')
    if user_id not in gmail_services:
        gmail_services[user_id] = oauth.get_gmail_service(user_id)
    return gmail_services[user_id]


def get_drive_service(user_id):
    """Get or create Drive service for user"""
    coerced = coerce_types(user_id=user_id)
    user_id = coerced.get('user_id')
    if user_id not in drive_services:
        drive_services[user_id] = oauth.get_drive_service(user_id)
    return drive_services[user_id]


def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def extract_body(payload):
    """Recursively extract email body from payload"""
    if 'parts' in payload:
        for part in payload['parts']:
            body = extract_body(part)
            if body:
                return body
    else:
        if 'data' in payload.get('body', {}):
            try:
                return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
            except Exception:
                return "[Unable to decode body]"
    return ""


def get_date_query(time_filter: str) -> str:
    """Generate Gmail query for date filtering"""
    today = datetime.now().date()

    if time_filter.lower() in ["today", "recent"]:
        return f"after:{today.isoformat()}"
    elif time_filter.lower() == "yesterday":
        yesterday = today - timedelta(days=1)
        return f"after:{yesterday.isoformat()} before:{today.isoformat()}"
    elif time_filter.lower() == "this_week":
        week_start = today - timedelta(days=today.weekday())
        return f"after:{week_start.isoformat()}"
    elif time_filter.lower() == "last_7_days":
        week_ago = today - timedelta(days=7)
        return f"after:{week_ago.isoformat()}"

    return ""


def extract_attachments_detailed(payload, email_id: str, user_id) -> List[Dict]:
    """Extract detailed attachment info with IDs cached"""
    attachments = []
    coerced = coerce_types(user_id=user_id)
    user_id = coerced.get('user_id')

    if user_id not in attachment_cache:
        attachment_cache[user_id] = {}

    def process_parts(parts, email_id):
        for part in parts:
            if part.get('filename'):
                att_info = {
                    'filename': part['filename'],
                    'mimeType': part.get('mimeType', 'unknown'),
                    'size': part.get('body', {}).get('size', 0),
                    'attachmentId': part.get('body', {}).get('attachmentId', ''),
                    'emailId': email_id
                }
                attachments.append(att_info)
                cache_key = f"{email_id}:{part['filename']}"
                attachment_cache[user_id][cache_key] = att_info

            if 'parts' in part:
                process_parts(part['parts'], email_id)

    if 'parts' in payload:
        process_parts(payload['parts'], email_id)

    return attachments


# Directories to skip entirely during local search (avoid scanning huge dependency trees)
_SKIP_DIRS = {
    'venv', '.venv', 'node_modules', '__pycache__', '.git', '.tox',
    'site-packages', 'dist-packages', 'dist', 'build', '.mypy_cache',
    '.pytest_cache', 'env', 'Lib', 'lib',
}


def _tokenize_filename(name: str) -> set:
    """Split a filename into lowercase word tokens, removing extension and punctuation."""
    stem = os.path.splitext(name)[0]  # strip extension
    # Replace common separators with space
    stem = re.sub(r'[_\-\.\s]+', ' ', stem)
    tokens = set(stem.lower().split())
    return tokens


def _extract_keywords(query: str) -> list:
    """Extract meaningful keywords from a query string, filtering out stopwords."""
    stop_words = {
        'the', 'of', 'a', 'an', 'and', 'or', 'in', 'on', 'at', 'to',
        'for', 'with', 'by', 'from', 'file', 'show', 'me', 'get', 'find',
        'my', 'search', 'can', 'you', 'u', 'please', 'is', 'where', 'are', 'what', 'files', 'document', 'documents'
    }
    keywords = [w.lower() for w in query.split() if w.lower() not in stop_words]
    return keywords if keywords else [query.lower()]


def _keyword_matches_file(keywords: list, filename: str) -> int:
    """
    Return a match score > 0 only when ALL keywords appear as substrings 
    in the filename. Returns a higher score for exact token matches.
    Returns 0 if any keyword is missing.
    """
    tokens = _tokenize_filename(filename)
    raw = filename.lower()
    score = 0
    for kw in keywords:
        if kw not in raw:
            return 0  # ALL keywords must match as substrings
        # Boost score if it's an exact word token
        if kw in tokens:
            score += 2
        else:
            score += 1
    return score


def search_local_files(query: str, max_results=10) -> List[Dict]:
    """Search local files based on the query using whole-word token matching."""
    coerced = coerce_types(max_results=max_results)
    max_results = coerced.get('max_results', 10)

    results = []
    keywords = _extract_keywords(query)

    for directory in LOCAL_SEARCH_DIRS:
        if not os.path.exists(directory):
            continue

        try:
            for root, dirs, files in os.walk(directory):
                # Skip hidden and blacklisted dirs in-place
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith('.')
                    and d not in _SKIP_DIRS
                ]

                for file in files:
                    score = _keyword_matches_file(keywords, file)
                    if score > 0:
                        file_path = os.path.join(root, file)
                        try:
                            stat = os.stat(file_path)
                            results.append({
                                'name': file,
                                'path': file_path,
                                'size': stat.st_size,
                                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                                'type': 'local_file',
                                'match_score': score
                            })
                        except (PermissionError, OSError):
                            continue

                        if len(results) >= max_results * 5:
                            break

                if len(results) >= max_results * 5:
                    break

        except (PermissionError, OSError):
            continue

    results.sort(key=lambda x: x['match_score'], reverse=True)
    return results[:max_results]


def search_drive_files_helper(service, keywords: List[str], max_results=10):
    """Robust Google Drive search (name + content + metadata)"""
    coerced = coerce_types(max_results=max_results)
    max_results = coerced.get('max_results', 10)

    query_parts = []
    for kw in keywords:
        safe_kw = kw.replace("'", "\\'")
        query_parts.append(
            f"(name contains '{safe_kw}' or fullText contains '{safe_kw}')"
        )

    query = " or ".join(query_parts)
    query += " and trashed=false"

    print(f"🔍 Drive Query: {query}")

    try:
        results = service.files().list(
            q=query,
            pageSize=max_results,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            orderBy="modifiedTime desc"
        ).execute()

        files = results.get("files", [])
        print(f"✅ Drive found {len(files)} files")
        return files
    except Exception as e:
        print(f"❌ Drive search error: {e}")
        return []


@mcp.tool()
def fetch_emails(user_id, max_results=10, time_filter: str = "all", unread_only=False) -> str:
    """Fetch recent emails for a specific user

    Args:
        user_id: User ID (integer)
        max_results: Maximum number of emails to fetch (integer, default 10)
        time_filter: Time filter - 'all', 'today', 'yesterday', 'this_week', 'last_7_days', 'recent'
        unread_only: Only fetch unread emails (boolean, default False)
    """
    coerced = coerce_types(user_id=user_id, max_results=max_results, unread_only=unread_only)
    user_id = coerced.get('user_id')
    max_results = coerced.get('max_results', 10)
    unread_only = coerced.get('unread_only', False)

    service = get_gmail_service(user_id)

    query_parts = []
    if unread_only:
        query_parts.append("is:unread")

    date_query = get_date_query(time_filter)
    if date_query:
        query_parts.append(date_query)

    query = " ".join(query_parts)

    results = service.users().messages().list(
        userId='me', q=query, maxResults=max_results
    ).execute()

    messages = results.get('messages', [])

    if not messages:
        return f"No emails found for filter: {time_filter}"

    if user_id not in email_cache:
        email_cache[user_id] = {}

    email_list = []
    for msg in messages:
        email = service.users().messages().get(
            userId='me', id=msg['id'], format='full'
        ).execute()

        email_cache[user_id][msg['id']] = email

        headers = {h['name']: h['value'] for h in email.get('payload', {}).get('headers', [])}
        attachments = extract_attachments_detailed(email.get('payload', {}), msg['id'], user_id)

        email_list.append({
            'id': msg['id'],
            'subject': headers.get('Subject', 'No Subject'),
            'from': headers.get('From', 'Unknown'),
            'date': headers.get('Date', 'Unknown'),
            'snippet': email.get('snippet', ''),
            'attachments': attachments
        })

    output = f"📬 Found {len(email_list)} emails ({time_filter}):\n\n"
    for i, email in enumerate(email_list, 1):
        output += f"{i}. **{email['subject']}**\n"
        output += f"   From: {email['from']}\n"
        output += f"   Date: {email['date']}\n"
        output += f"   Email ID: `{email['id']}`\n"

        if email['attachments']:
            output += f"   📎 Attachments ({len(email['attachments'])}):\n"
            for att in email['attachments']:
                size_kb = att['size'] / 1024
                output += f"      - **{att['filename']}** ({size_kb:.2f} KB)\n"

        output += f"   Preview: {email['snippet'][:100]}...\n\n"

    return output


@mcp.tool()
def search_emails(user_id, query: str, max_results=10, time_filter: str = "all") -> str:
    """Search emails for a specific user

    Args:
        user_id: User ID (integer)
        query: Search query (string)
        max_results: Maximum number of results (integer, default 10)
        time_filter: Time filter (string, default 'all')
    """
    coerced = coerce_types(user_id=user_id, max_results=max_results)
    user_id = coerced.get('user_id')
    max_results = coerced.get('max_results', 10)

    service = get_gmail_service(user_id)

    date_query = get_date_query(time_filter)
    full_query = f"{query} {date_query}".strip()

    results = service.users().messages().list(
        userId='me', q=full_query, maxResults=max_results
    ).execute()

    messages = results.get('messages', [])

    if not messages:
        return f"No emails found matching: {query} ({time_filter})"

    if user_id not in email_cache:
        email_cache[user_id] = {}

    output = f"🔍 Found {len(messages)} emails for '{query}' ({time_filter}):\n\n"

    for i, msg in enumerate(messages, 1):
        email = service.users().messages().get(
            userId='me', id=msg['id'], format='full'
        ).execute()

        email_cache[user_id][msg['id']] = email

        headers = {h['name']: h['value'] for h in email.get('payload', {}).get('headers', [])}
        attachments = extract_attachments_detailed(email.get('payload', {}), msg['id'], user_id)

        output += f"{i}. **{headers.get('Subject', 'No Subject')}**\n"
        output += f"   From: {headers.get('From', 'Unknown')}\n"
        output += f"   Date: {headers.get('Date', 'Unknown')}\n"
        output += f"   Email ID: `{msg['id']}`\n"

        if attachments:
            output += f"   📎 {len(attachments)} attachment(s)\n"

        output += "\n"

    return output


@mcp.tool()
def download_attachment(user_id, email_id: str, filename: str, attachment_id: str = None) -> str:
    """Download email attachment for a specific user

    Args:
        user_id: User ID (integer)
        email_id: Email ID (string)
        filename: Attachment filename (string)
        attachment_id: Attachment ID (string, optional)
    """
    coerced = coerce_types(user_id=user_id)
    user_id = coerced.get('user_id')

    service = get_gmail_service(user_id)

    try:
        if not attachment_id:
            if user_id in attachment_cache:
                cache_key = f"{email_id}:{filename}"
                if cache_key in attachment_cache[user_id]:
                    attachment_id = attachment_cache[user_id][cache_key]['attachmentId']

        if not attachment_id:
            return f"❌ Could not find attachment '{filename}' in email `{email_id}`"

        attachment = service.users().messages().attachments().get(
            userId='me', messageId=email_id, id=attachment_id
        ).execute()

        file_data = base64.urlsafe_b64decode(attachment['data'])

        save_path = oauth.get_user_attachments_path(user_id)
        file_path = os.path.join(save_path, filename)

        with open(file_path, 'wb') as f:
            f.write(file_data)

        db.save_download_record(user_id, filename, file_path, len(file_data))

        return f"✅ Downloaded: **{filename}**\n\nSaved to: `{file_path}`\nSize: {len(file_data) / 1024:.2f} KB"

    except Exception as e:
        return f"❌ Error downloading attachment: {str(e)}"


@mcp.tool()
def smart_search_with_memory(user_id, query: str, max_results=10) -> str:
    """Smart search across local files, Gmail attachments, and Google Drive.
    Returns structured results with a JSON block for the UI to render as cards.

    Args:
        user_id: User ID (integer)
        query: Search query (string)
        max_results: Maximum results per source (integer, default 10)
    """
    coerced = coerce_types(user_id=user_id, max_results=max_results)
    user_id = coerced.get('user_id')
    max_results = coerced.get('max_results', 10)

    if user_id is None:
        return "❌ User not logged in. Please login again."

    keywords = _extract_keywords(query)

    print(f"🔍 Search keywords: {keywords}")

    all_results = []
    result_index = 1

    # ── LOCAL FILE SEARCH ──────────────────────────────────────────
    local_files = search_local_files(query, max_results)
    for file in local_files:
        all_results.append({
            'number': result_index,
            'type': 'local',
            'name': file['name'],
            'path': file['path'],
            'size': file['size'],
            'size_str': format_file_size(file['size']),
            'source_label': file['path'],
        })
        result_index += 1

    # ── GMAIL ATTACHMENT SEARCH ────────────────────────────────────
    try:
        gmail_service = get_gmail_service(user_id)
        gmail_query = f"has:attachment ({' OR '.join(keywords)})"

        results = gmail_service.users().messages().list(
            userId='me', q=gmail_query, maxResults=max_results
        ).execute()

        messages = results.get('messages', [])

        if user_id not in email_cache:
            email_cache[user_id] = {}

        for msg in messages:
            email = gmail_service.users().messages().get(
                userId='me', id=msg['id'], format='full'
            ).execute()

            email_cache[user_id][msg['id']] = email
            headers = {h['name']: h['value']
                       for h in email.get('payload', {}).get('headers', [])}
            attachments = extract_attachments_detailed(
                email.get('payload', {}), msg['id'], user_id)

            for att in attachments:
                # Use the same whole-word token matching as local search
                att_score = _keyword_matches_file(keywords, att['filename'])
                if att_score > 0:
                    all_results.append({
                        'number': result_index,
                        'type': 'email',
                        'name': att['filename'],
                        'email_id': msg['id'],
                        'attachment_id': att['attachmentId'],
                        'size': att['size'],
                        'size_str': format_file_size(att['size']),
                        'source_label': headers.get('Subject', 'No Subject'),
                        'from': headers.get('From', ''),
                    })
                    result_index += 1

    except Exception as e:
        print(f"❌ Gmail search error: {e}")

    # ── GOOGLE DRIVE SEARCH ────────────────────────────────────────
    try:
        drive_service = oauth.get_drive_service(user_id)
        drive_files = search_drive_files_helper(drive_service, keywords, max_results)

        for file in drive_files:
            size_bytes = int(file.get("size", 0)) if file.get("size") else 0
            mime_type = file.get('mimeType', '')
            all_results.append({
                'number': result_index,
                'type': 'drive',
                'name': file['name'],
                'file_id': file['id'],
                'mimeType': mime_type,
                'size': size_bytes,
                'size_str': format_file_size(size_bytes) if size_bytes else 'Unknown size',
                'source_label': 'Google Drive',
            })
            result_index += 1

    except Exception as e:
        print(f"❌ Drive search error: {e}")

    # ── CACHE ALL RESULTS ──────────────────────────────────────────
    db.save_search_cache(user_id, 'last_search', json.dumps(all_results))

    # ── BUILD TEXT SUMMARY (shown in chat before cards render) ─────
    total = len(all_results)
    local_count = sum(1 for r in all_results if r['type'] == 'local')
    email_count = sum(1 for r in all_results if r['type'] == 'email')
    drive_count = sum(1 for r in all_results if r['type'] == 'drive')

    if total == 0:
        output = f"❌ No files found for '{query}'\n"
        output += f"\n💡 Tried searching for: {', '.join(keywords)}\n"
        output += "\n🔍 Try:\n- More specific keywords\n- Check file name spelling\n- Use 'list drive files' to see all files\n"
        return output

    output = f"Found **{total} files** matching '{query}' "
    output += f"({local_count} local, {email_count} email, {drive_count} Drive).\n\n"
    output += "Use the **Preview** or **Download** buttons on each file card below.\n"

    # Append the JSON marker — the Streamlit frontend will parse this
    output += f"\n\n<!--RESULTS_JSON:{json.dumps(all_results)}-->"

    return output


@mcp.tool()
def open_search_result(user_id, file_number) -> str:
    """Open a local file from last search by number

    Args:
        user_id: User ID (integer)
        file_number: File number from search results (integer)
    """
    coerced = coerce_types(user_id=user_id, file_number=file_number)
    user_id = coerced.get('user_id')
    file_number = coerced.get('file_number')

    cache_data = db.get_search_cache(user_id, 'last_search')

    if not cache_data:
        return "❌ No recent search results. Please search for files first."

    results = json.loads(cache_data)

    target_file = next((f for f in results if f['number'] == file_number), None)

    if not target_file:
        return f"❌ File #{file_number} not found."

    if target_file['type'] != 'local':
        return (f"❌ File #{file_number} is an {target_file['type']} file. "
                f"Use 'download file {file_number}' instead.")

    try:
        file_path = target_file['path']
        if os.name == 'nt':
            os.startfile(file_path)
        elif os.name == 'posix':
            import subprocess
            subprocess.Popen(['open', file_path])

        return f"✅ Opened: **{target_file['name']}**"
    except Exception as e:
        return f"❌ Error opening file: {str(e)}"


@mcp.tool()
def open_file_location(user_id, file_number) -> str:
    """Open the directory containing a local file from last search

    Args:
        user_id: User ID (integer)
        file_number: File number from search results (integer)
    """
    coerced = coerce_types(user_id=user_id, file_number=file_number)
    user_id = coerced.get('user_id')
    file_number = coerced.get('file_number')

    cache_data = db.get_search_cache(user_id, 'last_search')

    if not cache_data:
        return "❌ No recent search results. Please search for files first."

    results = json.loads(cache_data)

    target_file = next((f for f in results if f['number'] == file_number), None)

    if not target_file:
        return f"❌ File #{file_number} not found."

    if target_file['type'] != 'local':
        return (f"❌ File #{file_number} is an {target_file['type']} file. "
                f"Cannot open local directory.")

    try:
        file_path = target_file['path']
        if os.name == 'nt':
            import subprocess
            subprocess.Popen(f'explorer /select,"{file_path}"')
        elif os.name == 'posix':
            import subprocess
            dir_path = os.path.dirname(file_path)
            subprocess.Popen(['open', dir_path])

        return f"✅ Opened location for: **{target_file['name']}**"
    except Exception as e:
        return f"❌ Error opening file location: {str(e)}"


@mcp.tool()
def download_search_result(user_id, file_number) -> str:
    """Download email attachment or Drive file from last search by number

    Args:
        user_id: User ID (integer)
        file_number: File number from search results (integer)
    """
    coerced = coerce_types(user_id=user_id, file_number=file_number)
    user_id = coerced.get('user_id')
    file_number = coerced.get('file_number')

    cache_data = db.get_search_cache(user_id, 'last_search')

    if not cache_data:
        return "❌ No recent search results. Please search for files first."

    results = json.loads(cache_data)

    target_file = next((f for f in results if f['number'] == file_number), None)

    if not target_file:
        return f"❌ File #{file_number} not found."

    if target_file['type'] == 'local':
        return f"✅ File #{file_number} is already on your computer at:\n`{target_file['path']}`"

    if target_file['type'] == 'email':
        return download_attachment(
            user_id=user_id,
            email_id=target_file['email_id'],
            filename=target_file['name'],
            attachment_id=target_file['attachment_id']
        )

    if target_file['type'] == 'drive':
        return download_drive_file(
            user_id=user_id,
            file_id=target_file['file_id'],
            filename=target_file['name']
        )

    return f"❌ Unknown file type: {target_file['type']}"


@mcp.tool()
def list_drive_files(user_id, max_results=10, query: str = None) -> str:
    """List files from user's Google Drive

    Args:
        user_id: User ID (integer)
        max_results: Maximum number of files (integer, default 10)
        query: Optional Drive query (string)
    """
    try:
        coerced = coerce_types(user_id=user_id, max_results=max_results)
        user_id = coerced.get('user_id')
        max_results = coerced.get('max_results', 10)

        service = get_drive_service(user_id)

        drive_query = query if query else "trashed=false"

        results = service.files().list(
            pageSize=max_results,
            q=drive_query,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)",
            orderBy="modifiedTime desc"
        ).execute()

        files = results.get('files', [])

        if not files:
            return "📁 No files found in Drive"

        output = f"📁 **Found {len(files)} files in Drive:**\n\n"

        for i, file in enumerate(files, 1):
            name = file.get('name', 'Unnamed')
            file_id = file.get('id', '')
            mime_type = file.get('mimeType', 'unknown')
            size = int(file.get('size', 0)) if 'size' in file else 0
            modified = file.get('modifiedTime', 'Unknown')

            if 'folder' in mime_type:
                icon = '📂'
            elif 'document' in mime_type or 'pdf' in mime_type:
                icon = '📄'
            elif 'spreadsheet' in mime_type:
                icon = '📊'
            elif 'image' in mime_type:
                icon = '🖼️'
            else:
                icon = '📎'

            output += f"{i}. {icon} **{name}**\n"
            output += f"   File ID: `{file_id}`\n"
            if size > 0:
                output += f"   Size: {format_file_size(size)}\n"
            output += f"   Modified: {modified}\n\n"

        return output

    except Exception as e:
        return f"❌ Error listing Drive files: {str(e)}"


@mcp.tool()
def search_drive_files(user_id, query: str, max_results=20) -> str:
    """Search files in user's Google Drive by name and content

    Args:
        user_id: User ID (integer)
        query: Search query (string)
        max_results: Maximum results (integer, default 20)
    """
    try:
        coerced = coerce_types(user_id=user_id, max_results=max_results)
        user_id = coerced.get('user_id')
        max_results = coerced.get('max_results', 20)

        service = get_drive_service(user_id)

        keywords = _extract_keywords(query)

        query_parts = []
        for kw in keywords:
            safe_kw = kw.replace("'", "\\'")
            query_parts.append(
                f"(name contains '{safe_kw}' or fullText contains '{safe_kw}')"
            )

        drive_query = " or ".join(query_parts)
        drive_query += " and trashed=false"

        print(f"🔍 Drive search query: {drive_query}")

        results = service.files().list(
            q=drive_query,
            pageSize=max_results,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)",
            orderBy="modifiedTime desc",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        files = results.get('files', [])

        if not files:
            return (f"🔍 No files found matching '{query}' in Drive\n\n"
                    f"Tried searching for: {', '.join(keywords)}\n\n"
                    f"💡 Try:\n- 'list drive files' to see all files\n"
                    f"- More specific keywords\n- Check spelling")

        output = f"🔍 **Found {len(files)} files matching '{query}' in Drive:**\n\n"

        for i, file in enumerate(files, 1):
            name = file.get('name', 'Unnamed')
            file_id = file.get('id', '')
            size = int(file.get('size', 0)) if 'size' in file else 0
            modified = file.get('modifiedTime', 'Unknown')
            mime_type = file.get('mimeType', '')

            if 'folder' in mime_type:
                icon = '📂'
            elif 'document' in mime_type or 'pdf' in mime_type:
                icon = '📄'
            elif 'spreadsheet' in mime_type:
                icon = '📊'
            elif 'image' in mime_type:
                icon = '🖼️'
            else:
                icon = '📎'

            output += f"{i}. {icon} **{name}**\n"
            output += f"   File ID: `{file_id}`\n"
            if size > 0:
                output += f"   Size: {format_file_size(size)}\n"
            output += f"   Modified: {modified}\n"

            matched_keywords = [kw for kw in keywords if kw in name.lower()]
            if matched_keywords:
                output += f"   🎯 Matches: {', '.join(matched_keywords)}\n"

            output += "\n"

        output += "\n💡 To download, say: **download drive file [file_id]**"

        return output

    except Exception as e:
        return f"❌ Error searching Drive files: {str(e)}"


@mcp.tool()
def download_drive_file(user_id, file_id: str, filename: str = None) -> str:
    """Download a file from user's Google Drive

    Args:
        user_id: User ID (integer)
        file_id: Drive file ID (string)
        filename: Optional filename to save as (string)
    """
    try:
        coerced = coerce_types(user_id=user_id)
        user_id = coerced.get('user_id')

        service = get_drive_service(user_id)

        file_metadata = service.files().get(
            fileId=file_id, fields='name,mimeType,size'
        ).execute()

        if not filename:
            filename = file_metadata.get('name', f'drive_file_{file_id}')

        mime_type = file_metadata.get('mimeType', '')

        export_mimetypes = {
            'application/vnd.google-apps.document': ('application/pdf', '.pdf'),
            'application/vnd.google-apps.spreadsheet': (
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx'),
            'application/vnd.google-apps.presentation': (
                'application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx')
        }

        save_path = oauth.get_user_attachments_path(user_id)

        if mime_type in export_mimetypes:
            export_mime, ext = export_mimetypes[mime_type]
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            if not filename.endswith(ext):
                filename += ext
        else:
            request = service.files().get_media(fileId=file_id)

        file_path = os.path.join(save_path, filename)

        fh = io.FileIO(file_path, 'wb')
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()

        fh.close()

        file_size = os.path.getsize(file_path)
        db.save_download_record(user_id, filename, file_path, file_size)

        return (f"✅ Downloaded: **{filename}**\n\n"
                f"Saved to: `{file_path}`\n"
                f"Size: {format_file_size(file_size)}")

    except Exception as e:
        return f"❌ Error downloading Drive file: {str(e)}"


@mcp.tool()
def get_drive_storage_info(user_id) -> str:
    """Get user's Drive storage information

    Args:
        user_id: User ID (integer)
    """
    try:
        coerced = coerce_types(user_id=user_id)
        user_id = coerced.get('user_id')

        service = get_drive_service(user_id)

        about = service.about().get(fields='storageQuota,user').execute()

        quota = about.get('storageQuota', {})
        user_info = about.get('user', {})

        limit = int(quota.get('limit', 0))
        usage = int(quota.get('usage', 0))
        usage_in_drive = int(quota.get('usageInDrive', 0))

        output = "**📊 Drive Storage Information**\n\n"
        output += f"**User:** {user_info.get('emailAddress', 'Unknown')}\n\n"

        if limit > 0:
            percentage = (usage / limit) * 100
            output += f"**Total Usage:** {format_file_size(usage)} / {format_file_size(limit)} ({percentage:.1f}%)\n"
            output += f"**In Drive:** {format_file_size(usage_in_drive)}\n"
            output += f"**Available:** {format_file_size(limit - usage)}\n"
        else:
            output += f"**Storage:** Unlimited\n"
            output += f"**Current Usage:** {format_file_size(usage)}\n"

        return output

    except Exception as e:
        return f"❌ Error getting storage info: {str(e)}"


if __name__ == "__main__":
    db.initialize_database()
    print("✅ Database initialized")
    mcp.run(transport="streamable-http")