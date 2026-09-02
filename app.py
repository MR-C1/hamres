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
import memory
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
# is down/overloaded/rate-limited. Entries may name another provider with
# a prefix: "groq:MODEL" or "gemini:MODEL" (keys below), e.g.
# LLM_FALLBACK_MODELS=groq:llama-3.3-70b-versatile,gemini:gemini-2.5-flash
LLM_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("LLM_FALLBACK_MODELS", "").split(",")
    if m.strip()
]
# Extra providers (free, card-free signups, OpenAI-compatible):
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")    # console.groq.com
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")  # aistudio.google.com

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

_llm_format = None  # cached auto-detected format (default provider only)

# Multi-provider: "groq:MODEL" / "gemini:MODEL" entries in the chain route
# to that provider's endpoint+key. Bare entries use the default provider.
PROVIDERS = {
    "groq": {
        "base": "https://api.groq.com/openai/v1",
        "key": GROQ_API_KEY,
        "limit": 1000,  # free plan, requests/day (docs.groq)
    },
    "gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key": GEMINI_API_KEY,
        "limit": 1500,  # approx free-tier requests/day
    },
}


def _resolve(model):
    """'groq:x' | 'gemini:x' | bare → (provider, base, key, bare_model)."""
    if ":" in model:
        p, m = model.split(":", 1)
        if p in PROVIDERS:
            return p, PROVIDERS[p]["base"], PROVIDERS[p]["key"], m
    return "default", _base_url(), LLM_API_KEY, model


def _base_url():
    return (LLM_BASE_URL or "https://openrouter.ai/api/v1").rstrip("/")


def _raise(resp):
    body = resp.text[:600]
    raise RuntimeError(f"HTTP {resp.status_code} from {_base_url()}: {body}")


def _openai_url(base=None):
    base = (base or _base_url()).rstrip("/")
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


def _llm_openai(messages, max_tokens, model, base=None, key=None):
    url = _openai_url(base)
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key or LLM_API_KEY}"},
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
    """Primary model first, then fallbacks, deduplicated. Entries whose
    provider has no key configured are skipped."""
    chain = [LLM_MODEL] + LLM_FALLBACK_MODELS
    seen, out = set(), []
    for m in chain:
        if not m or m in seen:
            continue
        seen.add(m)
        provider, _, key, _ = _resolve(m)
        if provider != "default" and not key:
            continue
        out.append(m)
    return out


DAILY_FREE_LIMIT = 50  # default provider (OpenRouter free tier)


def _today():
    return datetime.now().strftime("%Y-%m-%d")  # UTC day, matching OR's reset


def _quota_state():
    q = state.STATE.setdefault("quota", {"day": _today(), "used": {}})
    if q["day"] != _today():  # new day — reset counters
        q["day"] = _today()
        q["used"] = {}
    if isinstance(q["used"], int):  # migrate the old single-counter format
        q["used"] = {"default": q["used"]}
    return q


def _count_request(provider="default"):
    """Track LLM requests per provider per day (survives restarts)."""
    q = _quota_state()
    q["used"][provider] = q["used"].get(provider, 0) + 1
    state.save_soon()
    return q["used"][provider]


def _provider_limit(provider):
    if provider == "default":
        return DAILY_FREE_LIMIT
    return PROVIDERS.get(provider, {}).get("limit", "?")


def llm(messages, max_tokens=800):
    global _llm_format
    forced = os.environ.get("LLM_API_FORMAT", "").strip().lower()

    errors = []
    for model in model_chain():
        provider, base, key, bare = _resolve(model)

        if provider != "default":
            # groq/gemini: OpenAI-compatible, single attempt
            try:
                text = _llm_openai(messages, max_tokens, bare, base=base, key=key)
                _count_request(provider)
                if model != LLM_MODEL:
                    log(f"fallback used: {model} ({provider})")
                return text
            except Exception as e:
                errors.append(f"{model} [{provider}]: {str(e)[:200]}")
            continue

        # default provider: 1. explicit format, 2. cached, 3. probe both
        order = [f for f in _FORMATS if f[0] == forced] if forced else (
            [f for f in _FORMATS if f[0] == _llm_format] or list(_FORMATS)
        )
        for name, fn in order:
            try:
                text = fn(messages, max_tokens, model)
                _llm_format = name
                used = _count_request("default")
                if used == DAILY_FREE_LIMIT - 5:  # warn once near the cap
                    tg_send(f"⚠️ OpenRouter free budget almost gone: "
                            f"{used}/{DAILY_FREE_LIMIT} today.")
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
        f"default base_url: {_base_url()}",
        "providers: " + ", ".join(
            f"{p} {'✅' if PROVIDERS[p]['key'] else '— no key'}"
            for p in PROVIDERS
        ) + f", default {'✅' if LLM_API_KEY else '— no key'}",
        f"chain: {' → '.join(model_chain()) or '(empty)'}\n",
    ]

    test = [{"role": "user", "content": "Reply with the single word: ok"}]
    worked = []
    for model in model_chain():
        provider, base, key, bare = _resolve(model)
        if provider != "default":
            try:
                text = _llm_openai(test, 20, bare, base=base, key=key)
                lines.append(f"✅ {model} — replied: {text[:60]!r}")
                worked.append(model)
            except Exception as e:
                lines.append(f"❌ {model} — {str(e)[:300]}\n")
            continue
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
/deep <question> — deep research: search + read sources + cite (2 req)
/reminders — pending reminders
/remind <when> <text> — set one (in 5m / at 18:30 / tomorrow 9am)
/memories — everything I remember about you
/forget <what> — delete a memory (or 'forget everything')
/summarize <url> — read a link and summarize it
/expenses — expense log: "spent 120 on rickshaw", "how much did i spend"
/quota — LLM requests used today (per provider)
/report — today's activity summary (what ran, what failed)
/kill <name> — stop a task created by message
/diag — test the LLM connection, show any error

