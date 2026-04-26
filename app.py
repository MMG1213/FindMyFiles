import streamlit as st
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
import asyncio
import warnings
import threading
import os
import json
import re
import requests
from datetime import datetime
from pathlib import Path

import database as db
import auth
import oauth_handler as oauth

from dotenv import load_dotenv
load_dotenv()

st.set_page_config(
    page_title="FindMyFiles",
    page_icon="📁",
    layout="wide",
    initial_sidebar_state="expanded"
)

MODELS = {
    "groq": {
        "name": "Llama 4 Scout (Groq)",
        "model_id": "meta-llama/llama-4-scout-17b-16e-instruct",
        "icon": "⚡",
        "description": "Fast and efficient",
        "temperature": 0
    },
    "gemini": {
        "name": "Gemini 2.5 Flash",
        "model_id": "gemini-2.5-flash",
        "icon": "✨",
        "description": "Powerful and accurate",
        "temperature": 0
    }
}

BOT_PROMPT = """
You are a file and email search assistant. You have access to tools that search Gmail, Google Drive, and the local computer.

**ABSOLUTE RULES — NEVER BREAK THESE:**
1. You MUST use the built-in tool-calling mechanism for EVERY user request. Do not just type the function name as text. You must ACTUALLY CALL the tool via the API.
2. NEVER answer from memory or make up results.
3. ALWAYS use user_id={user_id} as an INTEGER in your tool calls.

**WHEN TO USE WHICH TOOL:**
- If the user asks for a specific file (e.g. "my pan card", "aadhaar", "invoice", "photo"):
  -> You MUST invoke the tool `smart_search_with_memory` using the tool-calling API.
- If the user asks for general emails or their inbox:
  -> You MUST invoke the tool `fetch_emails` using the tool-calling API.
- If the user asks to open or download file number N:
  -> You MUST invoke `open_search_result` or `download_search_result`.

**AFTER THE TOOL RETURNS:**
- When `smart_search_with_memory` returns results to you, DO NOT list the files yourself. 
- The UI will automatically render the files. Just say "Here are the results I found:".
- If the tool returns 0 results, tell the user no files were found.

Remember: Do not type "call smart_search_with_memory(...)". Actually invoke the tool so the system can execute it!
"""

MCP_SERVER_URL = "http://localhost:9006"


