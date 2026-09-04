"""Channel Agent — a 24/7 YouTube growth manager (Render brain + home-PC
worker). Telegram is the console.

This is the composition root: Flask worker endpoints, Telegram loop,
scheduler, slash commands, and approval buttons. Intelligence lives in
brain.py; YouTube access in yt.py; jobs in jobs.py.
"""

import re
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, request

import brain
import comms
import config
import jobs
import state
import yt

app = Flask(__name__)
STARTED_AT = time.time()
UNAUTHORIZED = "This agent answers its owner only."

TG_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

HELP_HTML = """🎬 <b>Channel Agent</b> — your 24/7 YouTube growth manager

<b>Commands</b>
/status — system health + job queue
/stats — live channel numbers
/next — queue a new video right now
/report — today's growth report
/idea &lt;topic&gt; — draft a script on a topic
/settings — show / change settings
/pause • /resume — stop or resume auto work

<b>Or just talk to me</b> — "why are views down?", "what should the next
video be about?", "how close am I to monetization?"

I check comments every few hours, report stats each morning, learn which
topics grow the channel, and keep videos queued. Your PC renders them and
sends previews here — tap ✅ to publish.
"""


# ---------------------------------------------------------------------------
# worker endpoints (home PC)
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return "ok"


@app.route("/debug")
def debug():
    if not config.WORKER_SECRET or request.headers.get("X-Worker-Secret") != config.WORKER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    import os
    return jsonify({
        "pid": os.getpid(),
        "jobs_in_memory": len(state.STATE.get("jobs", [])),
        "pending": jobs.pending_count(),
        "worker_seen": state.STATE.get("worker", {}).get("last_seen", ""),
        "uptime_s": int(time.time() - STARTED_AT),
    })


@app.route("/next-job")
def next_job():
    if not config.WORKER_SECRET or request.headers.get("X-Worker-Secret") != config.WORKER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    state.default_state()  # idempotent — state keys always exist
    state.STATE["worker"]["last_seen"] = datetime.utcnow().isoformat()
    if state.STATE["worker"].get("warned_offline"):
        state.STATE["worker"]["warned_offline"] = False
        comms.send("🖥 PC worker is back online ✅")
    state.save_soon()
    job = jobs.next_job()
    return jsonify(job or {})


@app.route("/report", methods=["POST"])
def report():
    if not config.WORKER_SECRET or request.headers.get("X-Worker-Secret") != config.WORKER_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    state.default_state()  # idempotent — state keys always exist
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("job_id", "")
    ok = bool(data.get("ok"))
    job = jobs.complete_job(job_id, data)

    if job and job["type"] == "render" and ok:
        # worker already sent the video preview to Telegram with buttons
        # v:<approval_id> / vx:<approval_id> — remember its context
        state.STATE["pending_videos"][job.get("approval_id", job_id)] = {
            "title": data.get("title", ""),
            "paths": data.get("files", {}),
            "job_id": job_id,
        }
        state.save_soon()
        s = state.STATE["settings"]
        if s.get("auto_approve"):
            _queue_upload(job.get("approval_id", job_id),
                          note="auto-approved")
    elif job and job["type"] == "upload" and ok:
        comms.send(f"📤 <b>Uploaded</b> — {comms.esc(data.get('title', 'video'))}\n"
                   f"{comms.esc(data.get('video_url', ''))}", html=True)
    elif not ok:
        comms.send(f"⚠️ <b>Job failed</b> (<code>{comms.esc(job['type'])}</code>)\n"
                   f"{comms.esc(data.get('msg', ''))[:400]}", html=True)
    return jsonify({"ok": True})


def _queue_upload(approval_id, note=""):
    p = state.STATE["pending_videos"].get(approval_id)
    if not p:
        return
    hour = brain.learn_best_hour()
    jobs.add_job("upload", {
        "approval_id": approval_id,
        "paths": p["paths"],
        "meta": {"title": p["title"]},
        "publish_hour": hour,
    })
    state.STATE["pending_videos"].pop(approval_id, None)
    state.save_soon()
    comms.send(f"📤 Queued upload of {comms.esc(p['title'][:60])} "
               f"({note or 'approved'}) — will publish at {hour}:00.", html=True)