Or just talk, e.g.:
"remember my wifi password is xyz" → "what's my wifi password?"
"spent 120 on rickshaw" / "how much did I spend?"
"email someone@gmail.com saying I'll be late"
"remind me in 10m ..." / "alert me when bitcoin drops below 60000"
📷 Send a photo with a question. 🎙 Send a voice note — it becomes text.
"tell me ethereum's price every 30m" / "weather in 6 hours"
"watch @SamayRainaOfficial for new videos"
ANY other automation works too — the bot writes its own code:
"check the USD to BDT rate every hour and tell me if it moves"
Manage by chat: "stop the bitcoin alert", "make the weather
report every 10 minutes", "move my briefing to 8am"
Several requests in one message are fine too."""

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
Local time: {NOW} (Dhaka, UTC+6). The user lives in Dhaka, Bangladesh.
Reply with ONE JSON object only — no markdown fences, no commentary.

Task types you may create (fill params from the user's words — ALWAYS
prefer sensible defaults and create the task immediately over asking
clarifying questions):
- price alert: {"action":"task","type":"price_alert","params":{"coin":"<coingecko id, e.g. bitcoin, ethereum, solana>","below":<USD number or null>,"above":<USD number or null>,"every_minutes":30}}
- price report: {"action":"task","type":"price_report","params":{"coin":"bitcoin","every_minutes":60}} — recurring "tell me the price of X"
- one-time weather: {"action":"task","type":"weather_once","params":{"location":"Dhaka","when_spec":"in 6h"}} — weather report at a future time
- daily weather: {"action":"task","type":"weather_daily","params":{"location":"Dhaka","hour":8,"minute":0}}
- daily briefing: {"action":"task","type":"briefing","params":{"topic":"<topic>","hour":9,"minute":0}}
- page watch: {"action":"task","type":"watch_page","params":{"url":"https://...","every_minutes":60}}
- RSS feed watch: {"action":"task","type":"watch_rss","params":{"url":"<feed url>","every_minutes":30}} — new-entry alerts for any RSS/Atom feed
- YouTube watch: {"action":"task","type":"watch_youtube","params":{"url_or_handle":"<@handle or url>","every_minutes":15}} — new-video alerts
- Send an email: {"action":"email","to":"<address>","subject":"<subject>","text":"<message>"} — for "email X saying Y"
- CUSTOM SKILL — for ANY other recurring automation (the bot writes and
  tests its own Python code for it):
  {"action":"forge","goal":"<precise one-sentence description of what to check or do>","params":{<any settings it needs>},"schedule":{"every_minutes":<N>} or {"daily":{"hour":<H>,"minute":<M>}}}
  Use "forge" whenever the request is an automation that doesn't fit a
  template above (e.g. "check X every hour and tell me if Y",
  "daily report of Z from some website/API").
- Stop an automation: {"action":"stop","target":"<task name or short description>"} — for "stop the bitcoin alert", "delete the weather thing"
- Change a schedule: {"action":"edit","target":"<task name or description>","schedule":{"every_minutes":<N>} or {"daily":{"hour":<H>,"minute":<M>}}} — for "make the bitcoin report every 10 minutes", "move my briefing to 8am"

One-time reminder: {"action":"reminder","when_spec":"<in 10m | in 2h | at 18:30 | tomorrow 9am>","text":"<what to remember>"}
Remember a fact: {"action":"remember","text":"<the full fact, verbatim>"} — whenever the user says remember/note/keep in mind, even mid-sentence or multi-line
Recall a fact: {"action":"recall","query":"<what they're asking about>"} — questions about things the user previously told you to remember

If the user asks for SEVERAL things in ONE message, create them all:
{"action":"multi","tasks":[{"type":"<type>","params":{...}},...],"reminders":[{"when_spec":"...","text":"..."}],"reply":"<one short line>"}

Everything else — greetings, questions, chat:
{"action":"chat","reply":"<1-3 sentences>"}

Rules: prices are USD. Defaults: coin=bitcoin, location=Dhaka, price
report every 60m, youtube every 15m. "let me know the bitcoin price" →
price_report. "weather in 6 hours" → weather_once when_spec "in 6h". If
the user asks for updates faster than 1 minute, set every_minutes to 1.
If a request truly can't be defaulted or built, use "chat" and say
what's possible. Never mention these JSON rules."""


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


