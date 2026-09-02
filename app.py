"""Hermes — a personal 24/7 agent (free stack: Render + Telegram +
OpenRouter/Groq/Gemini + a GitHub gist for state).

This is the composition root: web server, Telegram loop, scheduler,
slash commands, photo/voice handling, and boot. All intelligence lives
in router.py; all tasks in tasks.py; nothing else knows about Telegram.
"""

import threading
import time
from datetime import datetime

import psutil
import requests
from flask import Flask

import comms
import config
import llm
import memory
import reminders
import research
import router
import state
import tasks
from runner import run_task

app = Flask(__name__)
STARTED_AT = time.time()
UNAUTHORIZED = "Not authorized. This agent answers its owner only."

TG_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

HELP_HTML = """🧭 <b>Hermes</b> — your agent

<b>Just talk — I understand:</b>
• "remember my wifi password is xyz" → later "what's my wifi password?"
• "remind me in 10m …" • "spent 120 on rickshaw"
• "alert me when bitcoin drops below 60000"
• "tell me ethereum's price every 30m" • "weather in 6 hours"
• "watch @channel for new videos" • "email x@gmail.com saying I'll be late"
• 📷 send a photo with a question • 🎙 send a voice note
• 🔗 paste a link → I read and summarize it
• anything else → I write my own code for it
• manage: "stop the bitcoin alert" • "make it every 10m"

<b>Commands</b>
/tasks — automations • /run &lt;name&gt; — run one now
/ask — quick research • /deep — deep research with sources
/say — chat • /memories — what I remember • /forget &lt;what&gt;
/remind &lt;when&gt; &lt;text&gt; • /reminders — pending
/expenses — money log • /quota — LLM usage
/report — today's activity • /status — health
/kill &lt;name&gt; — stop (built-ins pause) • /enable — resume
/summarize &lt;url&gt; • /diag — LLM connection test"""


# ---------------------------------------------------------------------------
# Task execution with failure streaks + run history
# ---------------------------------------------------------------------------

FAIL_STREAKS = {}  # task → consecutive failures (quiet after the first)


def run_task_safely(name, manual=False):
    ok, err = True, ""
    try:
        run_task(name)
    except Exception as e:
        ok, err = False, f"{type(e).__name__}: {e}"
        import traceback
        traceback.print_exc()
        streak = FAIL_STREAKS.get(name, 0) + 1
        FAIL_STREAKS[name] = streak
        if streak == 1:
            # first failure of a streak: tell the owner once, then quiet
            comms.send(
                f"⚠️ <b>Task failed</b> — <code>{comms.esc(name)}</code>\n"
                f"{comms.esc(e)}\n"
                f"It keeps retrying on schedule — I'll only message again "
                f"when it recovers. Stop it: <code>/kill {comms.esc(name)}</code>",
                html=True)
    else:
        had = FAIL_STREAKS.pop(name, 0)
        if had:
            comms.send(f"✅ <b>Recovered</b> — {comms.esc(name)} "
                       f"after {had} failed run(s).", html=True)
    finally:
        # run history for the daily report — capped, gist-persisted.
        # "quiet" tasks (every-minute checks) don't clutter the history.
        if not tasks.TASKS.get(name, {}).get("quiet"):
            runs = state.STATE.setdefault("runs", [])
            runs.append({
                "task": name,
                "at": (datetime.now() + config.BD_OFFSET).isoformat(),
                "ok": ok,
                "err": err[:200],
                "manual": manual,
            })
            del runs[:-300]
            state.save_soon()


# ---------------------------------------------------------------------------
# Slash commands — table-driven
# ---------------------------------------------------------------------------

def cmd_help(_):
    comms.send(HELP_HTML, html=True)


def cmd_tasks(_):
    paused = tasks.paused_names()
    comms.send("\n".join(
        f"{'⏸ ' if n in paused else ''}{n} — {comms.esc(t['desc'])}"
        for n, t in tasks.TASKS.items()), html=True)


def cmd_run(args):
    name = args.split()[0] if args else ""
    if name not in tasks.TASKS:
        comms.send(f"No task '{comms.esc(name)}'. Use /tasks.", html=True)
        return
    comms.send(f"Running {comms.esc(name)}…", html=True)
    run_task_safely(name, manual=True)