# ---------------------------------------------------------------------------
# approval buttons (callback queries)
# ---------------------------------------------------------------------------

def handle_callback(cb):
    state.default_state()
    data = cb.get("data", "")
    cid = cb.get("id")
    comms.answer_callback(cid)
    action, _, uid = data.partition(":")

    if action == "v":      # approve video → queue upload
        state.STATE["settings"]["approved_count"] += 1
        count = state.STATE["settings"]["approved_count"]
        after = state.STATE["settings"]["auto_approve_after"]
        if (not state.STATE["settings"]["auto_approve"]
                and count >= after):
            state.STATE["settings"]["auto_approve"] = True
            comms.send(f"🤖 <b>Auto-approve enabled</b> — {count} approvals "
                       f"earned my trust. New videos will publish themselves; "
                       f"comment replies still need your ✅.", html=True)
        _queue_upload(uid)
    elif action == "vx":   # reject video → clean up on PC
        p = state.STATE["pending_videos"].pop(uid, None)
        if p:
            jobs.add_job("cleanup", {"paths": list(p.get("paths", {}).values())})
            state.save_soon()
        comms.send("🗑 Video discarded.")
    elif action == "r":
        comms.send(brain.post_reply(uid))
    elif action == "rx":
        p = state.STATE["pending_replies"].pop(uid, None)
        if p:
            state.STATE.setdefault("replied_comments", []).append(
                p["comment_id"])
            state.save_soon()
        comms.send("Skipped.")
    elif action == "t":
        comms.send(brain.apply_title(uid))
    elif action == "tx":
        state.STATE["pending_titles"].pop(uid, None)
        state.save_soon()
        comms.send("Keeping current title.")


# ---------------------------------------------------------------------------
# slash commands
# ---------------------------------------------------------------------------

def cmd_help(_):
    comms.send(HELP_HTML, html=True)


def cmd_status(_):
    w = state.STATE.get("worker", {})
    last = w.get("last_seen", "")
    mins = ""
    if last:
        try:
            seen = datetime.fromisoformat(last)
            mins = f"{int((datetime.utcnow() - seen).total_seconds() // 60)} min ago"
        except Exception:
            pass
    s = state.STATE["settings"]
    up = int(time.time() - STARTED_AT)
    comms.send(
        f"🩺 <b>Status</b>\n"
        f"• Queue: {jobs.pending_count()} pending jobs\n"
        f"• Awaiting your ✅: {len(state.STATE['pending_videos'])} videos, "
        f"{len(state.STATE['pending_replies'])} replies\n"
        f"• Auto-approve: {'ON' if s.get('auto_approve') else 'off'} "
        f"({s.get('approved_count', 0)}/{s.get('auto_approve_after', 10)})\n"
        f"• Auto work: {'PAUSED' if s.get('paused') else 'running'}\n"
        f"• PC worker: {'online ' + mins if mins else 'never seen'}\n"
        f"• Brain uptime: {up // 3600}h {(up % 3600) // 60}m\n"
        + "\n".join(comms.LOG[-6:]), html=True)


def cmd_stats(_):
    try:
        ch = yt.channel_stats()
        comms.send(f"📺 <b>{comms.esc(ch['title'])}</b>\n"
                   f"Subs: <b>{ch['subs']:,}</b> • Views: <b>{ch['views']:,}</b> "
                   f"• Videos: {ch['videos']}\n"
                   f"Monetization progress: {ch['subs']:,}/1,000 subs",
                   html=True)
    except Exception as e:
        comms.send(f"⚠️ {comms.esc(e)}", html=True)


def cmd_next(_):
    comms.send("Writing a script…")
    made = brain.queue_next_video(1)
    if not made:
        comms.send("Script generation failed — Gemini may be busy. Try again.")


def cmd_report(_):
    brain.daily_report()


def cmd_idea(args):
    if not args:
        comms.send("Usage: /idea why cats purr")
        return
    comms.typing()
    script = brain.generate_script(direction=f"Topic requested by the "
                                             f"channel owner: {args}")
    if not script:
        comms.send("Couldn't draft that — try again.")
        return
    job = jobs.add_job("render", {"script": script,
                                  "approval_id": job_approval_id()})
    state.STATE.setdefault("used_topics", []).append(script.get("id", "?"))
    state.save_soon()
    comms.send(f"🎬 Queued: <b>{comms.esc(script['title'])}</b>\n"
               f"Hook: {comms.esc(script['hook'])}", html=True)