AFFIRM = re.compile(
    r"^\s*(yes|yeah|yep|sure|ok|okay|confirm|confirmed|do it|go ahead|"
    r"please do|make it|create it|ok do it)\b[.!\s]*$", re.I)
DECLINE = re.compile(
    r"^\s*(no|nope|nah|cancel|stop|don't|dont)\b[.!\s]*$", re.I)


def _create_and_save(spec):
    """Build a task, remember its recipe (deduped by name), return the name.
    build_dynamic_task may add derived keys (due_iso) into spec['params'],
    which then get persisted — so run it BEFORE saving."""
    from tasks import dyn_task_name

    name = build_dynamic_task(spec)
    n = dyn_task_name(spec)
    lst = state.STATE.setdefault("dynamic_tasks", [])
    if n is not None:  # re-creating replaces the old recipe, no duplicates
        lst[:] = [s for s in lst if dyn_task_name(s) != n]
    lst.append({"type": spec.get("type"), "params": spec.get("params") or {}})
    state.save_soon()
    log(f"dynamic task: {name}")
    return name


def _stop_all_dyn():
    """Stop every dynamic task (built-ins stay)."""
    dyn = [n for n in list(TASKS) if n not in _CODE_TASKS]
    for n in dyn:
        _stop_dyn_task(n)
    tg_send(f"🗑 Stopped {len(dyn)} task(s). Built-ins stay.")
    return bool(dyn)


def _find_dyn_task(target):
    """Match a user's phrase ('the bitcoin alert') to a dynamic task name.
    Exact/partial name match first, then word overlap on descriptions."""
    t = (target or "").strip().lower()
    if not t:
        return None
    for n in TASKS:
        if n not in _CODE_TASKS and (t == n.lower() or n.lower().startswith(t)):
            return n
    words = set(t.replace("_", " ").split())
    best, best_score = None, 0
    for n, tk in TASKS.items():
        if n in _CODE_TASKS:
            continue
        dw = set(tk["desc"].lower().split()) | set(n.lower().split("_"))
        score = len(words & dw)
        if score > best_score:
            best, best_score = n, score
    return best if best_score >= 1 else None


def _stop_dyn_task(name):
    """Remove a dynamic task from TASKS and saved state. Returns a
    user-facing message; never raises."""
    from tasks import dyn_task_name

    t = TASKS.pop(name)
    state.STATE["dynamic_tasks"] = [
        s for s in state.STATE.get("dynamic_tasks", [])
        if dyn_task_name(s) != name
    ]
    state.STATE.get("skill_memory", {}).pop(name, None)
    state.save_soon()
    log(f"task killed: {name}")
    return f"🗑 Stopped: {t['desc']}"


def _apply_schedule_to_spec(spec, sched):
    """Set a new schedule on a dynamic task spec (in place)."""
    params = spec.setdefault("params", {})
    ttype = spec.get("type")
    if ttype == "weather_once":
        raise ValueError("one-time tasks run at a fixed time — "
                         "stop it and create a new one")
    if sched.get("daily"):
        h = int(sched["daily"].get("hour", 8))
        m = int(sched["daily"].get("minute", 0))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("hour/minute out of range")
        if ttype == "skill":
            params["schedule"] = {"daily": {"hour": h, "minute": m}}
        else:
            params["hour"] = h
            params["minute"] = m
    elif sched.get("every_minutes"):
        n = max(1, int(sched["every_minutes"]))
        if ttype == "skill":
            params["schedule"] = {"every_minutes": n}
        else:
            params["every_minutes"] = n
    else:
        raise ValueError("schedule must be every_minutes or daily")


# ---------------------------------------------------------------------------
# Link reader — paste a URL, get a clean summary (1 LLM request per link)
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+")

# words that mean the URL is part of a task-creation request, not a
# "read this for me" request
_TASK_WORDS = re.compile(
    r"\b(watch|monitor|alert|every|remind|briefing|daily|check|"
    r"minute|hour|video|subscribe|follow)\b", re.I)


