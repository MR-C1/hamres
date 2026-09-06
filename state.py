"""
Persistent state, stored as JSON in a secret GitHub Gist.
(Same pattern as hermes-agent: Render's free disk is ephemeral, a gist
survives restarts, is free, and needs no card.)

STATE shape:
{
  "jobs": [ ... render/upload jobs for the PC worker ... ],
  "job_tombstones": { job_id: epoch },  # deliberately deleted — see below
  "stats_history": [ {"date": "...", "subs": n, "views": n, "videos": {...}} ],
  "topic_direction": "latest Gemini guidance for script generation",
  "used_topics": [...],
  "pending_videos": { uuid: {title, paths, meta, job_id} },
  "early_decisions": { approval_id: "approved"|"rejected" },  # tapped early
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
LOADED = False  # saves are blocked until the real state has been loaded

_gist_id = None
_gist_id_lock = threading.Lock()
_save_lock = threading.Lock()  # serializes dump+PATCH (and reload merge)
_timer = None
_timer_lock = threading.Lock()


def _headers():
    return {"Authorization": f"token {config.GIST_TOKEN}"}


def _find_gist():
    # per_page=100: the default listing stops at 30 gists, and if the
    # owner's account ever grows past that the state gist would fall off
    # the page — _find_gist would then return None and save_now would
    # silently create a duplicate, splitting state in two.
    r = requests.get("https://api.github.com/gists?per_page=100",
                     headers=_headers(), timeout=15)
    r.raise_for_status()
    for g in r.json():
        if g.get("description") == GIST_DESC and GIST_FILE in g.get("files", {}):
            return g["id"]
    return None


def load():
    """Fill STATE from the gist.

    On a TRANSIENT failure (GitHub 5xx, timeout) we retry a few times and
    must NOT mark saves as allowed — otherwise the first save would patch
    the real gist with near-empty default state, permanently wiping jobs,
    stats, and approvals. Only mark LOADED when we genuinely have state:
    loaded OK, no gist exists yet (first boot), or no token (memory-only).
    """
    global LOADED, _gist_id
    if not config.GIST_TOKEN:
        print("[state] GIST_TOKEN not set — memory-only mode")
        LOADED = True
        return
    import time as _time
    for attempt in range(4):
        try:
            _gist_id = _find_gist()
            if not _gist_id:
                print("[state] no gist yet — created on first save")
                LOADED = True  # nothing to lose
                return
            r = requests.get(f"https://api.github.com/gists/{_gist_id}",
                             headers=_headers(), timeout=15)
            r.raise_for_status()
            content = r.json()["files"][GIST_FILE].get("content") or "{}"
            STATE.update(json.loads(content))
            print(f"[state] restored {len(STATE)} keys from gist")
            LOADED = True
            return
        except Exception as e:
            print(f"[state] load attempt {attempt + 1} failed: {e}")
            _time.sleep(2 * (attempt + 1))
    # All retries failed: run memory-only and DO NOT save — writing
    # defaults over the real gist would destroy all persistent state.
    # The dyno will restart within hours and try again.
    print("[state] load failed after retries — memory-only, saves disabled")


def _stamp(job):
    """When this copy of the job was last touched. `updated` is re-stamped
    on every status transition, so the newest stamp is the latest truth —
    whichever process wrote it."""
    return job.get("updated") or job.get("created") or 0


def _merge_jobs(local, remote, tombstones=()):
    """Pure merge of the local job list with the gist's copy.

    Kept separate (and pure) so selftest_jobs.py can prove the rules
    without a network. For a job both sides know, the copy with the
    newer stamp wins — statuses move one transition at a time and every
    transition re-stamps `updated`, so newest-stamp-wins is "latest
    truth wins" even when the two copies came from different processes.
    That is what keeps a worker's claim alive across a Render deploy:
    the new dyno boots holding the pre-claim copy, and its first save
    must not write 'pending' back over the 'claimed' the draining dyno
    recorded. Ids on only one side are kept (a job the gist hasn't seen
    survives; a job only the gist has is picked up). Ids in `tombstones`
    are dropped from both sides — that is how a deliberate deletion
    (clear-queue, prune) stays deleted while the gist still holds a copy.
    """
    dead = set(tombstones)
    out = {}
    for job in local:
        jid = job.get("id")
        if jid is not None and jid not in dead:
            out[jid] = job
    for job in remote:
        jid = job.get("id")
        if jid is None or jid in dead:
            continue
        cur = out.get(jid)
        if cur is None or _stamp(job) > _stamp(cur):
            out[jid] = job
    merged = sorted(out.values(), key=lambda j: j.get("created") or 0)
    return merged[-100:]


def tombstone_jobs(ids):
    """Mark job ids as deliberately deleted.

    save_now() merges the gist's freshest jobs into the dump before
    writing (see _absorb_remote). Without this marker that merge would
    resurrect anything deliberately removed — clear-queue would become a
    no-op the moment another save ran. Entries expire after an hour:
    long enough to outlive any stale dump from a draining dyno, short
    enough that the map cannot grow without bound.
    """
    now = time.time()
    ts = {k: v for k, v in STATE.get("job_tombstones", {}).items()
          if now - v <= 3600}
    for jid in ids:
        if jid:
            ts[jid] = now
    STATE["job_tombstones"] = ts


def reload_jobs():
    """Pull just the jobs list fresh from the gist, then MERGE into the
    local list rather than replacing it — replacing could discard a job
    that another thread appended between our read and the assignment.

    Newest stamp wins for a job both copies know (see _merge_jobs): a
    transition recorded by this process is newer than anything on the
    gist, and a transition another process recorded during a deploy
    overlap is newer than this process's stale copy. A job the gist has
    not seen yet is KEPT: add_job appends and then writes, and the write
    can take seconds.
    """
    global _gist_id
    if not config.GIST_TOKEN or not LOADED:
        return
    try:
        with _gist_id_lock:
            if _gist_id is None:
                _gist_id = _find_gist()
            gist_id = _gist_id
        if gist_id is None:
            return
        r = requests.get(f"https://api.github.com/gists/{gist_id}",
                         headers=_headers(), timeout=10)
        r.raise_for_status()
        content = r.json()["files"][GIST_FILE].get("content") or "{}"
        remote = json.loads(content).get("jobs", [])
        with _save_lock:
            STATE["jobs"] = _merge_jobs(STATE["jobs"], remote,
                                        STATE.get("job_tombstones", {}))
    except Exception as e:
        print("[state] reload_jobs failed:", e)


def _dump():
    for attempt in (1, 2):
        try:
            return json.dumps(STATE, default=str)
        except RuntimeError:
            if attempt == 2:
                raise


def _absorb_remote(gist_id):
    """Fold the gist's freshest jobs and worker check-in into STATE, just
    before a dump.

    Render deploys run the new dyno while the old one is still serving,
    and a claim or report the old dyno records in that window lives only
    on the gist — the new dyno's memory still holds the pre-claim copy,
    and without this fold its next save would write that stale copy back
    over the transition. This exact race reverted a mid-render claim to
    'pending' (a second runner re-rendered the same video) and ate a
    finished render's report. Jobs merge by newest stamp with tombstones
    honored; `worker` goes to the gist's copy wholesale when its check-in
    is newer, since that dict's only writer is whichever dyno the worker
    last reached. A merge failure must never block the save itself.
    """
    try:
        r = requests.get(f"https://api.github.com/gists/{gist_id}",
                         headers=_headers(), timeout=10)
        r.raise_for_status()
        remote = json.loads(
            r.json()["files"][GIST_FILE].get("content") or "{}")
        STATE["jobs"] = _merge_jobs(STATE["jobs"], remote.get("jobs", []),
                                    STATE.get("job_tombstones", {}))
        rw, lw = remote.get("worker") or {}, STATE.get("worker") or {}
        if (str(rw.get("last_seen") or "")
                > str(lw.get("last_seen") or "")):
            STATE["worker"] = rw
    except Exception as e:
        print("[state] pre-save merge failed (saving anyway):", e)


def save_now():
    """Serialize the whole dump+PATCH under one lock so two threads can't
    interleave reads/writes and clobber each other's snapshot. The dump
    is preceded by _absorb_remote, so what goes to the gist is this
    process's changes layered over the gist's own freshest state."""
    global _gist_id
    if not config.GIST_TOKEN or not LOADED:
        return  # never write before the real state is loaded (restart race)
    with _save_lock:
        with _gist_id_lock:
            if _gist_id is None:
                _gist_id = _find_gist()
            gist_id = _gist_id
        if gist_id:
            _absorb_remote(gist_id)
        content = _dump()
        if gist_id:
            r = requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                headers=_headers(),
                json={"files": {GIST_FILE: {"content": content}}},
                timeout=15)
        else:
            r = requests.post(
                "https://api.github.com/gists", headers=_headers(),
                json={"description": GIST_DESC,
                      "files": {GIST_FILE: {"content": content}}},
                timeout=15)
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
    if not config.GIST_TOKEN or not LOADED:
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
    STATE.setdefault("job_tombstones", {})
    STATE.setdefault("stats_history", [])
    STATE.setdefault("topic_direction", "")
    STATE.setdefault("used_topics", [])
    STATE.setdefault("pending_videos", {})
    STATE.setdefault("early_decisions", {})
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