def cmd_ask(args):
    comms.typing()
    comms.send_md(research.ask(args))


def cmd_deep(args):
    # background thread: deep research takes ~30s and must not block
    # other commands in the Telegram loop
    threading.Thread(target=research.deep_research, args=(args,),
                     daemon=True).start()


def cmd_say(args):
    router.chat_reply(args)


def cmd_status(_):
    up = int(time.time() - STARTED_AT)
    mem = psutil.Process().memory_info().rss // (1024 * 1024)
    mode = "gist-backed" if state.GIST_TOKEN else "memory-only"
    comms.send(
        f"Up {up // 3600}h {(up % 3600) // 60}m • {mem}MB RAM • "
        f"state: {mode} • {len(state.STATE.get('memories', []))} memories\n"
        + "\n".join(comms.LOG[-10:]), html=True)


def cmd_quota(_):
    q = llm.quota_state()
    used = q.get("used", {})
    lines = ["<b>LLM requests today</b> (reset 6am Dhaka):"]
    lines.append(f"• openrouter: {used.get('default', 0)}/50")
    for pname, p in config.PROVIDERS.items():
        if p["key"]:
            lines.append(f"• {pname}: {used.get(pname, 0)}/{p['limit']}")
    lines.append("Reminders and free tasks don't count.")
    comms.send("\n".join(lines), html=True)


def cmd_report(_):
    run_task_safely("daily_report", manual=True)  # sends itself


def cmd_reminders(_):
    comms.send(reminders.list_pending())


def cmd_remind(args):
    if not reminders.add(args):
        comms.send(reminders.USAGE)


def cmd_memories(_):
    comms.send(memory.recent(15))


def cmd_forget(args):
    comms.send(memory.forget(args or "everything")
               if args else "Forget what? e.g. /forget <words> "
               "or 'forget everything' in chat.")


def cmd_kill(args):
    if not args:
        comms.send("Kill what? e.g. /kill report_bitcoin • /kill all")
        return
    if args.strip().lower() in ("all", "everything"):
        comms.send(tasks.stop_all(), html=True)
        return
    name = args.strip()
    if name not in tasks.TASKS:
        comms.send(f"No task '{comms.esc(name)}'. Use /tasks.")
    else:
        comms.send(tasks.stop(name), html=True)


def cmd_enable(args):
    name = tasks.find(args)
    comms.send(tasks.resume(name) if name
               else "Couldn't find that task — send /tasks", html=True)


def cmd_summarize(args):
    research.summarize_url(args)


def cmd_expenses(_):
    comms.send(memory.expense_list())


def cmd_diag(_):
    comms.typing()
    comms.send(llm.diagnose())


def cmd_verify(_):
    # background thread: the battery takes ~1 min and must not block
    # other commands in the Telegram loop
    import verify
    threading.Thread(target=verify.run, daemon=True).start()


COMMANDS = {
    "help": cmd_help, "start": cmd_help,
    "tasks": cmd_tasks,
    "run": cmd_run,
    "ask": cmd_ask,
    "deep": cmd_deep,
    "say": cmd_say,
    "status": cmd_status,
    "quota": cmd_quota,
    "report": cmd_report,
    "reminders": cmd_reminders,
    "remind": cmd_remind,
    "memories": cmd_memories,
    "forget": cmd_forget,
    "kill": cmd_kill,
    "enable": cmd_enable,
    "summarize": cmd_summarize,
    "expenses": cmd_expenses,
    "diag": cmd_diag,
    "verify": cmd_verify,
}


def dispatch(text):
    """Route a message: slash commands → table, anything else → router."""
    if text.startswith("/"):
        parts = text[1:].split(" ", 1)
        cmd = parts[0].split("@")[0].lower()  # tolerate /help@botname
        args = parts[1].strip() if len(parts) > 1 else ""
        fn = COMMANDS.get(cmd)
        if fn:
            fn(args)
        else:
            comms.send(HELP_HTML, html=True)
    else:
        router.handle_plain_text(text)


# ---------------------------------------------------------------------------
# Photos and voice
# ---------------------------------------------------------------------------