def _extract_text(html):
    """HTML → readable-ish text, stdlib only."""
    from html.parser import HTMLParser

    class _T(HTMLParser):
        SKIP = {"script", "style", "noscript", "head", "svg"}

        def __init__(self):
            super().__init__()
            self.parts = []
            self._skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in self.SKIP:
                self._skip += 1

        def handle_endtag(self, tag):
            if tag in self.SKIP and self._skip:
                self._skip -= 1

        def handle_data(self, data):
            if not self._skip and data.strip():
                self.parts.append(data.strip())

    p = _T()
    p.feed(html)
    return " ".join(p.parts)


# ---------------------------------------------------------------------------
# Deep research — /deep <question>: search → pick best sources → read them
# → synthesize with citations. 2 LLM requests, opt-in.
# ---------------------------------------------------------------------------

def _deep_research(question):
    if not question:
        return "Ask me something: /deep <question>"
    tg_send(f"🔬 Deep research: {question}\n(searching + reading sources…)")
    try:
        results = web_search(question, max_results=6)
    except Exception as e:
        return f"Search failed: {e}"
    if not results:
        return "No search results came back — try rephrasing?"

    # 1st LLM call: pick the 3 most promising URLs
    listed = "\n".join(f"{i+1}. {r['title']} — {r['href']}"
                       for i, r in enumerate(results))
    try:
        pick = llm(
            [
                {"role": "system",
                 "content": "Pick the 3 URLs most likely to answer the "
                            "question. Reply ONLY a JSON array of index "
                            "numbers, e.g. [1,3,5]."},
                {"role": "user", "content": f"Question: {question}\n\n{listed}"},
            ],
            max_tokens=60,
        )
        idxs = [i - 1 for i in json.loads(pick.strip())
                if isinstance(i, int) and 1 <= i <= len(results)][:3]
    except Exception:
        idxs = [0, 1, 2]

    # read the picked pages (free — no LLM)
    pages = []
    for i in idxs:
        try:
            r = requests.get(results[i]["href"], timeout=20, stream=True,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            chunks, size = [], 0
            for chunk in r.iter_content(chunk_size=64 * 1024):
                chunks.append(chunk)
                size += len(chunk)
                if size >= 600 * 1024:
                    break
            page = _extract_text(
                b"".join(chunks).decode("utf-8", "ignore"))[:2500]
            if len(page) > 150:
                pages.append({"url": results[i]["href"], "text": page})
        except Exception:
            pass

    context = "Search results:\n" + "\n\n".join(
        f"[{i+1}] {r['title']}\n{r['href']}\n{r['body']}"
        for i, r in enumerate(results))
    if pages:
        context += "\n\nRead pages:\n" + "\n\n".join(
            f"[P{i+1}] {p['url']}\n{p['text']}" for i, p in enumerate(pages))

    # 2nd LLM call: synthesize
    answer = llm(
        [
            {"role": "system",
             "content": "Research assistant. Answer the question using the "
                        "search results and read pages. Cite sources as [1] "
                        "or [P1]. Be concrete and complete but concise. If "
                        "sources disagree or lack the answer, say so."},
            {"role": "user",
             "content": f"Question: {question}\n\n{context[:15000]}"},
        ],
        max_tokens=900,
    )
    srcs = "\n".join(f"[{i+1}] {r['href']}" for i, r in enumerate(results))
    psrcs = "\n".join(f"[P{i+1}] {p['url']}" for i, p in enumerate(pages))
    return f"{answer}\n\nSources:\n{srcs}" + (f"\n{psrcs}" if psrcs else "")


# ---------------------------------------------------------------------------
# Email — Gmail app password (SMTP_* env vars). Free, no card.
# ---------------------------------------------------------------------------

def _send_email(to, subject, body):
    """Returns None on success, or an error/config message."""
    user = os.environ.get("SMTP_USER", "")
    pw = os.environ.get("SMTP_PASS", "")
    if not (user and pw):
        return ("Email isn't configured. On Render set SMTP_USER (your "
                "Gmail) and SMTP_PASS (an app password — Google account → "
                "Security → 2-Step Verification → App passwords).")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    import smtplib
    from email.mime.text import MIMEText

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = os.environ.get("EMAIL_FROM", user)
        msg["To"] = to
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        return None
    except Exception as e:
        return f"Send failed: {e}"


# ---------------------------------------------------------------------------
# Expenses — free, rule-based. "spent 120 on rickshaw" / "how much did i spend"
# ---------------------------------------------------------------------------

def _expense_list():
    exps = state.STATE.get("expenses", [])
    if not exps:
        return "No expenses logged. Say: spent <amount> on <thing>"
    now = datetime.now() + BD_OFFSET
    today = f"{now:%Y-%m-%d}"
    d = sum(e["amount"] for e in exps if str(e["at"])[:10] == today)
    m = sum(e["amount"] for e in exps if str(e["at"])[:7] == today[:7])
    lines = [f"💸 Today: {d:,.0f} tk | this month: {m:,.0f} tk",
             "Recent:"]
    lines += [f"• {e['amount']:,.0f} tk — {e['what']} ({str(e['at'])[5:10]})"
              for e in exps[-10:]]
    return "\n".join(lines)


def _summarize_url(url):
    if not url.startswith("http"):
        tg_send("Send a link that starts with http(s)://")
        return
    tg_send(f"🔎 Reading {url}…")
    try:
        r = requests.get(
            url, timeout=20, headers={"User-Agent": "Mozilla/5.0"},
            stream=True,
        )
        r.raise_for_status()
        chunks, size = [], 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size >= 900 * 1024:  # ~1MB is plenty for text
                break
        html = b"".join(chunks).decode("utf-8", "ignore")
    except Exception as e:
        tg_send(f"Couldn't fetch that page: {e}")
        return

    page = _extract_text(html)[:6000]
    if len(page) < 200:
        tg_send("That page has barely any text (a login wall or a pure "
                "app page?) — nothing to summarize.")
        return
    try:
        summary = llm(
            [
                {"role": "system",
                 "content": "Summarize this web page for the user in 5-10 "
                 "short bullet points. Lead with what the page is. "
                 "Bangla/Banglish pages: answer in the same style."},
                {"role": "user", "content": f"URL: {url}\n\n{page}"},
            ],
            max_tokens=500,
        )
        tg_send(f"📄 {summary}")
    except Exception as e:
        tg_send(f"Read the page but summarizing failed: {e}")


# ---------------------------------------------------------------------------
# Chat history — the last few exchanges, so follow-up messages have context.
# Stored in gist state; injected into the prompt. Still 1 request per message.
# ---------------------------------------------------------------------------

def _history_msgs(limit=6):
    """Recent turns as LLM messages."""
    h = state.STATE.get("chat_history", [])[-limit:]
    return [{"role": m["role"], "content": str(m["content"])[:400]} for m in h]


def _record_chat(user_text, bot_reply):
    h = state.STATE.setdefault("chat_history", [])
    h.append({"role": "user", "content": (user_text or "")[:800],
              "at": (datetime.now() + BD_OFFSET).isoformat()})
    h.append({"role": "assistant", "content": (bot_reply or "")[:800]})
    del h[:-12]  # keep the last 6 exchanges
    state.save_soon()


def handle_plain_text(text):
    """Plain messages: pending confirmation first (free), then ONE LLM call
    that decides: create task(s), set a reminder, or just chat."""
    # -- pending confirmation from a previous question: "yes" builds it --
    pending = state.STATE.get("pending_task")
    if pending is not None:
        state.STATE.pop("pending_task", None)
        state.save_soon()
        if AFFIRM.match(text):
            try:
                name = _create_and_save(pending)
                tg_send(
                    f"✅ Task created: {TASKS[name]['desc']}\n"
                    f"Test it now with /run {name}"
                )
            except Exception as e:
                tg_send(f"Couldn't build that task: {e}")
            return
        if DECLINE.match(text):
            tg_send("Okay, cancelled. 🙂")
            return
        # any other text: forget the question, classify the new message

    # -- link reader: a message that's basically just a URL --
    m = _URL_RE.search(text)
    if m and not _TASK_WORDS.search(text):
        _summarize_url(m.group(0))
        return

    now = datetime.now() + BD_OFFSET
    # -- memory injection: relevant stored facts join the prompt. Costs 0
    # extra requests (quota counts requests, not prompt length). --
    sys_prompt = INTENT_SYSTEM.replace("{NOW}", f"{now:%Y-%m-%d %H:%M}")
    mem_block = memory.inject_for(text)
    if mem_block:
        sys_prompt += "\n\n" + mem_block
    raw = llm(
        [{"role": "system", "content": sys_prompt}]
        + _history_msgs()
        + [{"role": "user", "content": text}],
        max_tokens=500,
    )
    data = _extract_json(raw)

    if not data:  # model didn't produce JSON — treat its text as a chat reply
        tg_send(raw or "…")
        return

    action = data.get("action")

    if action == "task":
        try:
            name = _create_and_save(data)
            tg_send(
                f"✅ Task created: {TASKS[name]['desc']}\n"
                f"Saved — it survives restarts. Test it now with /run {name}"
            )
        except Exception as e:
            tg_send(f"Couldn't build that task: {e}")
    elif action == "multi":
        made, reminders, failed = [], [], []
        for spec in data.get("tasks") or []:
            try:
                name = _create_and_save(spec)
                made.append(TASKS[name]["desc"])
            except Exception as e:
                failed.append(f"{spec.get('type')}: {e}")
        for rem in data.get("reminders") or []:
            spec = f"{(rem.get('when_spec') or '').strip()} " \
                   f"{(rem.get('text') or '').strip()}".strip()
            if add_reminder(spec):
                reminders.append(spec)
            else:
                failed.append(f"reminder '{spec}' didn't parse")
        lines = []
        if data.get("reply"):
            lines.append(str(data["reply"]))
        if made:
            lines.append("✅ Created:")
            lines += [f"• {d}" for d in made]
        if reminders:
            lines.append("⏰ Reminders set: " + "; ".join(reminders))
        if failed:
            lines.append("⚠️ Failed:")
            lines += [f"• {f}" for f in failed]
        if not (made or reminders or failed):
            lines.append("(nothing to create)")
        tg_send("\n".join(lines))
    elif action == "confirm":
        spec = data.get("spec")
        if isinstance(spec, dict) and spec.get("type"):
            state.STATE["pending_task"] = spec
            state.save_soon()
            tg_send(f"{data.get('question') or 'Create it?'}\n(yes / no)")
        else:
            tg_send(data.get("reply") or raw or "…")
    if action == "forge":
        import forge

        spec = forge.forge_skill(
            goal=str(data.get("goal") or text).strip(),
            user_params=data.get("params") or {},
            schedule=data.get("schedule") or {},
            llm=llm,
            report=tg_send,
        )
        if spec:
            try:
                name = _create_and_save(spec)
                tg_send(
                    f"✅ Skill saved: {TASKS[name]['desc']}\n"
                    f"Test it now with /run {name} — runs on its own from "
                    f"here on. Kill it with /kill {name}"
                )
            except Exception as e:
                tg_send(f"Skill passed testing but saving failed: {e}")
    elif action == "stop":
        target = (data.get("target") or "").strip().lower()
        if target in ("all", "everything", "all tasks", "all automations"):
            _stop_all_dyn()
        else:
            name = _find_dyn_task(target)
            if name:
                tg_send(_stop_dyn_task(name))
            else:
                direct = target.replace(" ", "_")
                code_hit = next(
                    (n for n in TASKS if n in _CODE_TASKS and direct and direct in n),
                    None,
                )
                if code_hit:
                    tg_send(f"'{code_hit}' is a built-in task — those stay.")
                else:
                    tg_send(f"Couldn't find an automation like "
                            f"'{data.get('target')}'. Send /tasks to see them.")
    elif action == "edit":
        name = _find_dyn_task(data.get("target"))
        if not name:
            tg_send(f"Couldn't find an automation like "
                    f"'{data.get('target')}'. Send /tasks to see them.")
            return
        from tasks import dyn_task_name

        spec = next(
            (s for s in state.STATE.get("dynamic_tasks", [])
             if dyn_task_name(s) == name),
            None,
        )
        if spec is None:
            tg_send("That task can't be rescheduled.")
            return
        try:
            _apply_schedule_to_spec(spec, data.get("schedule") or {})
            # remove the old entry everywhere (the name itself may change,
            # e.g. briefing_0900 → briefing_0800), then re-create it
            TASKS.pop(name, None)
            state.STATE["dynamic_tasks"] = [
                s for s in state.STATE.get("dynamic_tasks", [])
                if dyn_task_name(s) != name
            ]
            state.save_soon()
            new_name = _create_and_save(spec)
            tg_send(f"🔁 Updated: {TASKS[new_name]['desc']}")
        except Exception as e:
            tg_send(f"Couldn't change that: {e}")
    elif action == "email":
        to = str(data.get("to") or "").strip()
        subject = str(data.get("subject") or "From your Hermes agent").strip()
        body = str(data.get("text") or data.get("body") or "").strip()
        if not (to and body):
            tg_send("Email needs a recipient and a message.")
        else:
            err = _send_email(to, subject, body)
            tg_send(err or f"📧 Sent to {to}")
    elif action == "remember":
        fact = str(data.get("text") or "").strip()
        tg_send(memory.remember(fact) if fact else "Remember what? 🙂")
    elif action == "recall":
        tg_send(memory.recall(str(data.get("query") or text)))
    elif action == "reminder":
        spec = f"{data.get('when_spec', '')} {data.get('text', '')}".strip()
        if not add_reminder(spec):
            tg_send(REMIND_USAGE)
    else:  # chat
        reply = data.get("reply") or raw or "…"
        tg_send(reply)
        _record_chat(text, reply)
        for f in memory.auto_extract(text):
            log(f"auto-memory: {f[:60]}")


# ---------------------------------------------------------------------------
# Vision + voice — photos understood by a multimodal model; voice notes
# transcribed by Groq's free Whisper, then handled as text.
# ---------------------------------------------------------------------------

def _tg_download(file_id):
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=15)
    r.raise_for_status()
    fp = r.json()["result"]["file_path"]
    r2 = requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{fp}", timeout=60
    )
    r2.raise_for_status()
    return r2.content, fp


