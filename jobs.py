"""Job queue for the home-PC worker, stored inside the gist-backed STATE.

Job shapes (all have: id, type, status, created, updated):
  {"type": "render", "script": {pipeline script JSON}}
  {"type": "upload", "paths": {"short": "...", "long": "...", "meta": "...",
                               "thumb": "..."}, "meta": {...}}
  {"type": "cleanup", "paths": [...]}
"""

import time
import uuid

import state


def add_job(jtype, payload):
    job = {
        "id": uuid.uuid4().hex[:10],
        "type": jtype,
        "status": "pending",
        "created": time.time(),
        "updated": time.time(),
    }
    job.update(payload)
    state.STATE["jobs"].append(job)
    del state.STATE["jobs"][:-100]  # keep the list bounded
    state.save_now()  # immediate write — a crash can never lose a job
    return job


def _cost_minutes(job):
    """Rough render-cost estimate in worker-minutes. Narration at ~150wpm
    -> video duration; MoviePy+motion encodes at ~13x realtime on an
    Actions runner. Cloud workers use this to skip jobs that can't fit
    their remaining budget — the PC worker (no deadline) takes those."""
    if job.get("type") != "render":
        return 1
    script = job.get("script") or {}
    words = len(script.get("hook", "").split())
    words += sum(len(s.get("narration", "").split())
                 for s in script.get("scenes", []))
    words += len(script.get("outro", "").split())
    duration_min = words / 150
    return duration_min * 13 + 3  # +3 for TTS/downloads/warmup


def next_job(max_cost_minutes=None):
    """Claim the oldest pending job the caller can afford. Re-syncs the
    queue from the gist first (source of truth), so a job queued by any
    thread or process is always visible. A job claimed but not reported
    on for 40 minutes goes back to pending (worker crashed)."""
    state.reload_jobs()
    now = time.time()
    for job in state.STATE["jobs"]:
        if job["status"] == "claimed" and now - job["updated"] > 2400:
            job["status"] = "pending"
            job["updated"] = now
            state.save_now()
    for job in state.STATE["jobs"]:
        if job["status"] != "pending":
            continue
        if (max_cost_minutes is not None
                and _cost_minutes(job) > max_cost_minutes):
            continue  # too big for this caller — leave it for the PC
        job["status"] = "claimed"
        job["updated"] = now
        state.save_now()  # immediate — two pollers can't both get it
        return job
    return None


def complete_job(job_id, result):
    for job in state.STATE["jobs"]:
        if job["id"] == job_id:
            job["status"] = "done" if result.get("ok") else "failed"
            job["updated"] = time.time()
            job["result"] = {k: result.get(k) for k in
                             ("msg", "video_url", "files")}
            state.save_now()
            return job
    return None


def pending_count():
    return sum(1 for j in state.STATE["jobs"] if j["status"] == "pending")


def prune_done():
    """Drop done/failed jobs older than a day."""
    now = time.time()
    before = len(state.STATE["jobs"])
    state.STATE["jobs"] = [j for j in state.STATE["jobs"]
                           if j["status"] in ("pending", "claimed")
                           or now - j["updated"] < 86400]
    if len(state.STATE["jobs"]) != before:
        state.save_soon()