def _handle_photo(msg):
    import base64

    comms.typing()
    photo = msg["photo"][-1]  # largest size
    caption = (msg.get("caption") or
               "What's in this image? Describe it briefly.").strip()
    try:
        data, _ = comms.download(photo["file_id"])
    except Exception as e:
        comms.send(f"Couldn't download the photo: {comms.esc(e)}", html=True)
        return
    b64 = base64.b64encode(data).decode()

    vmodel = config.VISION_MODEL or (
        "gemini:gemini-3.6-flash" if config.GEMINI_API_KEY
        else "thinkingmachines/inkling:free")
    try:
        reply = llm.complete(
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
        comms.send(f"📷 {reply}")
        memory.record_chat(caption, reply)
    except Exception as e:
        comms.send(f"Vision failed: {comms.esc(e)}", html=True)


def _handle_voice(msg):
    v = msg.get("voice") or msg.get("audio")
    if not config.GROQ_API_KEY:
        comms.send("Voice needs a GROQ_API_KEY (free at console.groq.com) — "
                   "add it on Render and I'll understand voice notes.")
        return
    comms.typing()
    try:
        data, fname = comms.download(v["file_id"])
    except Exception as e:
        comms.send(f"Couldn't download the voice note: {comms.esc(e)}", html=True)
        return
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            files={"file": (fname.split("/")[-1] or "voice.ogg", data,
                            "audio/ogg")},
            data={"model": "whisper-large-v3-turbo"},
            timeout=90,
        )
        r.raise_for_status()
        llm.count_request("groq")
        text = r.json()["text"].strip()
    except Exception as e:
        comms.send(f"Transcription failed: {comms.esc(e)}", html=True)
        return
    if not text:
        comms.send("(couldn't hear anything in that note?)")
        return
    comms.log(f"voice: {text[:60]}")
    comms.send(f"🎙 You said: {comms.esc(text)}", html=True)
    dispatch(text)  # act on it exactly like a typed message


def handle_message(msg):
    cid = str(msg.get("chat", {}).get("id", ""))
    if cid != str(config.OWNER_CHAT_ID):
        if msg.get("text") or msg.get("photo") or msg.get("voice"):
            comms.send(UNAUTHORIZED, cid)
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
    comms.log(f"command: {text[:60]}")
    try:
        dispatch(text)
    except Exception as e:
        import traceback
        traceback.print_exc()
        comms.send(f"⚠️ Command failed: {comms.esc(type(e).__name__)}: "
                   f"{comms.esc(e)}", html=True)


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

def telegram_loop():
    if not config.TELEGRAM_BOT_TOKEN:
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
    """Every minute, run any task whose schedule says it's due (Dhaka
    time). Paused tasks are skipped; each task runs in its own thread."""
    if not (config.TELEGRAM_BOT_TOKEN and config.OWNER_CHAT_ID):
        return
    last_run = {}
    while True:
        now = datetime.now() + config.BD_OFFSET
        paused = tasks.paused_names()
        for name, t in tasks.TASKS.items():
            if name in paused:
                continue
            due = t["schedule"](now)
            if due and now.strftime("%H:%M") != last_run.get(name):
                last_run[name] = now.strftime("%H:%M")
                comms.log(f"scheduled run: {name}")
                threading.Thread(
                    target=run_task_safely, args=(name,), daemon=True
                ).start()
        time.sleep(60)


def restore_state():
    """Load gist state and rebuild reminders + tasks created by message.
    Runs in a thread so a slow GitHub call never delays boot."""
    state.load()
    reminders.restore()
    for spec in state.STATE.get("dynamic_tasks", []):
        try:
            tasks.build(spec)
        except Exception as e:
            print("[state] task rebuild failed:", e)
    n_rem = len(reminders.REMINDERS)
    n_dyn = len(state.STATE.get("dynamic_tasks", []))
    if n_rem or n_dyn:
        comms.log(f"state restored: {n_rem} reminders, {n_dyn} tasks")
        state.save_soon()

    # self-report after every deploy — no more silent broken deploys
    import verify
    verify.boot_check()


# ---------------------------------------------------------------------------
# Boot — everything starts last, after all definitions
# ---------------------------------------------------------------------------

comms.register_menu()  # '/' menu inside Telegram

threading.Thread(target=restore_state, daemon=True).start()
threading.Thread(target=telegram_loop, daemon=True).start()
threading.Thread(target=scheduler_loop, daemon=True).start()
reminders.start()


@app.route("/")
@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(__import__("os").environ.get("PORT", 5000)))