def _handle_photo(msg):
    import base64

    photo = msg["photo"][-1]  # largest size
    caption = (msg.get("caption") or
               "What's in this image? Describe it briefly.").strip()
    try:
        data, _ = _tg_download(photo["file_id"])
    except Exception as e:
        tg_send(f"Couldn't download the photo: {e}")
        return
    b64 = base64.b64encode(data).decode()

    vmodel = os.environ.get("VISION_MODEL", "").strip()
    if not vmodel:
        vmodel = ("gemini:gemini-2.5-flash" if GEMINI_API_KEY
                  else "thinkingmachines/inkling:free")
    try:
        reply = llm(
            [
                {"role": "system",
                 "content": "You are Hermes. Answer about the user's image "
                            "concisely (1-4 sentences). Match their language."},
                {"role": "user", "content": [
                    {"type": "text", "text": caption},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]},
            ],
            max_tokens=400,
        )
        tg_send(f"📷 {reply}")
        _record_chat(caption, reply)
    except Exception as e:
        tg_send(f"Vision failed: {e}")


def _handle_voice(msg):
    v = msg.get("voice") or msg.get("audio")
    if not GROQ_API_KEY:
        tg_send("Voice needs a GROQ_API_KEY (free at console.groq.com) — "
                "add it on Render and I'll understand voice notes.")
        return
    try:
        data, fname = _tg_download(v["file_id"])
    except Exception as e:
        tg_send(f"Couldn't download the voice note: {e}")
        return
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (fname.split("/")[-1] or "voice.ogg", data,
                            "audio/ogg")},
            data={"model": "whisper-large-v3-turbo"},
            timeout=90,
        )
        r.raise_for_status()
        _count_request("groq")
        text = r.json()["text"].strip()
    except Exception as e:
        tg_send(f"Transcription failed: {e}")
        return
    if not text:
        tg_send("(couldn't hear anything in that note?)")
        return
    log(f"voice: {text[:60]}")
    tg_send(f"🎙 You said: {text}")
    dispatch(text)  # act on it exactly like a typed message


