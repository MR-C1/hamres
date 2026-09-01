"""
Hermes — personal 24/7 agent (free Render hosting, no credit card).

You control it from your own Telegram account; it runs on the server and
reports back. Owner-only: it ignores anyone whose chat id isn't yours.

Commands you send it:
  /help          — list commands
  /tasks         — list installed tasks
  /run <name>    — run a task right now
  /ask <query>   — research a question (LLM + web search)
  /status        — heartbeat: uptime, memory, task log tail
  /say <text>    — plain chat with the LLM
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

import psutil
import requests
from flask import Flask

from tasks import TASKS, build_dynamic_task

_CODE_TASKS = frozenset(TASKS)  # tasks defined in tasks.py (not via message)

from runner import run_task
import state

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config — secrets live in Render's Environment tab, never in code
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")  # only you can command it
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openrouter/auto")
# Fallback chain: comma-separated model ids tried in order when the primary
# is down/overloaded/rate-limited. Add or reorder freely.
LLM_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("LLM_FALLBACK_MODELS", "").split(",")
    if m.strip()
]

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

STARTED_AT = time.time()
LOG = []  # last N events, kept in memory (sent to you via /status)


def log(event):
    line = f"{datetime.now():%H:%M:%S} {event}"
    print("[hermes]", line)
    LOG.append(line)
    del LOG[:-20]  # keep last 20


# ---------------------------------------------------------------------------
# LLM + web search helpers (used by tasks and /ask)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# LLM layer — speaks BOTH provider formats and auto-detects which one works.
#   openai format    : POST {base}/v1/chat/completions   (OpenRouter etc.)
#   anthropic format : POST {base}/v1/messages           (AgentRouter etc.)
# Raw requests (no SDK) so failures show the server's own error text.
# ---------------------------------------------------------------------------

_llm_format = None  # cached auto-detected format: "openai" | "anthropic"


def _base_url():
    return (LLM_BASE_URL or "https://openrouter.ai/api/v1").rstrip("/")


def _raise(resp):
    body = resp.text[:600]
    raise RuntimeError(f"HTTP {resp.status_code} from {_base_url()}: {body}")


def _openai_url():
    base = _base_url()
    # Fully-qualified bases (end /v1, or Gemini-style .../openai) need only
    # the method path; OpenRouter-style bare bases need /v1 added.
    if base.endswith("/v1") or "/openai" in base:
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _anthropic_url():
    base = _base_url()
    if base.endswith("/v1") or "/messages" in base:
        return base + "/messages"
    return base + "/v1/messages"


def _llm_openai(messages, max_tokens, model):
    url = _openai_url()
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={"model": model, "messages": messages, "max_tokens": max_tokens},
        timeout=90,
    )
    if resp.status_code != 200:
        _raise(resp)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"provider error: {str(data['error'])[:600]}")
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected response: {str(data)[:600]}")


def _llm_anthropic(messages, max_tokens, model):
    url = _anthropic_url()
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    chat = [{"role": m["role"], "content": m["content"]}
            for m in messages if m["role"] != "system"]
    # Send both auth headers: proxies accept either.
    headers = {
        "x-api-key": LLM_API_KEY,
        "Authorization": f"Bearer {LLM_API_KEY}",
        "anthropic-version": "2023-06-01",
    }
    body = {"model": model, "max_tokens": max_tokens, "messages": chat}
    if system:
        body["system"] = system
    resp = requests.post(url, headers=headers, json=body, timeout=90)
    if resp.status_code != 200:
        _raise(resp)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"provider error: {str(data['error'])[:600]}")
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if isinstance(b, dict)
    ).strip()
    if not text:
        raise RuntimeError(f"unexpected response: {str(data)[:600]}")
    return text


_FORMATS = (  # anthropic first: AgentRouter is anthropic-native
    ("anthropic", _llm_anthropic),
    ("openai", _llm_openai),
)


def model_chain():
    """Primary model first, then fallbacks, deduplicated."""
    chain = [LLM_MODEL] + LLM_FALLBACK_MODELS
    seen, out = set(), []
    for m in chain:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


DAILY_FREE_LIMIT = 50  # OpenRouter free tier; see /quota


def _today():
    return datetime.now().strftime("%Y-%m-%d")  # UTC day, matching OR's reset


def _count_request():
    """Track LLM requests per day in state so it survives restarts."""
    q = state.STATE.setdefault("quota", {"day": _today(), "used": 0})
    if q["day"] != _today():  # new day — reset counter
        q["day"] = _today()
        q["used"] = 0
    q["used"] += 1
    state.save_soon()
    return q["used"]


def _quota_report():
    q = state.STATE.setdefault("quota", {"day": _today(), "used": 0})
    if q["day"] != _today():
        q["day"] = _today()
        q["used"] = 0
    return q


def llm(messages, max_tokens=800):
    global _llm_format
    forced = os.environ.get("LLM_API_FORMAT", "").strip().lower()

    errors = []
    for model in model_chain():
        # 1. explicit format setting, 2. cached detection, 3. probe both
        order = [f for f in _FORMATS if f[0] == forced] if forced else (
            [f for f in _FORMATS if f[0] == _llm_format] or list(_FORMATS)
        )
        for name, fn in order:
            try:
                text = fn(messages, max_tokens, model)
                _llm_format = name
                used = _count_request()
                if used == DAILY_FREE_LIMIT - 5:  # warn once near the cap
                    tg_send(f"⚠️ Free-tier budget almost gone: "
                            f"{used}/{DAILY_FREE_LIMIT} requests today.")
                if model != LLM_MODEL:
                    log(f"fallback used: {model} ({name})")
                return text
            except Exception as e:
                errors.append(f"{model} [{name}]: {str(e)[:200]}")
    raise RuntimeError(
        "all models failed — run /diag:\n" + "\n".join(errors[:4])
    )


def diagnose():
    """Connection test — walks the whole model chain, reports which models
    answer and which fail, with the server's own error text."""
    lines = [
        f"base_url: {_base_url()}",
        f"key: {'set (' + str(len(LLM_API_KEY)) + ' chars)' if LLM_API_KEY else 'NOT SET'}",
        f"format: {os.environ.get('LLM_API_FORMAT', 'auto-detect').strip() or 'auto-detect'}",
        f"chain: {' → '.join(model_chain())}\n",
    ]

    test = [{"role": "user", "content": "Reply with the single word: ok"}]
    worked = []
    for model in model_chain():
        last_err = ""
        for name, fn in _FORMATS:
            try:
                text = fn(test, 20, model)
                lines.append(f"✅ {model} ({name}) — replied: {text[:60]!r}")
                worked.append(model)
                break
            except Exception as e:
                last_err = f"{name}: {str(e)[:300]}"
        else:
            # no format worked for this model — report the last error seen
            lines.append(f"❌ {model} — {last_err}\n")

    if worked:
        lines.append(f"\n{len(worked)}/{len(model_chain())} models healthy.")
    else:
        lines.append("No model answered — check LLM_API_KEY / base_url.")
    return "\n".join(lines)


