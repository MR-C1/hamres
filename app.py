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

import os
import threading
import time
from datetime import datetime

import psutil
import requests
from flask import Flask

from tasks import TASKS
from runner import run_task

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
        ok_any = False
        for name, fn in _FORMATS:
            try:
                text = fn(test, 20, model)
                lines.append(f"✅ {model} ({name}) — replied: {text[:60]!r}")
                ok_any = True
                break
            except Exception as e:
                pass  # try next format; report below if all fail
        if not ok_any:
            # capture the error from the LAST format attempted
            last = ""
            for name, fn in _FORMATS:
                try:
                    fn(test, 20, model)
                except Exception as e:
                    last = f"{name}: {str(e)[:300]}"
            lines.append(f"❌ {model} — {last}\n")
        else:
            worked.append(model)

    if _llm_format is None and worked:
        # remember the format of the first working model
        _llm_format = "openai"  # corrected on next successful llm() call
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
/diag — test the LLM connection, show any error"""

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
        tg_send(
            f"Up {up // 3600}h {(up % 3600) // 60}m, {mem}MB RAM\n"
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


def scheduler_loop():
    """Every minute, run any task whose schedule says it's due."""
    if not (TELEGRAM_BOT_TOKEN and OWNER_CHAT_ID):
        return
    last_run = {}
    while True:
        now = datetime.now()
        for name, t in TASKS.items():
            due = t["schedule"](now)
            if due and now.strftime("%H:%M") != last_run.get(name):
                last_run[name] = now.strftime("%H:%M")
                log(f"scheduled run: {name}")
                try:
                    run_task(name)
                except Exception as e:
                    tg_send(f"Task '{name}' failed: {e}")
        time.sleep(60)


threading.Thread(target=telegram_loop, daemon=True).start()
threading.Thread(target=scheduler_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Health endpoint — UptimeRobot pings this to keep the free service awake
# ---------------------------------------------------------------------------

@app.route("/")
@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