def handle_message(msg):
    if not is_owner(msg):
        if msg.get("text") or msg.get("photo") or msg.get("voice"):
            tg_send(UNAUTHORIZED, msg["chat"]["id"])
        return
    if msg.get("photo"):
        _handle_photo(msg)
        return
    if msg.get("voice") or msg.get("audio"):
        _handle_voice(msg)
        return

    text = (msg.get("text") or "").strip()
    if not text:
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
        nmem = len(state.STATE.get("memories", []))
        tg_send(
            f"Up {up // 3600}h {(up % 3600) // 60}m, {mem}MB RAM, "
            f"state: {mode}, {nmem} memories\n"
            + "\n".join(LOG[-10:])
        )
    elif text.startswith("/run "):
        name = text[5:].strip().split()[0]
        if name not in TASKS:
            tg_send(f"No task '{name}'. Use /tasks.")
            return
        tg_send(f"Running {name}...")
        _run_task_safely(name, manual=True)  # result delivered by the task
    elif text.startswith("/ask "):
        tg_send(do_research(text[5:].strip()))
    elif text.startswith("/say "):
        mem_block = memory.inject_for(text[5:])
        sys_p = ("You are Hermes, the user's personal Telegram agent in "
                 "Dhaka. Reply in 1-3 short sentences, warm and direct, "
                 "matching the user's language.")
        if mem_block:
            sys_p += "\n\n" + mem_block
        reply = llm(
            [{"role": "system", "content": sys_p}]
            + _history_msgs()
            + [{"role": "user", "content": text[5:]}]
        )
        tg_send(reply)
        _record_chat(text[5:], reply)
    elif text == "/quota":
        q = _quota_state()
        used = q.get("used", {})
        lines = ["LLM requests today (reset 6am Dhaka):"]
        lines.append(f"• openrouter: {used.get('default', 0)}/{DAILY_FREE_LIMIT}")
        for pname, p in PROVIDERS.items():
            if p["key"]:
                lines.append(f"• {pname}: {used.get(pname, 0)}/{p['limit']}")
        lines.append("Reminders and free tasks (price/page/prayer watches) "
                     "don't count.")
        tg_send("\n".join(lines))
    elif text.startswith("/kill "):
        name = text[6:].strip()
        if name.lower() in ("all", "everything"):
            _stop_all_dyn()
        elif name not in TASKS:
            tg_send(f"No task '{name}'. Use /tasks.")
        elif name in _CODE_TASKS:  # written in tasks.py — remove from there
            tg_send(f"'{name}' is a built-in task — those stay.")
        else:
            tg_send(_stop_dyn_task(name))
    elif text == "/report":
        _run_task_safely("daily_report")  # sends the summary itself
    elif text == "/memories":
        tg_send(memory.recent(15))
    elif text.startswith("/forget "):
        tg_send(memory.forget(text[8:].strip()))
    elif text.startswith("/deep "):
        tg_send(_deep_research(text[6:].strip()))
    elif text.startswith("/summarize "):
        _summarize_url(text[11:].strip())
    elif text == "/expenses":
        tg_send(_expense_list())
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
        # Plain text: the human way. "remember …" stores a fact, "remind
        # me in 5m …" sets a reminder — both free, no LLM. Everything
        # else goes to the intent router (or the link reader).
        low = text.lower()

        m = re.match(r"^remember\s+(?:that\s+)?(.+)$", low, re.S)
        if m:
            # re.S so multi-line "remember this;\n\nthese are…" stores
            # EVERYTHING, not just the first line
            tg_send(memory.remember(text[m.start(1):]))  # original case
            return

        m = (re.match(r"^what's?\s+my\s+(.+)$", low)
             or re.match(r"^what\s+do\s+you\s+remember(?:\s+about\s+(.+))?$", low)
             or re.match(r"^what\s+did\s+i\s+(?:tell\s+you|say)\s+about\s+(.+)$", low)
             or re.match(r"^do\s+you\s+remember\s+(.+)$", low)
             or re.match(r"^what\s+was\s+the\s+(.+?)\s+(?:i\s+|we\s+)?"
                         r"(?:talked|discussed|said|mentioned)\s+about$", low))
        if m:
            query = m.group(1) or text
            tg_send(memory.recall(query))
            return

        m = re.match(r"^forget\s+(.+)$", low)
        if m:
            tg_send(memory.forget(m.group(1)))
            return

        # expenses: "spent 120 on rickshaw" — free, no LLM
        m = re.match(r"^(?:i\s+)?(?:spent|paid)\s+(\d+(?:\.\d+)?)\s*"
                     r"(?:taka|tk|bdt|৳)?\s*(?:on|for|in)?\s+(.+)$", low)
        if m and len(m.group(2)) > 1:
            amount = float(m.group(1))
            what = " ".join(text[m.start(2):].split())
            state.STATE.setdefault("expenses", []).append({
                "amount": amount, "what": what,
                "at": (datetime.now() + BD_OFFSET).isoformat(),
            })
            state.save_soon()
            now = datetime.now() + BD_OFFSET
            day = sum(e["amount"] for e in state.STATE["expenses"]
                      if str(e["at"])[:10] == f"{now:%Y-%m-%d}")
            tg_send(f"💸 Logged {amount:,.0f} tk — {what}. "
                    f"Today: {day:,.0f} tk")
            return
        if re.match(r"^how much (?:did i|have i|do i) spend", low):
            now = datetime.now() + BD_OFFSET
            exps = state.STATE.get("expenses", [])
            d = sum(e["amount"] for e in exps if str(e["at"])[:10] == f"{now:%Y-%m-%d}")
            mo = sum(e["amount"] for e in exps if str(e["at"])[:7] == f"{now:%Y-%m}")
            tg_send(f"💸 Today: {d:,.0f} tk | this month: {mo:,.0f} tk "
                    f"(/expenses for the list)")
            return

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


