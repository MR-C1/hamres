"""The PC worker — the muscle of the channel-agent system.

Polls the Render brain every 60s for jobs, renders videos (existing
pipeline), uploads approved ones to YouTube, and sends video previews
straight into Telegram with ✅/❌ approval buttons.

The bot TOKEN is shared with the brain: sending via the bot API is fine
from many machines — only getUpdates (which the brain owns) is exclusive.

Run:   .venv\\Scripts\\python worker.py        (keep this window open)
Auto:  run_worker.bat registered at logon (see SETUP_AGENT.md)
"""

import json
import os
import shutil
import sys
import time
import traceback
import requests
from pathlib import Path

import common
from common import REVIEW, load_config, setup_logging

log = setup_logging("worker")

CFG = None


# ---------------------------------------------------------------------------
# brain HTTP
# ---------------------------------------------------------------------------

def brain_get(path):
    url = CFG["agent"]["url"].rstrip("/") + path
    r = requests.get(url, headers={"X-Worker-Secret": CFG["agent"]["secret"]},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def brain_report(payload):
    url = CFG["agent"]["url"].rstrip("/") + "/report"
    r = requests.post(url, json=payload,
                      headers={"X-Worker-Secret": CFG["agent"]["secret"]},
                      timeout=30)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# telegram direct send (video previews with approval buttons)
# ---------------------------------------------------------------------------

def send_video_preview(path, approval_id, title):
    """Send the rendered file to Telegram with ✅/❌ buttons."""
    api = f"https://api.telegram.org/bot{CFG['agent']['bot_token']}"
    chat = CFG["agent"]["chat_id"]
    kb = {"inline_keyboard": [[
        {"text": "✅ Publish", "callback_data": f"v:{approval_id}"},
        {"text": "❌ Discard", "callback_data": f"vx:{approval_id}"},
    ]]}
    is_short = "_short" in path.name
    caption = (f"🎬 <b>{title}</b>\n"
               f"{'Short (vertical)' if is_short else 'Long-form'} — "
               f"watch, then decide. ✅ publishes as scheduled at the "
               f"best hour; ❌ deletes it.")
    with open(path, "rb") as f:
        r = requests.post(
            f"{api}/sendDocument" if path.stat().st_size > 10 << 20
            else f"{api}/sendVideo",
            data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
            files={"document" if path.stat().st_size > 10 << 20
                   else "video": (path.name, f, "video/mp4")},
            timeout=600)
    # the buttons ride on the video message
    msg = r.json().get("result", {}).get("message_id")
    if msg:
        requests.post(f"{api}/editMessageReplyMarkup", json={
            "chat_id": chat, "message_id": msg, "reply_markup": kb},
            timeout=15)
    return r.json().get("ok", False)


# ---------------------------------------------------------------------------
# job execution
# ---------------------------------------------------------------------------

def do_render(job):
    import render_video
    from make_thumbnails import make_thumbnail
    script = job["script"]
    sid = script["id"]
    log.info("render job: %s", sid)
    outputs = render_video.render_from_dict(script, CFG)
    make_thumbnail(script, REVIEW / f"{sid}_thumb.png")
    short = REVIEW / f"{sid}_short.mp4"
    long_v = REVIEW / f"{sid}_long.mp4"
    meta = REVIEW / f"{sid}_metadata.txt"

    # preview into Telegram (short is small enough to send fully)
    sent = False
    if short.exists():
        sent = send_video_preview(short, job.get("approval_id", job["id"]),
                                  script["title"])
        if long_v.exists() and long_v.stat().st_size < 45 << 20:
            # send long-form too, same approval buttons
            send_video_preview(long_v, job.get("approval_id", job["id"]),
                               script["title"])
    return {
        "ok": True,
        "title": script["title"],
        "files": {
            "short": str(short) if short.exists() else "",
            "long": str(long_v) if long_v.exists() else "",
            "meta": str(meta) if meta.exists() else "",
            "thumb": str(REVIEW / f"{sid}_thumb.png"),
        },
        "msg": "rendered" + ("" if sent else " (telegram preview failed)"),
    }


def do_upload(job):
    import upload
    paths = job.get("paths", {})
    hour = job.get("publish_hour", 17)
    meta = {"title": job.get("meta", {}).get("title", ""),
            "description": "", "tags": ""}
    # enrich metadata from the meta file if present
    if paths.get("meta") and Path(paths["meta"]).exists():
        parsed = upload.parse_metadata(Path(paths["meta"]))
        meta.update({k: v for k, v in parsed.items() if v})

    results = []
    for key in ("short", "long"):
        f = paths.get(key)
        if f and Path(f).exists():
            url = upload.upload_video(Path(f), meta, CFG, publish_hour=hour)
            results.append(url)
            log.info("uploaded %s -> %s", f, url)
    return {"ok": bool(results), "video_url": results[0] if results else "",
            "msg": "; ".join(results)}


def do_cleanup(job):
    removed = []
    for p in job.get("paths", []):
        if p and Path(p).exists():
            Path(p).unlink()
            removed.append(Path(p).name)
    return {"ok": True, "msg": f"removed {len(removed)} files"}


HANDLERS = {"render": do_render, "upload": do_upload, "cleanup": do_cleanup}


def guard_disk():
    """Keep the render cache under control — free space below 3GB cleans it."""
    if shutil.disk_usage(common.ROOT).free < 3 << 30:
        clips = common.CACHE / "clips"
        if clips.exists():
            shutil.rmtree(clips, ignore_errors=True)
            log.warning("disk low — clip cache wiped (it re-downloads)")


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def ensure_single_instance():
    """One worker only — a second start exits quietly instead of fighting
    the first one over jobs."""
    lock = common.ROOT / "worker.lock"
    if lock.exists():
        try:
            old_pid = int(lock.read_text().strip())
            import psutil
            try:
                proc = psutil.Process(old_pid)
                # only treat it as OUR lock if the pid is actually a python
                # process — Windows recycles pids, so a stale lock can point
                # at an unrelated live process (e.g. VS Code)
                if proc.name().lower().startswith("python"):
                    print(f"worker already running (pid {old_pid}) — exiting")
                    sys.exit(0)
            except psutil.NoSuchProcess:
                pass  # stale lock — take over
        except (ValueError, ImportError):
            pass  # corrupt lock or psutil missing — take over
    lock.write_text(str(os.getpid()))
    return lock


def main():
    global CFG
    # MoviePy writes its temp audio files to the CWD — make sure that's the
    # PC dir no matter how we were launched (logon autostart CWDs to System32,
    # which gives ffmpeg Permission denied)
    os.chdir(Path(__file__).resolve().parent)
    ensure_single_instance()
    deadline = (time.time() + float(os.environ.get("WORKER_MAX_MINUTES", 0)) * 60
                if os.environ.get("WORKER_MAX_MINUTES") else None)
    CFG = load_config()
    agent = CFG.get("agent", {})
    if not agent.get("url") or not agent.get("secret"):
        print("config.yaml missing [agent] url/secret — see SETUP_AGENT.md")
        return
    log.info("worker started — polling %s", agent["url"])
    empty_polls = 0
    while True:
        try:
            # don't claim a job we can't finish before the deadline
            if deadline and deadline - time.time() < 18 * 60:
                log.info("under 18 min left — exiting, next run takes over")
                break
            job = brain_get("/next-job")
            if job and job.get("type") in HANDLERS:
                log.info("job %s (%s)", job["id"], job["type"])
                try:
                    result = HANDLERS[job["type"]](job)
                except Exception as e:
                    log.error("job %s crashed:\n%s", job["id"],
                              traceback.format_exc())
                    result = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
                brain_report({"job_id": job["id"], **result})
                log.info("job %s -> %s", job["id"],
                         "ok" if result.get("ok") else "FAILED")
                empty_polls = 0
            else:
                empty_polls += 1
                if empty_polls % 5 == 1:  # heartbeat every ~5 min
                    log.info("idle — no jobs (polling every 60s, %d min)",
                             empty_polls)
                # in cloud mode (deadline set): nothing to do for 5 min = leave
                if deadline and empty_polls >= 5:
                    log.info("idle for 5 min — exiting to save quota")
                    break
            guard_disk()
        except requests.RequestException as e:
            log.warning("brain unreachable: %s", str(e)[:100])
        except Exception as e:
            log.error("poll loop crashed:\n%s", traceback.format_exc())
        if deadline and time.time() > deadline:
            log.info("run time limit reached — exiting cleanly")
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
