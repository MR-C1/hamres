"""The PC worker — the muscle of the channel-agent system.

Polls the Render brain every 60s for jobs, renders videos (existing
pipeline), uploads approved ones to YouTube, and sends video previews
straight into Telegram with ✅/❌ approval buttons.

The bot TOKEN is shared with the brain: sending via the bot API is fine
from many machines — only getUpdates (which the brain owns) is exclusive.

Run:   .venv\\Scripts\\python worker.py        (keep this window open)
Auto:  run_worker.bat registered at logon (see SETUP_AGENT.md)
"""

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
deadline = None  # wall-clock run limit (cloud mode); None on the PC


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

def send_video_preview(path, approval_id, title, url=""):
    """Send the rendered file to Telegram with ✅/❌ buttons. The video
    is ALREADY uploaded (private) — ✅ makes it public, ❌ deletes it.
    There is no time limit on the decision."""
    api = f"https://api.telegram.org/bot{CFG['agent']['bot_token']}"
    chat = CFG["agent"]["chat_id"]
    kb = {"inline_keyboard": [[
        {"text": "✅ Publish", "callback_data": f"v:{approval_id}"},
        {"text": "❌ Discard", "callback_data": f"vx:{approval_id}"},
    ]]}
    is_short = "_short" in path.name
    caption = (f"🎬 <b>{_esc(title)}</b>\n"
               f"{'Short (vertical)' if is_short else 'Long-form'}\n"
               f"⚡ Already uploaded (private) — no time limit. "
               f"✅ makes it public; ❌ deletes it. ")
    if url:
        caption += f"<a href=\"{_esc(url)}\">Watch it</a>."
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


def _esc(s):
    """HTML-escape for Telegram captions — an LLM-written title with
    < or & would make Telegram reject the whole preview message."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# job execution
# ---------------------------------------------------------------------------

def do_render(job):
    """Render, upload IMMEDIATELY as private (approval-first), then send
    the Telegram preview with ✅/❌ buttons that flip it public / delete
    it — hours or days later, no time window. The video is already
    safely on YouTube before the owner is ever asked, so nothing can be
    lost to a runner dying or a worker restart."""
    import render_video
    from make_thumbnails import make_thumbnail
    script = job["script"]
    sid = script["id"]
    log.info("render job: %s", sid)
    render_video.render_from_dict(script, CFG)
    make_thumbnail(script, REVIEW / f"{sid}_thumb.png")
    short = REVIEW / f"{sid}_short.mp4"
    long_v = REVIEW / f"{sid}_long.mp4"
    approval_id = job.get("approval_id", job["id"])

    # upload right away — private, NEVER auto-scheduled public (only the
    # owner's ✅ makes a video public)
    up = _upload_files(script, sid)
    if up.get("video_url"):
        sent = send_video_preview(short, approval_id, script["title"],
                                  url=up.get("video_url"))
        if long_v.exists() and long_v.stat().st_size < 45 << 20:
            # send long-form too, same approval buttons
            send_video_preview(long_v, approval_id, script["title"],
                               url=up.get("video_url"))
        if sent:
            _cleanup_files(sid)  # preview sent; intermediates unneeded

    result = {
        "ok": True,
        "title": script["title"],
        "video_url": up.get("video_url", ""),
        "video_urls": up.get("video_urls", []),
        "uploaded": bool(up.get("video_url")),
        "msg": up.get("msg", "rendered"),
    }
    if not up.get("video_url"):
        # upload failed (e.g. dead token) — keep files for a retry and say so
        result["msg"] = (f"rendered but upload FAILED: {up.get('msg', '?')} "
                         f"— files kept locally for retry")
    return result


def _upload_files(script, sid):
    import upload
    meta = {"title": script["title"],
            "description": script.get("description", ""),
            "tags": ", ".join(script.get("tags", []))}
    meta_path = REVIEW / f"{sid}_metadata.txt"
    if meta_path.exists():
        parsed = upload.parse_metadata(meta_path)
        meta.update({k: v for k, v in parsed.items() if v})

    urls = []
    for name in (f"{sid}_short.mp4", f"{sid}_long.mp4"):
        f = REVIEW / name
        if not f.exists():
            continue
        url = upload.upload_video(f, meta, CFG)
        urls.append(url)
        log.info("uploaded %s -> %s", f.name, url)
        # custom thumbnail on the long-form only (Shorts ignore it)
        if url and name.endswith("_long.mp4"):
            thumb = REVIEW / f"{sid}_thumb.png"
            if thumb.exists():
                try:
                    upload.set_thumbnail(url, thumb, CFG)
                    log.info("thumbnail set for %s", name)
                except Exception as e:
                    log.warning("thumbnail failed: %s", e)
    # BOTH urls ride the report — the brain's ✅ must flip the short AND
    # the long public, not just the first one
    return {"video_url": urls[0] if urls else "",
            "video_urls": urls,
            "title": script["title"] if urls else "",
            "msg": "; ".join(urls)}


def _cleanup_files(sid):
    """Remove a published video's intermediates so review/ never fills
    the disk. Keep the files on reject — owner may want another look."""
    removed = 0
    for pattern in (f"{sid}_short.mp4", f"{sid}_long.mp4",
                    f"{sid}_metadata.txt", f"{sid}_thumb.png",
                    f"{sid}_shortTEMP_MPY_wvf_snd.*",
                    f"{sid}_longTEMP_MPY_wvf_snd.*"):
        for p in REVIEW.glob(pattern):
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
    if removed:
        log.info("cleaned %d files for %s", removed, sid)


def do_cleanup(job):
    """Legacy remote-cleanup jobs (paths from another machine never
    resolve — cleanup now happens in-render on the same machine)."""
    removed = []
    for p in job.get("paths", []):
        if p and Path(p).exists():
            Path(p).unlink()
            removed.append(Path(p).name)
    return {"ok": True, "msg": f"removed {len(removed)} files"}


HANDLERS = {"render": do_render, "cleanup": do_cleanup}


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
    global CFG, deadline
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
            elif job:
                # unknown job type — report the mismatch instead of
                # leaving it claimed forever
                log.warning("job %s has unknown type %r — failing it",
                            job.get("id"), job.get("type"))
                brain_report({"job_id": job.get("id"), "ok": False,
                              "msg": f"unknown job type: {job.get('type')!r}"})
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