def init_session_state():
    defaults = {
        "authenticated": False,
        "user_id": None,
        "session_token": None,
        "user_info": None,
        "messages": [],
        "agent_manager": None,
        "page": "login",
        "selected_model": "groq",
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


# ─────────────────────────────────────────────────────────────
#  DIRECT MCP CALLS (bypass LLM — used by card buttons)
# ─────────────────────────────────────────────────────────────

import server

def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Call an MCP tool directly via local import without going through HTTP."""
    try:
        tool_func = getattr(server, tool_name, None)
        if tool_func and callable(tool_func):
            result = tool_func(**arguments)
            return str(result)
        else:
            return f"❌ Error: Tool {tool_name} not found locally."
    except Exception as e:
        return f"❌ Error: {str(e)}"


# ─────────────────────────────────────────────────────────────
#  GMAIL PROFILE PHOTO HELPER
# ─────────────────────────────────────────────────────────────

def get_gmail_profile_photo(user_id: int) -> str | None:
    """
    Fetch the Gmail account profile photo URL using the stored OAuth credentials.
    Returns the photo URL string, or None if unavailable.
    """
    try:
        creds = oauth.get_user_credentials(user_id)
        if not creds:
            return None
        resp = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("picture")
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────
#  FILE CARD RENDERER
# ─────────────────────────────────────────────────────────────

def get_file_icon(name: str, mime_type: str = "") -> str:
    ext = os.path.splitext(name)[1].lower()
    icon_map = {
        '.pdf': '📕', '.doc': '📘', '.docx': '📘',
        '.xls': '📗', '.xlsx': '📗',
        '.ppt': '📙', '.pptx': '📙',
        '.jpg': '🖼️', '.jpeg': '🖼️', '.png': '🖼️', '.gif': '🖼️',
        '.zip': '📦', '.rar': '📦',
        '.txt': '📄', '.csv': '📄',
        '.mp4': '🎬', '.mp3': '🎵',
    }
    if ext in icon_map:
        return icon_map[ext]
    if 'folder' in mime_type:
        return '📂'
    if 'spreadsheet' in mime_type:
        return '📗'
    if 'document' in mime_type:
        return '📘'
    if 'image' in mime_type:
        return '🖼️'
    return '📎'


def render_file_cards(results: list, user_id: int, msg_idx: int = 0):
    """Render file results as interactive cards with Preview/Open/Download buttons."""

    local_files = [r for r in results if r['type'] == 'local']
    email_files = [r for r in results if r['type'] == 'email']
    drive_files = [r for r in results if r['type'] == 'drive']

    st.markdown(
        f"<div style='font-size:13px;color:gray;margin-bottom:8px;'>"
        f"Found <strong>{len(results)}</strong> files &nbsp;·&nbsp; "
        f"📁 {len(local_files)} local &nbsp;·&nbsp; "
        f"📧 {len(email_files)} email &nbsp;·&nbsp; "
        f"☁️ {len(drive_files)} Drive"
        f"</div>",
        unsafe_allow_html=True
    )

    def card_section(title: str, files: list):
        if not files:
            return
        st.markdown(f"**{title}**")
        for f in files:
            icon = get_file_icon(f['name'], f.get('mimeType', ''))
            size_str = f.get('size_str', '')
            source = f.get('source_label', '')

            # Use a bordered container to simulate a card
            with st.container(border=True):
                col_icon, col_info, col_actions = st.columns([0.5, 5, 3])

                with col_icon:
                    st.markdown(
                        f"<div style='font-size:22px;padding-top:6px'>{icon}</div>",
                        unsafe_allow_html=True
                    )

                with col_info:
                    st.markdown(
                        f"<div style='font-size:14px;font-weight:600;margin-bottom:2px'>"
                        f"#{f['number']} &nbsp; {f['name']}</div>"
                        f"<div style='font-size:12px;color:gray'>{source}"
                        f"{' · ' + size_str if size_str else ''}</div>",
                        unsafe_allow_html=True
                    )

                with col_actions:
                    if f['type'] == 'local':
                        b1, b2 = st.columns(2)
                        with b1:
                            if st.button(
                                "👁️ Preview",
                                key=f"prev_{msg_idx}_{f['number']}",
                                use_container_width=True
                            ):
                                result = call_mcp_tool(
                                    "open_search_result",
                                    {"user_id": user_id, "file_number": f['number']}
                                )
                                st.toast(result)
                        with b2:
                            if st.button(
                                "📂 Open",
                                key=f"open_{msg_idx}_{f['number']}",
                                use_container_width=True
                            ):
                                result = call_mcp_tool(
                                    "open_file_location",
                                    {"user_id": user_id, "file_number": f['number']}
                                )
                                st.toast(result)

                    elif f['type'] == 'email':
                        if st.button(
                            "⬇️ Download",
                            key=f"dl_email_{msg_idx}_{f['number']}",
                            use_container_width=True,
                            type="primary"
                        ):
                            with st.spinner(f"Downloading {f['name']}..."):
                                result = call_mcp_tool(
                                    "download_attachment",
                                    {
                                        "user_id": user_id,
                                        "email_id": f['email_id'],
                                        "filename": f['name'],
                                        "attachment_id": f['attachment_id']
                                    }
                                )
                            st.toast(result)
                            st.rerun()

                    elif f['type'] == 'drive':
                        if st.button(
                            "⬇️ Download",
                            key=f"dl_drive_{msg_idx}_{f['number']}",
                            use_container_width=True,
                            type="primary"
                        ):
                            with st.spinner(f"Downloading {f['name']}..."):
                                result = call_mcp_tool(
                                    "download_drive_file",
                                    {
                                        "user_id": user_id,
                                        "file_id": f['file_id'],
                                        "filename": f['name']
                                    }
                                )
                            st.toast(result)
                            st.rerun()

    card_section("📁 Local Files", local_files)
    card_section("📧 Email Attachments", email_files)
    card_section("☁️ Google Drive", drive_files)


# ─────────────────────────────────────────────────────────────
#  MESSAGE DISPLAY — detects JSON block & renders cards
# ─────────────────────────────────────────────────────────────

RESULTS_PATTERN = re.compile(r'<!--RESULTS_JSON:(.*?)-->', re.DOTALL)


def render_assistant_content(content: str, user_id: int, msg_idx: int = 0):
    """
    Render assistant message content.
    If it contains a RESULTS_JSON block, show text summary + file cards.
    Otherwise show plain markdown.
    Called OUTSIDE of any st.chat_message context.
    """
    match = RESULTS_PATTERN.search(content)
    if match:
        text_part = content[:match.start()].strip()
        if text_part:
            st.markdown(text_part)
        try:
            results = json.loads(match.group(1))
            render_file_cards(results, user_id, msg_idx)
        except json.JSONDecodeError:
            st.markdown(content)
    else:
        st.markdown(content)


def display_chat_history(messages: list, user_id: int):
    """
    Replay all messages from history.
    Each message gets its own st.chat_message block.
    """
    for i, message in enumerate(messages):
        role = message["role"]
        content = message["content"]
        avatar = "👤" if role == "user" else "🤖"

        with st.chat_message(role, avatar=avatar):
            if role == "assistant":
                render_assistant_content(content, user_id, msg_idx=i)
            else:
                st.markdown(content)


# ─────────────────────────────────────────────────────────────
#  AGENT
# ─────────────────────────────────────────────────────────────

class SmartAgent:
    def __init__(self, user_id: int, model_type: str = "groq"):
        self.user_id = user_id
        self.model_type = model_type
        self.agent = None
        self.loop = None
        self._lock = threading.Lock()

    def _ensure_initialized(self):
        with self._lock:
            if self.agent is not None:
                return
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                self.agent = loop.run_until_complete(self._init_agent())
                self.loop = loop
            except Exception:
                loop.close()
                raise

    async def _init_agent(self):
        client = MultiServerMCPClient({
            "Email_Agent": {
                "url": "http://localhost:9006/mcp",
                "transport": "streamable_http",
            },
        })

        tools = await client.get_tools()

        print(f"\n{'='*60}")
        print(f"🔧 MCP Tools Loaded: {len(tools)}")
        for i, tool in enumerate(tools, 1):
            print(f"  {i}. {tool.name}")
        print(f"{'='*60}\n")

        if len(tools) == 0:
            raise RuntimeError(
                "❌ No tools loaded from MCP server! "
                "Make sure server.py is running on port 9006"
            )

        if self.model_type == "groq":
            api_key = os.getenv("GROQ_API_KEY")
            if not api_key:
                raise ValueError(
                    "GROQ_API_KEY not set in .env file.\n"
                    "Get your key from: https://console.groq.com/keys"
                )
            model = ChatGroq(
                model=MODELS["groq"]["model_id"],
                api_key=api_key,
                temperature=MODELS["groq"]["temperature"]
            )

        elif self.model_type == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise ValueError(
                    "GEMINI_API_KEY not set in .env file.\n"
                    "Get your key from: https://aistudio.google.com/apikey"
                )
            model = ChatGoogleGenerativeAI(
                model=MODELS["gemini"]["model_id"],
                api_key=api_key,
                temperature=MODELS["gemini"]["temperature"]
            )

        else:
            raise ValueError(f"Unknown model type: {self.model_type}")

        print(f"✅ Agent initialized with {MODELS[self.model_type]['name']}")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return create_react_agent(model, tools=tools)

    def chat(self, user_message: str, history: list) -> str:
        self._ensure_initialized()

        system_prompt = BOT_PROMPT.format(user_id=self.user_id)
        messages = [{"role": "system", "content": system_prompt}]

        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})

        messages.append({"role": "user", "content": user_message})

        try:
            print(f"\n{'='*60}")
            print(f"💬 User: {user_message}")
            print(f"🤖 Model: {MODELS[self.model_type]['name']}")
            print(f"{'='*60}")

            response = self.loop.run_until_complete(
                self.agent.ainvoke({"messages": messages})
            )

            # Extract the new messages generated in this turn
            new_messages = response["messages"][len(messages):]
            
            last_message = response["messages"][-1]
            content = last_message.content if hasattr(last_message, 'content') else last_message
            content = self._extract_clean_text(content)

            # Look for tool outputs containing the JSON marker in this turn
            for msg in new_messages:
                if msg.type == "tool":
                    tool_content = msg.content if hasattr(msg, 'content') else msg
                    tool_content = self._extract_clean_text(tool_content)
                    if "<!--RESULTS_JSON:" in tool_content:
                        # Extract the marker and append it to the final content
                        match = re.search(r'<!--RESULTS_JSON:(.*?)-->', tool_content, re.DOTALL)
                        if match:
                            content += f"\n\n<!--RESULTS_JSON:{match.group(1)}-->"
                            break

            return content

        except Exception as e:
            error_msg = f"❌ Error: {str(e)}"
            if "Connection" in str(e) or "refused" in str(e):
                error_msg += "\n\n🔌 Make sure server.py is running:\n`python server.py`"
            elif "API key" in str(e) or "authentication" in str(e).lower():
                error_msg += f"\n\n🔑 Check your {self.model_type.upper()}_API_KEY in .env"
            return error_msg

    def _extract_clean_text(self, content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and 'text' in item:
                    parts.append(item['text'])
                elif isinstance(item, str):
                    parts.append(item)
                elif hasattr(item, 'text'):
                    parts.append(item.text)
                else:
                    parts.append(str(item))
            return '\n'.join(filter(None, parts))
        return str(content)

    def reset(self):
        with self._lock:
            if self.loop:
                self.loop.close()
            self.agent = None
            self.loop = None


# ─────────────────────────────────────────────────────────────
#  PAGE: LOGIN
# ─────────────────────────────────────────────────────────────

def show_login_page():
    st.title("📁 FindMyFiles")
    st.markdown("---")

    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        tab1, tab2 = st.tabs(["🔐 Login", "📝 Register"])

        with tab1:
            st.subheader("Welcome Back!")
            with st.form("login_form"):
                username = st.text_input("Username", placeholder="Enter your username")
                password = st.text_input("Password", type="password",
                                         placeholder="Enter your password")
                submit = st.form_submit_button("🔓 Login", use_container_width=True)

                if submit:
                    if not username or not password:
                        st.error("❌ Please fill in all fields")
                    else:
                        with st.spinner("Logging in..."):
                            success, msg, user_id = auth.login_user(username, password)
                            if success:
                                session_token = auth.create_user_session(user_id)
                                st.session_state.authenticated = True
                                st.session_state.user_id = user_id
                                st.session_state.session_token = session_token
                                st.session_state.user_info = auth.get_user_info(user_id)
                                history = db.get_chat_history(user_id)
                                st.session_state.messages = [
                                    {"role": h["role"], "content": h["content"]}
                                    for h in history
                                ]
                                st.success(f"✅ {msg}")
                                st.rerun()
                            else:
                                st.error(f"❌ {msg}")

        with tab2:
            st.subheader("Create Account")
            with st.form("register_form"):
                reg_username = st.text_input("Username", placeholder="Choose a username")
                reg_email = st.text_input("Email", placeholder="your.email@example.com")
                reg_password = st.text_input("Password", type="password",
                                              placeholder="Min 8 chars, 1 uppercase, 1 number")
                reg_password_confirm = st.text_input("Confirm Password", type="password",
                                                      placeholder="Re-enter password")
                submit_reg = st.form_submit_button("📝 Register", use_container_width=True)

                if submit_reg:
                    if not all([reg_username, reg_email, reg_password, reg_password_confirm]):
                        st.error("❌ Please fill in all fields")
                    elif reg_password != reg_password_confirm:
                        st.error("❌ Passwords don't match")
                    else:
                        with st.spinner("Creating account..."):
                            success, msg, user_id = auth.register_user(
                                reg_username, reg_email, reg_password)
                            if success:
                                st.success(f"✅ {msg}")
                                st.session_state.page = "oauth_setup"
                                st.session_state.temp_user_id = user_id
                                st.rerun()
                            else:
                                st.error(f"❌ {msg}")

        st.markdown("---")
        st.caption("🔒 Your data is encrypted and secure")


# ─────────────────────────────────────────────────────────────
#  PAGE: OAUTH SETUP
# ─────────────────────────────────────────────────────────────

def show_oauth_setup_page():
    st.title("📧 Connect Your Gmail & Drive")
    st.markdown("---")

    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        st.info("""
        ### 🔗 Google Services Connection Required

        To use this app, connect your Google account.

        **What happens:**
        1. Click the button below
        2. A browser window will open
        3. Login to your Google account
        4. Grant permissions for Gmail & Drive
        5. You'll be redirected back

        **We can access:**
        - 📧 Your emails (read, search, download attachments)
        - 📁 Your Drive files (read, search, download)
        """)

        if st.button("🔗 Connect Gmail & Drive", use_container_width=True, type="primary"):
            user_id = st.session_state.temp_user_id
            with st.spinner("Opening browser for Google authorization..."):
                exists, msg = oauth.check_credentials_file()
                if not exists:
                    st.error(msg)
                else:
                    success, msg = oauth.initiate_oauth_flow(user_id)
                    if success:
                        st.success(msg)
                        st.balloons()
                        st.info("✅ Setup complete! You can now login.")
                        if st.button("Go to Login", use_container_width=True):
                            st.session_state.page = "login"
                            del st.session_state.temp_user_id
                            st.rerun()
                    else:
                        st.error(msg)

        if st.button("← Back to Login", use_container_width=True):
            st.session_state.page = "login"
            if 'temp_user_id' in st.session_state:
                del st.session_state.temp_user_id
            st.rerun()


# ─────────────────────────────────────────────────────────────
#  PAGE: MAIN APP
# ─────────────────────────────────────────────────────────────

def show_main_app():
    user_id = st.session_state.user_id
    user_info = st.session_state.user_info

    if not auth.validate_user_session(st.session_state.session_token):
        st.error("⚠️ Session expired. Please login again.")
        logout()
        return

    st.title("📁 FindMyFiles")
    st.caption(f"Logged in as: **{user_info['username']}** ({user_info['email']})")

    # ── SIDEBAR ───────────────────────────────────────────────
    with st.sidebar:
        # ── PROFILE CARD ──────────────────────────────────────
        profile_photo_url = get_gmail_profile_photo(user_id)
        username = user_info.get('username', 'User')
        email = user_info['email']
        initials = ''.join(p[0].upper() for p in username.split()[:2]) or username[0].upper()

        st.markdown("""
        <style>
        .profile-card {
            background: linear-gradient(145deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            border: 1px solid rgba(99, 179, 237, 0.25);
            border-radius: 18px;
            padding: 22px 16px 18px;
            text-align: center;
            margin-bottom: 6px;
            position: relative;
            overflow: hidden;
            box-shadow: 0 4px 24px rgba(0,0,0,0.4);
        }
        .profile-card::before {
            content: '';
            position: absolute;
            top: -50px; right: -50px;
            width: 140px; height: 140px;
            background: radial-gradient(circle, rgba(99,179,237,0.1) 0%, transparent 70%);
            border-radius: 50%;
            pointer-events: none;
        }
        .profile-card::after {
            content: '';
            position: absolute;
            bottom: -30px; left: -30px;
            width: 100px; height: 100px;
            background: radial-gradient(circle, rgba(138,99,210,0.1) 0%, transparent 70%);
            border-radius: 50%;
            pointer-events: none;
        }
        .profile-avatar-wrap {
            position: relative;
            display: inline-block;
            margin-bottom: 12px;
        }
        .profile-avatar-img {
            width: 76px;
            height: 76px;
            border-radius: 50%;
            border: 3px solid rgba(99,179,237,0.6);
            box-shadow: 0 0 0 5px rgba(99,179,237,0.1), 0 6px 24px rgba(0,0,0,0.5);
            object-fit: cover;
            display: block;
        }
        .profile-avatar-fallback {
            width: 76px;
            height: 76px;
            border-radius: 50%;
            border: 3px solid rgba(99,179,237,0.6);
            box-shadow: 0 0 0 5px rgba(99,179,237,0.1), 0 6px 24px rgba(0,0,0,0.5);
            background: linear-gradient(135deg, #4299e1, #2b6cb0);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 28px;
            font-weight: 800;
            color: #fff;
            letter-spacing: 1px;
            margin: 0 auto;
        }
        .profile-online-dot {
            position: absolute;
            bottom: 4px;
            right: 4px;
            width: 15px;
            height: 15px;
            background: #48bb78;
            border-radius: 50%;
            border: 2.5px solid #1a1a2e;
            box-shadow: 0 0 8px rgba(72,187,120,0.7);
            animation: pulse-dot 2s infinite;
        }
        @keyframes pulse-dot {
            0%, 100% { box-shadow: 0 0 8px rgba(72,187,120,0.7); }
            50% { box-shadow: 0 0 14px rgba(72,187,120,1); }
        }
        .profile-name {
            font-size: 17px;
            font-weight: 700;
            color: #e8f4fd;
            margin: 2px 0 4px;
            letter-spacing: 0.3px;
        }
        .profile-email {
            font-size: 11px;
            color: rgba(160,200,240,0.65);
            word-break: break-all;
            margin-bottom: 10px;
            line-height: 1.4;
        }
        .profile-badges {
            display: flex;
            justify-content: center;
            gap: 6px;
            flex-wrap: wrap;
        }
        .profile-badge {
            display: inline-block;
            background: rgba(66,153,225,0.15);
            border: 1px solid rgba(66,153,225,0.35);
            border-radius: 20px;
            padding: 3px 10px;
            font-size: 10px;
            color: #90cdf4;
            letter-spacing: 0.4px;
        }
        .profile-badge.green {
            background: rgba(72,187,120,0.12);
            border-color: rgba(72,187,120,0.3);
            color: #9ae6b4;
        }
        </style>
        """, unsafe_allow_html=True)

        if profile_photo_url:
            avatar_html = f'<img src="{profile_photo_url}" class="profile-avatar-img" referrerpolicy="no-referrer"/>'
        else:
            avatar_html = f'<div class="profile-avatar-fallback">{initials}</div>'

        st.markdown(f"""
        <div class="profile-card">
            <div class="profile-avatar-wrap">
                {avatar_html}
                <div class="profile-online-dot"></div>
            </div>
            <div class="profile-name">{username}</div>
            <div class="profile-email">{email}</div>
            <div class="profile-badges">
                <span class="profile-badge">✦ Member</span>
                <span class="profile-badge green">● Online</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        if st.button("🚪 Logout", use_container_width=True, type="primary"):
            logout()

        st.divider()

        st.subheader("👀 About")
        st.markdown(
            "Hello! I can help you find files from your "
            "Emails, Google Drive and Local storage."
        )

        st.divider()

        st.subheader("🧠 AI Model")
        model_options = {
            "groq": f"{MODELS['groq']['icon']} {MODELS['groq']['name']}",
            "gemini": f"{MODELS['gemini']['icon']} {MODELS['gemini']['name']}"
        }
        selected = st.selectbox(
            "Choose Model",
            options=list(model_options.keys()),
            format_func=lambda x: model_options[x],
            index=list(model_options.keys()).index(st.session_state.selected_model),
            key="model_selector"
        )
        if selected != st.session_state.selected_model:
            st.session_state.selected_model = selected
            if st.session_state.agent_manager:
                st.session_state.agent_manager.reset()
                st.session_state.agent_manager = None
            st.rerun()

        st.caption(f"**{MODELS[selected]['description']}**")

        st.divider()

        st.subheader("🔗 Connected Services")
        gmail_connected, gmail_email = oauth.verify_gmail_connection(user_id)
        if gmail_connected:
            st.success(f"📧 Gmail: {gmail_email}")
        else:
            st.error("📧 Gmail: Disconnected")

        drive_connected, drive_info = oauth.verify_drive_connection(user_id)
        if drive_connected:
            st.success(f"📁 Drive: {drive_info}")
        else:
            st.error("📁 Drive: Disconnected")

        if not gmail_connected or not drive_connected:
            if st.button("🔄 Reconnect Services", use_container_width=True):
                success, msg = oauth.initiate_oauth_flow(user_id)
                if success:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

        st.divider()

        # ── DOWNLOADED FILES PANEL ────────────────────────────
        col_title, col_refresh = st.columns([3, 1])
        with col_title:
            st.subheader("📂 Downloaded Files")
        with col_refresh:
            if st.button("🔄", help="Refresh", key="refresh_btn", use_container_width=True):
                st.rerun()

        if 'last_file_count' not in st.session_state:
            st.session_state.last_file_count = 0

        user_id_str = f"user_{st.session_state.user_id}"
        base_dir = os.path.dirname(os.path.abspath(__file__))
        attachments_dir = os.path.join(base_dir, "user_data", user_id_str, "Attachments")
        os.makedirs(attachments_dir, exist_ok=True)

        if os.path.exists(attachments_dir):
            files = [f for f in os.listdir(attachments_dir)
                     if os.path.isfile(os.path.join(attachments_dir, f))]
            current_file_count = len(files)

            if current_file_count != st.session_state.last_file_count:
                if current_file_count > st.session_state.last_file_count:
                    st.toast("✅ New file downloaded!", icon="📥")
                st.session_state.last_file_count = current_file_count

            if files:
                col_metric, col_indicator = st.columns([2, 1])
                with col_metric:
                    st.metric("Total Files", len(files))
                with col_indicator:
                    st.caption("🟢 Live")

                with st.container(height=400):
                    for idx, file in enumerate(sorted(files, reverse=True), 1):
                        file_path = os.path.join(attachments_dir, file)
                        file_size = os.path.getsize(file_path) / 1024
                        file_modified = datetime.fromtimestamp(os.path.getmtime(file_path))

                        ext = os.path.splitext(file)[1].lower()
                        icon_map = {
                            '.pdf': '📕', '.doc': '📘', '.docx': '📘',
                            '.xls': '📗', '.xlsx': '📗',
                            '.ppt': '📙', '.pptx': '📙',
                            '.jpg': '🖼️', '.jpeg': '🖼️', '.png': '🖼️',
                            '.zip': '📦', '.rar': '📦', '.txt': '📄',
                        }
                        icon = icon_map.get(ext, '📎')

                        time_diff = (datetime.now() - file_modified).total_seconds()
                        new_badge = "🆕 " if time_diff < 60 else ""

                        st.markdown(f"{new_badge}**{idx}. {icon} {file}**")
                        st.caption(
                            f"💾 {file_size:.1f} KB · "
                            f"{file_modified.strftime('%I:%M %p')}"
                        )

                        col1, col2 = st.columns(2)
                        with col1:
                            if st.button("👁️ Open", key=f"sidebar_open_{file}",
                                         use_container_width=True):
                                try:
                                    if os.name == 'nt':
                                        os.startfile(file_path)
                                    elif os.name == 'posix':
                                        import subprocess
                                        subprocess.Popen(['open', file_path])
                                except Exception as e:
                                    st.error(f"❌ {e}")

                        with col2:
                            with open(file_path, 'rb') as f:
                                st.download_button(
                                    label="💾 Save",
                                    data=f.read(),
                                    file_name=file,
                                    key=f"sidebar_dl_{file}",
                                    use_container_width=True
                                )

                        with st.expander("⚙️ More"):
                            st.code(file_path, language=None)
                            if st.button("🗑️ Delete", key=f"sidebar_del_{file}",
                                         type="secondary"):
                                os.remove(file_path)
                                st.rerun()

                        st.divider()
            else:
                st.caption("No downloaded files yet.")

        st.divider()

        st.subheader("⚙️ Controls")
        if st.button("🗑️ Clear Chat", use_container_width=True):
            db.clear_chat_history(st.session_state.user_id)
            st.session_state.messages = []
            st.rerun()

        if st.button("🔄 Reset Agent", use_container_width=True):
            if st.session_state.agent_manager:
                st.session_state.agent_manager.reset()
                st.session_state.agent_manager = None
            st.success("Agent reset!")
            st.rerun()

    # ── MAIN CHAT AREA ────────────────────────────────────────
    st.markdown("---")

    # Replay full chat history — each message in its own chat_message block
    display_chat_history(st.session_state.messages, user_id)

    # Handle new user input
    if prompt := st.chat_input("Ask me about your emails, attachments, or files..."):
        # 1. Save and immediately show the user message
        st.session_state.messages.append({"role": "user", "content": prompt})
        db.save_chat_message(user_id, "user", prompt)

        with st.chat_message("user", avatar="👤"):
            st.markdown(prompt)

        # 2. Run the agent and show the assistant reply
        with st.chat_message("assistant", avatar="🤖"):
            with st.spinner("Thinking..."):
                agent = get_or_create_agent(user_id)
                # Pass history EXCLUDING the message we just added
                history = st.session_state.messages[:-1]
                response = agent.chat(prompt, history)

            # ✅ FIX: render content INSIDE this chat_message block,
            #         NOT via display_message() which opens another one
            render_assistant_content(response, user_id, msg_idx=len(st.session_state.messages))

        # 3. Save to state + DB after rendering
        st.session_state.messages.append({"role": "assistant", "content": response})
        db.save_chat_message(user_id, "assistant", response)


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────

def get_or_create_agent(user_id: int):
    if st.session_state.agent_manager is None:
        try:
            agent = SmartAgent(
                user_id,
                model_type=st.session_state.selected_model
            )
            # Trigger lazy init now so errors surface immediately
            agent._ensure_initialized()
            st.session_state.agent_manager = agent
            st.toast(
                f"✅ {MODELS[st.session_state.selected_model]['name']} ready!",
                icon="🤖"
            )
        except Exception as e:
            st.error(f"❌ Failed to initialize agent: {str(e)}")
            st.stop()
    return st.session_state.agent_manager


def logout():
    if st.session_state.session_token:
        auth.logout_user(st.session_state.session_token)
    for key in ["authenticated", "user_id", "session_token",
                "user_info", "messages", "agent_manager"]:
        st.session_state[key] = None if key != "authenticated" else False
    st.session_state.messages = []
    st.session_state.page = "login"
    st.rerun()


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main():
    db.initialize_database()
    init_session_state()
    db.cleanup_expired_sessions()

    if not st.session_state.authenticated:
        if st.session_state.page == "oauth_setup":
            show_oauth_setup_page()
        else:
            show_login_page()
    else:
        show_main_app()


if __name__ == "__main__":
    main()