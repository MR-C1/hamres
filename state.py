"""
Persistent state, stored as JSON in a secret GitHub Gist.
(Same pattern as hermes-agent: Render's free disk is ephemeral, a gist
survives restarts, is free, and needs no card.)

STATE shape:
{
  "jobs": [ ... render/upload jobs for the PC worker ... ],
  "stats_history": [ {"date": "...", "subs": n, "views": n, "videos": {...}} ],
  "topic_direction": "latest Gemini guidance for script generation",
  "used_topics": [...],
  "pending_videos": { uuid: {title, paths, meta, job_id} },
  "pending_replies": { uuid: {comment_id, draft, video_title} },
  "pending_titles":  { uuid: {video_id, title, current} },
  "replied_comments": [...],
  "settings": {paused, auto_approve, approved_count, auto_approve_after,
               publish_hour},
  "worker": {"last_seen": iso, "last_job": str, "warned_offline": bool},
  "best_hour": 17
}
"""

import json
import os
import threading
import time

import requests

import config

GIST_DESC = "channel-agent state"
GIST_FILE = "channel-state.json"

STATE = {}  # the single shared state dict — mutate in place, then save_soon()

_gist_id = None
_gist_id_lock = threading.Lock()
_timer = None
_timer_lock = threading.Lock()


def _headers():
    return {"Authorization": f"token {config.GIST_TOKEN}"}


def _find_gist():
    r = requests.get("https://api.github.com/gists", headers=_headers(),
                     timeout=15)
    r.raise_for_status()
    for g in r.json():
        if g.get("description") == GIST_DESC and GIST_FILE in g.get("files", {}):
            return g["id"]
    return None


def load():
    if not config.GIST_TOKEN:
        print("[state] GIST_TOKEN not set — memory-only mode")
        return
    global _gist_id
    try:
        _gist_id = _find_gist()
        if not _gist_id:
            print("[state] no gist yet — created on first save")
            return
        r = requests.get(f"https://api.github.com/gists/{_gist_id}",
                         headers=_headers(), timeout=15)
        r.raise_for_status()
        content = r.json()["files"][GIST_FILE].get("content") or "{}"
        STATE.update(json.loads(content))
        print(f"[state] restored {len(STATE)} keys from gist")
    except Exception as e:
        print("[state] load failed:", e)


def _dump():
    for attempt in (1, 2):
        try:
            return json.dumps(STATE, default=str)
        except RuntimeError:
            if attempt == 2:
                raise


def save_now():
    global _gist_id
    if not config.GIST_TOKEN:
        return
    content = _dump()
    with _gist_id_lock:
        if _gist_id is None:
            _gist_id = _find_gist()
        if _gist_id:
            r = requests.patch(
                f"https://api.github.com/gists/{_gist_id}",
                headers=_headers(),
                json={"files": {GIST_FILE: {"content": content}}}, timeout=15)
        else:
            r = requests.post(
                "https://api.github.com/gists", headers=_headers(),
                json={"description": GIST_DESC,
                      "files": {GIST_FILE: {"content": content}}}, timeout=15)
            if r.ok:
                _gist_id = r.json()["id"]
    r.raise_for_status()


def _flush():
    global _timer
    with _timer_lock:
        _timer = None
    try:
        save_now()
    except Exception as e:
        print("[state] save failed:", e)


def save_soon():
    """Debounced save — a burst of changes becomes one gist write (~2s)."""
    global _timer
    if not config.GIST_TOKEN:
        return
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(2.0, _flush)
        _timer.daemon = True
        _timer.start()


def saver_loop():
    if not config.GIST_TOKEN:
        return
    while True:
        time.sleep(300)
        try:
            save_now()
        except Exception as e:
            print("[state] periodic save failed:", e)


def default_state():
    """Ensure every key exists — call once after load()."""
    STATE.setdefault("jobs", [])
    STATE.setdefault("stats_history", [])
    STATE.setdefault("topic_direction", "")
    STATE.setdefault("used_topics", [])
    STATE.setdefault("pending_videos", {})
    STATE.setdefault("pending_replies", {})
    STATE.setdefault("pending_titles", {})
    STATE.setdefault("replied_comments", [])
    STATE.setdefault("settings", {
        "paused": False,
        "auto_approve": False,
        "approved_count": 0,
        "auto_approve_after": 10,
        "publish_hour": 17,
    })
    STATE.setdefault("worker", {"last_seen": "", "last_job": "",
                                "warned_offline": False})
    STATE.setdefault("best_hour", 17)