def web_search(query, max_results=5):
    """Free web search, no API key. Returns [{'title':..., 'url':..., 'body':...}]."""
    from ddgs import DDGS

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


# ---------------------------------------------------------------------------
# Telegram plumbing
# ---------------------------------------------------------------------------

def tg_send(text, chat_id=None):
    chat_id = chat_id or OWNER_CHAT_ID
    if not chat_id:
        return
    try:
        # Telegram caps messages at 4096 chars; split longer research reports
        for i in range(0, len(text), 4000):
            requests.post(
                f"{TG_API}/sendMessage",
                json={"chat_id": chat_id, "text": text[i : i + 4000]},
                timeout=10,
            )
    except Exception as e:
        print("[telegram] send failed:", e)


def is_owner(msg):
    return OWNER_CHAT_ID and str(msg["chat"]["id"]) == str(OWNER_CHAT_ID)


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------

HELP = """Hermes — your agent. Commands:
/tasks — list installed tasks
/run <name> — run a task now
/ask <question> — research it (web + LLM)
/status — uptime, memory, recent log
/say <text> — chat with the LLM
/reminders — pending reminders
/remind <when> <text> — set one (in 5m / at 18:30 / tomorrow 9am)
/quota — LLM requests used today
/kill <name> — stop a task created by message
/diag — test the LLM connection, show any error

Or just talk: "remind me in 10m ..." sets a reminder, and
"alert me when bitcoin drops below 60000" / "every day 8am
brief me on cricket" CREATE automations from your message."""