_FAIL_STREAKS = {}  # task name -> consecutive failures (quiet after the first)


def _run_task_safely(name, manual=False):
    ok, err = True, ""
    try:
        run_task(name)
    except Exception as e:
        ok, err = False, f"{type(e).__name__}: {e}"
        import traceback

        traceback.print_exc()
        streak = _FAIL_STREAKS.get(name, 0) + 1
        _FAIL_STREAKS[name] = streak
        if streak == 1:
            # first failure of a streak: tell the owner once, then go
            # quiet — the daily report still lists every failure
            tg_send(
                f"⚠️ Task '{name}' failed: {e}\n"
                f"It keeps retrying on schedule — I'll only message again "
                f"when it recovers. /kill {name} to stop it."
            )
    else:
        had = _FAIL_STREAKS.pop(name, 0)
        if had:
            tg_send(f"✅ Task '{name}' recovered after {had} failed run(s).")
    finally:
        # run history for the daily report — capped, gist-persisted.
        # "quiet" tasks (every-minute checks) don't clutter the history.
        if not TASKS.get(name, {}).get("quiet"):
            runs = state.STATE.setdefault("runs", [])
            runs.append({
                "task": name,
                "at": (datetime.now() + BD_OFFSET).isoformat(),
                "ok": ok,
                "err": err[:200],
                "manual": manual,
            })
            del runs[:-300]
            state.save_soon()


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
