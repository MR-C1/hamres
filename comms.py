"""Telegram plumbing for the brain: sending (HTML), typing indicator,
inline keyboard buttons, callback handling, and the command menu."""

import html as _html
import re
from datetime import datetime

import requests

import config

TG_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

LOG = []  # last N events, shown by /status


def log(event):
    line = f"{datetime.now() + config.BD_OFFSET:%H:%M:%S} {event}"
    print("[agent]", line)
    LOG.append(line)
    del LOG[:-30]


def esc(s):
    return _html.escape(str(s or ""))


def md(text):
    """Escape for HTML, then convert common markdown to Telegram HTML."""
    s = esc(text)
    s = re.sub(r"^#{1,4}\s*(.+?)\s*$", r"<b>\1</b>", s, flags=re.M)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=re.S)
    s = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", s)
    return s


def _post(method, payload, timeout=15):
    r = requests.post(f"{TG_API}/{method}", json=payload, timeout=timeout)
    ok = bool(r.json().get("ok", False)) if r.headers.get(
        "content-type", "").startswith("application/json") else False
    return r, ok


def send(text, chat_id=None, html=False):
    """Send a message, split at Telegram's 4096-char cap."""
    chat_id = chat_id or config.OWNER_CHAT_ID
    if not chat_id or not text:
        return False
    ok_all = True
    for i in range(0, len(text), 4000):
        payload = {"chat_id": chat_id, "text": text[i:i + 4000]}
        if html:
            payload["parse_mode"] = "HTML"
        try:
            _, ok = _post("sendMessage", payload)
            ok_all = ok_all and ok
        except Exception as e:
            print("[telegram] send failed:", e)
            ok_all = False
    return ok_all


def send_md(text, chat_id=None):
    return send(md(text), chat_id=chat_id, html=True)


def send_buttons(text, buttons, chat_id=None):
    """buttons: list of rows, each row a list of (label, callback_data)."""
    chat_id = chat_id or config.OWNER_CHAT_ID
    if not chat_id:
        return False
    kb = {"inline_keyboard": [
        [{"text": label, "callback_data": cb} for label, cb in row]
        for row in buttons
    ]}
    try:
        _, ok = _post("sendMessage", {
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "reply_markup": kb,
        })
        return ok
    except Exception as e:
        print("[telegram] send_buttons failed:", e)
        return False


def answer_callback(callback_query_id, text=""):
    try:
        _post("answerCallbackQuery",
              {"callback_query_id": callback_query_id, "text": text})
    except Exception:
        pass


def typing(chat_id=None):
    chat_id = chat_id or config.OWNER_CHAT_ID
    if not chat_id:
        return
    try:
        _post("sendChatAction", {"chat_id": chat_id, "action": "typing"}, 5)
    except Exception:
        pass


COMMAND_MENU = [
    ("status", "System health + queue"),
    ("stats", "Channel growth numbers"),
    ("next", "Render the next video now"),
    ("pause", "Pause auto operations"),
    ("resume", "Resume auto operations"),
    ("settings", "Show settings"),
    ("idea", "Draft a script on a topic"),
    ("report", "Today's report"),
    ("diag", "Test the AI providers"),
    ("help", "What I can do"),
]


def register_menu():
    if not config.TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(f"{TG_API}/setMyCommands", json={
            "commands": [{"command": c, "description": d}
                         for c, d in COMMAND_MENU]}, timeout=10)
    except Exception:
        pass  # cosmetic — never block boot