UNAUTHORIZED = "Not authorized. This agent answers its owner only."


def do_research(question):
    log(f"research: {question[:60]}")
    try:
        results = web_search(question)
    except Exception as e:
        return f"Web search failed ({e}). LLM-only answer:\n\n{llm([{'role': 'user', 'content': question}])}"

    context = "\n\n".join(
        f"[{i+1}] {r['title']}\n{r['href']}\n{r['body']}"
        for i, r in enumerate(results)
    )
    answer = llm(
        [
            {
                "role": "system",
                "content": "Research assistant. Answer the user's question using "
                "the web results. Cite sources as [1], [2]. Be concise and "
                "concrete. If results don't answer it, say so.",
            },
            {"role": "user", "content": f"Question: {question}\n\nWeb results:\n{context}"},
        ]
    )
    return f"{answer}\n\nSources:\n" + "\n".join(
        f"[{i+1}] {r['href']}" for i, r in enumerate(results)
    )


# ---------------------------------------------------------------------------
# Natural-language task creation — ONE LLM request classifies + replies
# ---------------------------------------------------------------------------

INTENT_SYSTEM = """You are Hermes, the intent router for a personal Telegram agent.
Current local time: {NOW} (Dhaka, UTC+6).
The user sends a message. Decide what to do and reply with a SINGLE JSON
object only — no markdown fences, no commentary.

Automations you may create (fill params from the user's words):
- Crypto price alert: {"action":"task","type":"price_alert","params":{"coin":"<coingecko id, e.g. bitcoin, ethereum, solana>","below":<USD number or null>,"above":<USD number or null>,"every_minutes":<int, default 30>}}
- Daily briefing: {"action":"task","type":"briefing","params":{"topic":"<topic>","hour":<0-23>,"minute":<0-59>}}
- Page change watch: {"action":"task","type":"watch_page","params":{"url":"https://...","every_minutes":<int, default 60>}}
- One-time reminder: {"action":"reminder","when_spec":"<in 10m | in 2h | at 18:30 | tomorrow 9am>","text":"<what to remember>"}

For everything else — greetings, questions, chat:
- {"action":"chat","reply":"<your reply, 1-3 sentences>"}

Rules: prices are USD. If the user asks for an automation you cannot build
from the list above, use "chat" and briefly mention what you can automate
(price alerts, briefings, page watches, reminders). Keep chat replies short."""


def _extract_json(raw):
    """Pull the first JSON object out of an LLM reply, tolerating fences."""
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except Exception:
        return None