def job_approval_id():
    import uuid
    return uuid.uuid4().hex[:10]


def cmd_pause(_):
    state.STATE["settings"]["paused"] = True
    state.save_soon()
    comms.send("⏸ Paused — no new auto work. Queued jobs still finish. "
               "Use /resume to restart.")


def cmd_resume(_):
    state.STATE["settings"]["paused"] = False
    state.save_soon()
    comms.send("▶️ Resumed.")


def cmd_settings(_):
    s = state.STATE["settings"]
    comms.send(
        f"⚙️ <b>Settings</b>\n"
        f"• auto_approve: <code>{s.get('auto_approve')}</code> "
        f"(earned {s.get('approved_count', 0)}/"
        f"{s.get('auto_approve_after', 10)} approvals)\n"
        f"• publish_hour: <code>{s.get('publish_hour', 17)}:00</code>\n"
        f"• paused: <code>{s.get('paused')}</code>\n\n"
        f"Change by chatting: \"auto approve on\", \"publish at 7pm\"",
        html=True)


def cmd_diag(_):
    comms.typing()
    import llm
    comms.send(f"🔍 <b>LLM chain health</b>\n\n{comms.esc(llm.diagnose())}",
               html=True)


def cmd_publish(_):
    """Publish rendered-but-unapproved videos (same as saying 'publish')."""
    _publish_pending()


COMMANDS = {
    "help": cmd_help, "start": cmd_help,
    "status": cmd_status,
    "stats": cmd_stats,
    "next": cmd_next,
    "report": cmd_report,
    "idea": cmd_idea,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "settings": cmd_settings,
    "diag": cmd_diag,
    "publish": cmd_publish,
}


PUBLISH_WORDS = {"publish", "upload", "release"}


def _is_publish_intent(text):
    """True for 'publish', 'ok publish now', 'please upload' etc. — any
    short chat message whose words are essentially a publish command.
    Longer sentences with real content still go to Gemini."""
    words = re.findall(r"[a-z]+", text.lower())
    return (bool(words) and len(words) <= 6
            and any(w in PUBLISH_WORDS for w in words)
            and all(w in PUBLISH_WORDS or w in {"ok", "now", "please",
                                                "the", "video", "it", "go",
                                                "yes", "do", "just"}
                    for w in words))


def _publish_pending():
    """Queue uploads for every rendered-but-unapproved video."""
    pending = state.STATE["pending_videos"]
    if not pending:
        comms.send("Nothing to publish — no rendered videos awaiting "
                   "approval. Render one with /next, then tap ✅ on the "
                   "preview, or say 'publish' once it's rendered.")
        return
    for uid in list(pending):
        state.STATE["settings"]["approved_count"] += 1
        _queue_upload(uid, note="published via chat")


def chat_reply(text):
    """Free text → Gemini with channel context. Publish-intent phrases
    are handled as real commands instead (the LLM only talks, it can't
    actually publish anything)."""
    if _is_publish_intent(text):
        state.default_state()
        _publish_pending()
        return
    comms.typing()
    ctx = ""
    try:
        ch = yt.channel_stats()
        ctx = (f"Channel: {ch['title']}, {ch['subs']} subs, "
               f"{ch['views']} views, {ch['videos']} videos. ")
    except Exception:
        pass
    reply = ""
    try:
        reply = brain.gemini(
            f"{ctx}The owner asks: \"{text}\"\n"
            f"Answer as their channel growth manager. Be concise and "
            f"concrete (under 150 words).")
    except Exception as e:
        reply = f"⚠️ All AI providers failed: {comms.esc(str(e)[:150])}"
    comms.send_md(reply)


