"""
Persistent state, stored as JSON in a secret GitHub Gist.

Why: Render's free tier has ephemeral disk — every restart and every
deploy wipes in-memory state (reminders, tasks created by message,
price baselines, page-watch hashes). A gist survives both, is free,
and needs no card.

Setup: GIST_TOKEN = a GitHub personal access token with gist
read/write permission. Without it, everything still works — state
just stays in memory, as before.
"""

import json
import os
import threading
import time

import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN", "")
GIST_DESC = "hermes-agent state"
GIST_FILE = "hermes-state.json"

STATE = {}  # the single shared state dict — mutate in place, then save_soon()

_gist_id = None
_gist_id_lock = threading.Lock()
_timer = None
_timer_lock = threading.Lock()


def _headers():
    return {"Authorization": f"token {GIST_TOKEN}"}


def _find_gist():
    # one page of the user's gists is plenty for a match by description
    r = requests.get("https://api.github.com/gists", headers=_headers(), timeout=15)
    r.raise_for_status()
    for g in r.json():
        if g.get("description") == GIST_DESC and GIST_FILE in g.get("files", {}):
            return g["id"]
    return None


def load():
    """Fill STATE from the gist. No-op without a token."""
    if not GIST_TOKEN:
        print("[state] GIST_TOKEN not set — memory-only mode")
        return
    global _gist_id
    try:
        _gist_id = _find_gist()
        if not _gist_id:
            print("[state] no gist yet — it will be created on first save")
            return
        r = requests.get(
            f"https://api.github.com/gists/{_gist_id}", headers=_headers(), timeout=15
        )
        r.raise_for_status()
        content = r.json()["files"][GIST_FILE].get("content") or "{}"
        STATE.update(json.loads(content))
        print(f"[state] restored {len(STATE)} keys from gist {_gist_id[:8]}…")
    except Exception as e:
        print("[state] load failed:", e)


def _dump():
    for attempt in (1, 2):  # a concurrent mutation can interrupt iteration
        try:
            return json.dumps(STATE, default=str)
        except RuntimeError:
            if attempt == 2:
                raise


def save_now():
    """Write STATE to the gist immediately. Raises on failure."""
    global _gist_id
    if not GIST_TOKEN:
        return
    content = _dump()
    with _gist_id_lock:
        if _gist_id is None:
            _gist_id = _find_gist()
        if _gist_id:
            r = requests.patch(
                f"https://api.github.com/gists/{_gist_id}",
                headers=_headers(),
                json={"files": {GIST_FILE: {"content": content}}},
                timeout=15,
            )
        else:
            r = requests.post(
                "https://api.github.com/gists",
                headers=_headers(),
                json={
                    "description": GIST_DESC,
                    "files": {GIST_FILE: {"content": content}},
                },
                timeout=15,
            )
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
    if not GIST_TOKEN:
        return
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(2.0, _flush)
        _timer.daemon = True
        _timer.start()


def saver_loop():
    """Periodic safety net — flushes price baselines etc. every 5 min."""
    if not GIST_TOKEN:
        return
    while True:
        time.sleep(300)
        try:
            save_now()
        except Exception as e:
            print("[state] periodic save failed:", e)