def handle_plain_text(text):
    """One LLM call decides: create a task, set a reminder, or just chat."""
    now = datetime.now() + BD_OFFSET
    raw = llm(
        [
            {"role": "system",
             "content": INTENT_SYSTEM.replace("{NOW}", f"{now:%Y-%m-%d %H:%M}")},
            {"role": "user", "content": text},
        ],
        max_tokens=400,
    )
    data = _extract_json(raw)

    if not data:  # model didn't produce JSON — treat its text as a chat reply
        tg_send(raw or "…")
        return

    action = data.get("action")

    if action == "task":
        try:
            name = build_dynamic_task(data)
            # remember the recipe so it can be rebuilt after a restart
            state.STATE.setdefault("dynamic_tasks", []).append(
                {"type": data.get("type"), "params": data.get("params") or {}}
            )
            state.save_soon()
            t = TASKS[name]
            tg_send(
                f"✅ Task created: {t['desc']}\n"
                f"Saved — it survives restarts. Test it now with /run {name}"
            )
            log(f"dynamic task: {name}")
        except Exception as e:
            tg_send(f"Couldn't build that task: {e}")
    elif action == "reminder":
        spec = f"{data.get('when_spec', '')} {data.get('text', '')}".strip()
        if not add_reminder(spec):
            tg_send(REMIND_USAGE)
    else:  # chat
        tg_send(data.get("reply") or raw or "…")


def handle_message(msg):
    text = (msg.get("text") or "").strip()
    if not text or not is_owner(msg):
        if text and not is_owner(msg):
            tg_send(UNAUTHORIZED, msg["chat"]["id"])
        return

    log(f"command: {text[:60]}")
    try:
        dispatch(text)
    except Exception as e:
        # Errors belong in the owner's chat, not only the server console.
        import traceback

        traceback.print_exc()
        tg_send(f"⚠️ Command failed: {type(e).__name__}: {e}")


def dispatch(text):
    if text == "/help" or text == "/start":
        tg_send(HELP)
    elif text == "/diag":
        tg_send(diagnose())
    elif text == "/tasks":
        tg_send("\n".join(f"{name} — {t['desc']}" for name, t in TASKS.items()))
    elif text == "/status":
        up = int(time.time() - STARTED_AT)
        mem = psutil.Process().memory_info().rss // (1024 * 1024)
        mode = "gist-backed" if state.GIST_TOKEN else "memory-only"
        tg_send(
            f"Up {up // 3600}h {(up % 3600) // 60}m, {mem}MB RAM, state: {mode}\n"
            + "\n".join(LOG[-10:])
        )
    elif text.startswith("/run "):
        name = text[5:].strip().split()[0]
        if name not in TASKS:
            tg_send(f"No task '{name}'. Use /tasks.")
            return
        tg_send(f"Running {name}...")
        run_task(name)  # result is delivered by the task itself
    elif text.startswith("/ask "):
        tg_send(do_research(text[5:].strip()))
    elif text.startswith("/say "):
        tg_send(llm([{"role": "user", "content": text[5:]}]))
    elif text == "/quota":
        q = _quota_report()
        reset = "midnight UTC (6am Dhaka)"
        tg_send(f"LLM requests today: {q['used']}/{DAILY_FREE_LIMIT}\n"
                f"Resets at {reset}. Reminders and free tasks "
                f"(price/page watches) don't count.")
    elif text.startswith("/kill "):
        name = text[6:].strip()
        if name not in TASKS:
            tg_send(f"No task '{name}'. Use /tasks.")
        elif name in _CODE_TASKS:  # written in tasks.py — remove from there
            tg_send(f"'{name}' is code-defined — delete it from tasks.py "
                    f"to stop it permanently.")
        else:
            t = TASKS.pop(name)
            # drop from saved state so restarts don't revive it. Names are
            # deterministic, so compute each spec's name and compare.
            from tasks import dyn_task_name

            state.STATE["dynamic_tasks"] = [
                s for s in state.STATE.get("dynamic_tasks", [])
                if dyn_task_name(s) != name
            ]
            state.save_soon()
            log(f"task killed: {name}")
            tg_send(f"🗑 Killed: {t['desc']}")
    elif text == "/reminders":
        if not REMINDERS:
            tg_send("No pending reminders.")
        else:
            now = datetime.now() + BD_OFFSET
            tg_send("\n".join(
                f"{i+1}. {r['text']} — at {r['due']:%H:%M} "
                f"(in {_fmt_delta((r['due'] - now).total_seconds())})"
                for i, r in enumerate(REMINDERS)
            ))
    elif text.startswith("/remind"):
        spec = text[len("/remind"):].strip()
        m = re.match(r"^cancel\s+(\d+)$", spec, re.I)
        if m:
            i = int(m.group(1)) - 1
            if 0 <= i < len(REMINDERS):
                removed = REMINDERS.pop(i)
                _sync_reminders_state()
                tg_send(f"Cancelled: {removed['text']}")
            else:
                tg_send(f"No reminder #{m.group(1)}. Use /reminders.")
        elif not spec:
            tg_send(REMIND_USAGE)
        elif not add_reminder(spec):
            tg_send(REMIND_USAGE)
    elif not text.startswith("/"):
        # Plain text: the human way. "remind me in 5m …" / "in 2h …" set a
        # reminder; anything else is just chat with the LLM.
        low = text.lower()
        if low.startswith("remind me "):
            spec = text[10:]
        elif low.startswith("remind "):
            spec = text[7:]
        elif re.match(r"^in\s+\d", low):
            spec = text
        else:
            spec = None
        if spec is not None and not add_reminder(spec):
            tg_send(REMIND_USAGE)
        elif spec is None:
            handle_plain_text(text)
    else:
        tg_send(HELP)


