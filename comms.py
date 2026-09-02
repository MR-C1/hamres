"""Telegram plumbing: sending (HTML-aware), typing indicator, file
downloads, email, and the in-memory activity log."""

import html as _html
from datetime import datetime

import requests

import config

TG_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

LOG = []  # last N events, shown by /status


def log(event):
    line = f"{datetime.now():%H:%M:%S} {event}"
    print("[hermes]", line)
    LOG.append(line)
    del LOG[:-20]


# --- messages ---------------------------------------------------------------

def esc(s):
    """Escape user-derived text for safe use inside HTML messages."""
    return _html.escape(str(s or ""))


def send(text, chat_id=None, html=False):
    """Send a message, split at Telegram's 4096-char cap.
    html=True enables HTML formatting (only for text we constructed).
    Returns True if Telegram accepted every chunk."""
    chat_id = chat_id or config.OWNER_CHAT_ID
    if not chat_id or not text:
        return False
    ok_all = True
    for i in range(0, len(text), 4000):
        payload = {"chat_id": chat_id, "text": text[i:i + 4000]}
        if html:
            payload["parse_mode"] = "HTML"
        try:
            r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=10)
            ok_all = ok_all and bool(r.json().get("ok", False))
        except Exception as e:
            print("[telegram] send failed:", e)
            ok_all = False
    return ok_all


def typing(chat_id=None):
    """Show the 'typing…' indicator while the model works."""
    chat_id = chat_id or config.OWNER_CHAT_ID
    if not chat_id:
        return
    try:
        requests.post(f"{TG_API}/sendChatAction",
                      json={"chat_id": chat_id, "action": "typing"},
                      timeout=5)
    except Exception:
        pass


def download(file_id):
    """Download a Telegram file (photo/voice) → (bytes, file_path)."""
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id},
                     timeout=15)
    r.raise_for_status()
    fp = r.json()["result"]["file_path"]
    r2 = requests.get(
        f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{fp}",
        timeout=60,
    )
    r2.raise_for_status()
    return r2.content, fp


COMMAND_MENU = [
    ("help", "What I can do"),
    ("tasks", "List automations"),
    ("run", "Run a task now"),
    ("ask", "Quick research"),
    ("deep", "Deep research (2 req)"),
    ("say", "Chat with the LLM"),
    ("remind", "Set a reminder"),
    ("reminders", "Pending reminders"),
    ("memories", "What I remember"),
    ("expenses", "Expense log"),
    ("quota", "LLM usage per provider"),
    ("report", "Today's activity"),
    ("status", "Health check"),
    ("kill", "Stop an automation"),
    ("enable", "Resume a paused one"),
    ("diag", "Test LLM connection"),
    ("verify", "Full self-test (13 LLM calls)"),
]


def register_menu():
    """Show the command menu inside Telegram's '/' button."""
    if not config.TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"{TG_API}/setMyCommands",
            json={"commands": [{"command": c, "description": d}
                               for c, d in COMMAND_MENU]},
            timeout=10,
        )
    except Exception:
        pass  # cosmetic — never block boot


# --- email -------------------------------------------------------------------

def send_email(to, subject, body):
    """Returns None on success, or an error/config message."""
    if not (config.SMTP_USER and config.SMTP_PASS):
        return ("Email isn't configured. On Render set SMTP_USER (your "
                "Gmail) and SMTP_PASS (an app password — Google account → "
                "Security → 2-Step Verification → App passwords).")
    import smtplib
    from email.mime.text import MIMEText

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = config.SMTP_USER
        msg["To"] = to
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)
        return None
    except Exception as e:
        return f"Send failed: {e}"
