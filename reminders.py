"""Reminder engine: natural-language time specs → scheduled buzzes.

The router LLM extracts a normalized when_spec ("in 10m", "at 18:30",
"tomorrow 9am"); parse() turns that into a datetime in Dhaka time.
"""

import re
import threading
import time
from datetime import datetime, timedelta

import comms
import config
import state

REMINDERS = []  # live list: [{"due": datetime (Dhaka), "text": str}]

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

USAGE = ("Remind me how? 🙂 Try:\n"
         "• remind me in 5m to check the oven\n"
         "• in 2h call mom\n"
         "• remind me at 18:30 to pray\n"
         "• tomorrow 9am standup notes")


def fmt_delta(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m" if not s else f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h" if not m else f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d" if not h else f"{d}d {h}h"


def _now():
    return datetime.now() + config.BD_OFFSET


def parse(spec):
    """'in 5m buy eggs' / 'at 18:30 call' / 'tomorrow 9am X' →
    (due datetime in Dhaka time, text) or None if unparseable."""
    spec = (spec or "").strip().strip(",").strip()
    m = re.match(r"^in\s+(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*(.*)$", spec, re.I)
    if m:
        unit = m.group(2).lower()
        if unit in _UNIT_SECONDS:
            due = _now() + timedelta(
                seconds=float(m.group(1)) * _UNIT_SECONDS[unit])
            return due, (m.group(3).strip(" ,:") or "(no text)")
    m = re.match(
        r"^(tomorrow\s+)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(.*)$",
        spec, re.I)
    if m:
        tomorrow, hour, minute, ap, rest = m.groups()
        hour, minute = int(hour), int(minute or 0)
        rest = rest.strip(" ,:")
        if not (tomorrow or ap or ":" in spec or rest):
            return None  # bare number — not a time
        if ap:
            ap = ap.lower()
            if ap == "pm" and hour < 12:
                hour += 12
            elif ap == "am" and hour == 12:
                hour = 0
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        now = _now()
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if tomorrow or due <= now:  # a passed time means the next day
            due += timedelta(days=1)
        return due, (rest or "(no text)")
    return None


def _sync():
    state.STATE["reminders"] = [
        {"due": r["due"].isoformat(), "text": r["text"]} for r in REMINDERS]
    state.save_soon()


def add(spec):
    """Parse + store a reminder. Returns True on success."""
    parsed = parse(spec)
    if not parsed:
        return False
    due, text = parsed
    REMINDERS.append({"due": due, "text": text})
    _sync()
    comms.log(f"reminder set: {text[:40]} @ {due:%H:%M:%S}")
    comms.send(f"⏰ <b>Set</b> — {comms.esc(text)}\n"
               f"At {due:%H:%M} (in {fmt_delta((due - _now()).total_seconds())})",
               html=True)
    return True


def cancel(index):
    """Cancel reminder #index (1-based)."""
    if 0 <= index - 1 < len(REMINDERS):
        removed = REMINDERS.pop(index - 1)
        _sync()
        return f"Cancelled: {removed['text']}"
    return f"No reminder #{index}. Use /reminders."


def list_pending():
    if not REMINDERS:
        return "No pending reminders."
    now = _now()
    return "\n".join(
        f"{i+1}. {r['text']} — at {r['due']:%H:%M} "
        f"(in {fmt_delta((r['due'] - now).total_seconds())})"
        for i, r in enumerate(REMINDERS))


def fire_due():
    """Send every reminder whose time has come. Missed ones fire on boot."""
    now = _now()
    for r in [r for r in REMINDERS if r["due"] <= now]:
        REMINDERS.remove(r)
        _sync()
        comms.send(f"⏰ <b>Reminder</b>: {comms.esc(r['text'])}", html=True)


def restore():
    """Rebuild live reminders from gist state (called on boot)."""
    for item in state.STATE.get("reminders", []):
        try:
            REMINDERS.append({"due": datetime.fromisoformat(item["due"]),
                              "text": item["text"]})
        except Exception:
            pass  # malformed entry — skip


def loop():
    while True:
        try:
            fire_due()
        except Exception as e:
            print("[reminders] loop error:", e)
        time.sleep(10)


def start():
    threading.Thread(target=loop, daemon=True).start()