# ---------------------------------------------------------------------------
# Background loops: Telegram listener + scheduled tasks
# ---------------------------------------------------------------------------

def telegram_loop():
    if not TELEGRAM_BOT_TOKEN:
        print("[hermes] TELEGRAM_BOT_TOKEN not set — commands disabled")
        return
    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{TG_API}/getUpdates", params=params, timeout=30)
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                if update.get("message"):
                    handle_message(update["message"])
        except Exception as e:
            print("[telegram] loop error:", e)
            time.sleep(5)


def _run_task_safely(name):
    try:
        run_task(name)
    except Exception as e:
        import traceback

        traceback.print_exc()
        tg_send(f"⚠️ Task '{name}' failed: {e}")


BD_OFFSET = timedelta(hours=6)  # Bangladesh is UTC+6


def scheduler_loop():
    """Every minute, run any task whose schedule says it's due.
    Schedules are written in DHAKA time — Render's clock is UTC, so we
    shift before checking. Each task runs in its own thread so a slow
    one can't delay the others."""
    if not (TELEGRAM_BOT_TOKEN and OWNER_CHAT_ID):
        return
    last_run = {}
    while True:
        now = datetime.now() + BD_OFFSET  # Dhaka local time
        for name, t in TASKS.items():
            due = t["schedule"](now)
            if due and now.strftime("%H:%M") != last_run.get(name):
                last_run[name] = now.strftime("%H:%M")
                log(f"scheduled run: {name}")
                threading.Thread(
                    target=_run_task_safely, args=(name,), daemon=True
                ).start()
        time.sleep(60)


def _restore_state():
    """Load gist state and rebuild reminders + tasks created by message.
    Runs in a thread so a slow GitHub call never delays boot; without a
    GIST_TOKEN it's a no-op."""
    state.load()
    for item in state.STATE.get("reminders", []):
        try:
            REMINDERS.append(
                {"due": datetime.fromisoformat(item["due"]), "text": item["text"]}
            )
        except Exception:
            pass  # malformed entry — skip it
    for spec in state.STATE.get("dynamic_tasks", []):
        try:
            build_dynamic_task(spec)
        except Exception as e:
            print("[state] task rebuild failed:", e)
    if REMINDERS or state.STATE.get("dynamic_tasks"):
        log(
            f"state restored: {len(REMINDERS)} reminders, "
            f"{len(state.STATE.get('dynamic_tasks', []))} tasks"
        )
        state.save_soon()


# ---------------------------------------------------------------------------
# Reminders — natural language, checked every 10 seconds
#   "remind me in 5s ..."     "in 2h take food off the stove"
#   "remind me at 18:30 ..."  "tomorrow 9am ..."   (times are DHAKA time)
# ---------------------------------------------------------------------------

