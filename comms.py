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
    now = datetime.now() + config.BD_OFFSET
    line = f"{now:%H:%M:%S} {event}"
    print("[agent]", line)
    LOG.append(line)
    del LOG[:-30]
    _persist(now, event)


def _persist(now, event):
    """Mirror the line into saved state so the ledger survives a restart.

    LOG is memory-only: Render's free tier restarts often and every line is
    lost, which is why the panel's Ledger used to look empty. State lives in
    the gist, so a dated copy there is the only durable record. state is
    imported lazily (comms is imported first at boot) and save_soon() is
    debounced + no-ops until state is loaded, so a burst of log lines is one
    gist write and never a boot-order problem.
    """
    try:
        import state
        tail = state.STATE.setdefault("log_tail", [])
        tail.append({"t": f"{now:%Y-%m-%d %H:%M:%S}", "m": str(event)[:240]})
        del tail[:-250]
        state.save_soon()
    except Exception:
        pass  # logging must never be the thing that breaks


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


def _chunks(text, limit=4000):
    """Split for Telegram's 4096 cap, preferring a line break.

    A blind cut every 4000 characters can land inside a tag, or between a
    <b> and its close — Telegram rejects a chunk whose HTML it can't parse,
    so that part of the message simply never arrived.
    """
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:  # no usable break in range — hard cut
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


def _plain(s):
    """Same text with the markup taken out, for the no-parse_mode retry."""
    return _html.unescape(re.sub(r"</?[a-zA-Z][^>]{0,80}>", "", s))


def send(text, chat_id=None, html=False):
    """Send a message, split at Telegram's 4096-char cap."""
    chat_id = chat_id or config.OWNER_CHAT_ID
    if not chat_id or not text:
        return False
    ok_all = True
    for chunk in _chunks(str(text)):
        payload = {"chat_id": chat_id, "text": chunk}
        if html:
            payload["parse_mode"] = "HTML"
        try:
            _, ok = _post("sendMessage", payload)
            if not ok and html:
                # Telegram refused the HTML — a tag split across chunks, or
                # markup a model invented. The words matter more than the
                # bold, so send the same chunk again as plain text instead
                # of letting it vanish.
                _, ok = _post("sendMessage", {"chat_id": chat_id,
                                              "text": _plain(chunk)})
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
    # The buttons ride on this one message, so it cannot be split — a message
    # over the 4096 cap is rejected outright and the owner is left with no
    # ✅/❌ at all. Trim instead, and say that it was trimmed.
    if len(text) > 4000:
        text = text[:3900].rstrip() + "\n…(trimmed)"
    # The buttons ride on this one message, so it cannot be split — a message
    # over the 4096 cap is rejected outright and the owner is left with no
    # ✅/❌ at all. Trim instead, and say that it was trimmed.
    if len(text) > 4000:
        text = text[:3900]
        if text.rfind("<") > text.rfind(">"):  # sliced inside a tag
            text = text[:text.rfind("<")]
        text = text.rstrip() + "\n…(trimmed)"
    try:
        _, ok = _post("sendMessage", {
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "reply_markup": kb,
        })
        if not ok:
            # HTML Telegram won't parse (an unclosed tag left by the trim,
            # or markup a model invented) must not cost the owner the
            # buttons — resend the same text plain, with the keyboard.
            _, ok = _post("sendMessage", {
                "chat_id": chat_id, "text": _plain(text),
                "reply_markup": kb,
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
    ("clear", "Empty the job queue"),
    ("publish", "Publish pending videos"),
    ("queue", "Show the job queue"),
    ("pending", "Videos awaiting your decision"),
    ("log", "Recent brain activity"),
    ("retry", "Requeue the last failed job"),
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