def handle_message(msg):
    cid = str(msg.get("chat", {}).get("id", ""))
    if cid != str(config.OWNER_CHAT_ID):
        if msg.get("text"):
            comms.send(UNAUTHORIZED, cid)
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    comms.log(f"command: {text[:60]}")
    try:
        if text.startswith("/"):
            parts = text[1:].split(" ", 1)
            cmd = parts[0].split("@")[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ""
            COMMANDS.get(cmd, cmd_help)(args)
        else:
            chat_reply(text)
    except Exception as e:
        import traceback
        traceback.print_exc()
        comms.send(f"⚠️ Command failed: {comms.esc(type(e).__name__)}: "
                   f"{comms.esc(e)}", html=True)


# ---------------------------------------------------------------------------
# background loops
# ---------------------------------------------------------------------------

def telegram_loop():
    if not config.TELEGRAM_BOT_TOKEN:
        print("[agent] TELEGRAM_BOT_TOKEN not set — console disabled")
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
                if update.get("callback_query"):
                    handle_callback(update["callback_query"])
                elif update.get("message"):
                    handle_message(update["message"])
        except Exception as e:
            print("[telegram] loop error:", e)
            time.sleep(5)


def run_safely(name, fn):
    try:
        fn()
    except Exception as e:
        import traceback
        traceback.print_exc()
        comms.send(f"⚠️ <b>{comms.esc(name)}</b> failed: {comms.esc(e)[:200]}",
                   html=True)


def scheduler_loop():
    """Every minute, run whatever is due (Dhaka time). Daily jobs catch up
    if their slot was missed while the dyno was restarting."""
    if not (config.TELEGRAM_BOT_TOKEN and config.OWNER_CHAT_ID):
        return
    last_run = {}

    def once_per_day(name, fn, hour, minute):
        key = f"{name}:{now:%Y-%m-%d}"
        if now >= now.replace(hour=hour, minute=minute) and key not in last_run:
            last_run[key] = True
            comms.log(f"scheduled: {name}")
            threading.Thread(target=run_safely, args=(name, fn),
                             daemon=True).start()

    def every_hours(name, fn, hours):
        key = f"{name}:{now:%Y-%m-%d-%H}"
        if now.hour % hours == 0 and now.minute < 2 and key not in last_run:
            last_run[key] = True
            comms.log(f"scheduled: {name}")
            threading.Thread(target=run_safely, args=(name, fn),
                             daemon=True).start()

    while True:
        now = datetime.now() + config.BD_OFFSET
        state.default_state()  # self-heal if a restart lost keys
        if not state.STATE["settings"].get("paused"):
            once_per_day("daily report", brain.daily_report, 8, 0)
            once_per_day("planning", brain.analyze_and_plan, 8, 30)
            once_per_day("title check", brain.title_check, 12, 0)
            if now.weekday() == 6:
                once_per_day("weekly summary", brain.weekly_summary, 9, 0)
            every_hours("comment sweep", brain.comment_sweep, 4)

            # keep the render queue fed: if nothing pending, plan one video
            if (now.hour == 9 and now.minute == 0
                    and jobs.pending_count() == 0):
                threading.Thread(target=run_safely,
                                 args=("queue top-up",
                                       lambda: brain.queue_next_video(1)),
                                 daemon=True).start()

        # worker offline watchdog
        w = state.STATE.get("worker", {})
        if w.get("last_seen") and not w.get("warned_offline"):
            try:
                seen = datetime.fromisoformat(w["last_seen"])
                if (datetime.utcnow() - seen).total_seconds() > 7200:
                    w["warned_offline"] = True
                    state.save_soon()
                    comms.send("🖥 <b>PC worker offline for 2h+</b> — videos "
                               "can't render until it's back on. (Render "
                               "brain keeps watching the channel.)",
                               html=True)
            except Exception:
                pass
        time.sleep(60)


# ---------------------------------------------------------------------------
# boot
# ---------------------------------------------------------------------------

def boot():
    # SYNCHRONOUS load: must complete before gunicorn serves requests,
    # otherwise a worker poll saves empty state over the real gist.
    state.default_state()
    state.load()          # sets LOADED — saves stay blocked until done
    state.default_state()  # fill anything the gist lacked
    comms.register_menu()
    threading.Thread(target=state.saver_loop, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    if config.TELEGRAM_BOT_TOKEN and config.OWNER_CHAT_ID:
        comms.send(f"🎬 <b>Channel Agent online</b> — brain rebooted. "
                   f"Use /status for a health check.", html=True)


boot()  # runs at import, before the first request can arrive


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(__import__("os").environ.get("PORT", 5000)))