REMINDERS = []  # [{due: datetime (Dhaka wall clock), text: str}]

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def _fmt_delta(seconds):
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


def _parse_reminder(spec):
    """'in 5m buy eggs' / 'at 18:30 call' / 'tomorrow 9am X' →
    (due datetime in Dhaka time, text) or None if unparseable."""
    spec = spec.strip().strip(",").strip()
    m = re.match(r"^in\s+(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*(.*)$", spec, re.I)
    if m:
        unit = m.group(2).lower()
        if unit in _UNIT_SECONDS:
            due = datetime.now() + BD_OFFSET + timedelta(
                seconds=float(m.group(1)) * _UNIT_SECONDS[unit]
            )
            return due, (m.group(3).strip(" ,:").strip() or "(no text)")
    m = re.match(
        r"^(tomorrow\s+)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(.*)$",
        spec, re.I,
    )
    if m:
        tomorrow, hour, minute, ap, rest = m.groups()
        hour, minute = int(hour), int(minute or 0)
        rest = rest.strip(" ,:").strip()
        # avoid treating a bare number ("5") as 5am — need a real time signal
        if not (tomorrow or ap or ":" in spec or rest):
            return None
        if ap:
            ap = ap.lower()
            if ap == "pm" and hour < 12:
                hour += 12
            elif ap == "am" and hour == 12:
                hour = 0
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        now = datetime.now() + BD_OFFSET
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if tomorrow or due <= now:  # a passed time means the next day
            due += timedelta(days=1)
        return due, (rest or "(no text)")
    return None


REMIND_USAGE = (
    "Remind me how? 🙂 Try:\n"
    "• remind me in 5m to check the oven\n"
    "• in 2h call mom\n"
    "• remind me at 18:30 to pray\n"
    "• tomorrow 9am standup notes\n"
    "See pending ones with /reminders, cancel with /remind cancel 2"
)


def _sync_reminders_state():
    state.STATE["reminders"] = [
        {"due": r["due"].isoformat(), "text": r["text"]} for r in REMINDERS
    ]
    state.save_soon()


def add_reminder(spec, quiet=False):
    parsed = _parse_reminder(spec)
    if not parsed:
        return False
    due, rtext = parsed
    REMINDERS.append({"due": due, "text": rtext})
    _sync_reminders_state()
    log(f"reminder set: {rtext[:40]} @ {due:%H:%M:%S}")
    if not quiet:
        now = datetime.now() + BD_OFFSET
        tg_send(
            f"⏰ Ok — at {due:%H:%M} "
            f"(in {_fmt_delta((due - now).total_seconds())}):\n{rtext}"
        )
    return True


def reminder_loop():
    if not (TELEGRAM_BOT_TOKEN and OWNER_CHAT_ID):
        return
    while True:
        try:
            now = datetime.now() + BD_OFFSET
            for r in [r for r in REMINDERS if r["due"] <= now]:
                REMINDERS.remove(r)
                _sync_reminders_state()
                tg_send(f"⏰ Reminder: {r['text']}")
        except Exception as e:
            print("[reminders] loop error:", e)
        time.sleep(10)


# (reminder_loop is started with all other threads at the bottom of the file)


# ---------------------------------------------------------------------------
# Start every background loop LAST, after all definitions, so no thread can
# race a half-initialized module.
# ---------------------------------------------------------------------------

threading.Thread(target=_restore_state, daemon=True).start()
threading.Thread(target=telegram_loop, daemon=True).start()
threading.Thread(target=scheduler_loop, daemon=True).start()
threading.Thread(target=reminder_loop, daemon=True).start()
threading.Thread(target=state.saver_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Health endpoint — UptimeRobot pings this to keep the free service awake
# ---------------------------------------------------------------------------

@app.route("/")
@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
