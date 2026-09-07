"""Job queue for the home-PC worker, stored inside the gist-backed STATE.

Job shapes (all have: id, type, status, created, updated):
  {"type": "render", "script": {pipeline script JSON}}
  {"type": "upload", "paths": {"short": "...", "long": "...", "meta": "...",
                               "thumb": "..."}, "meta": {...}}
  {"type": "cleanup", "paths": [...]}
"""

import time
import uuid

import comms
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
    dropped = state.STATE["jobs"][:-100]  # keep the list bounded
    del state.STATE["jobs"][:-100]
    if dropped:
        # tombstone, or the pre-save merge would resurrect them from the
        # gist's copy on the very next write
        state.tombstone_jobs(j["id"] for j in dropped)
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


def _claim_ttl(job):
    """Seconds a claim may go quiet before its worker is presumed dead.

    A flat 40 minutes was shorter than a real render. _cost_minutes puts a
    long-form video at 100+ worker-minutes, and the worker doesn't poll while
    it renders, so the brain handed a job back to the queue that a runner was
    still working on: a second runner claimed it, rendered the same script,
    and uploaded the same video again — leaving the first upload private,
    orphaned and invisible. Twice the estimate gives honest headroom, floored
    at the old 40 minutes for tiny jobs and capped just past the workflow's
    own 330-minute ceiling, past which no runner can still be alive.
    """
    return min(max(2400, _cost_minutes(job) * 120), 6 * 3600)


def next_job(max_cost_minutes=None):
    """Claim the oldest pending job the caller can afford. Re-syncs the
    queue from the gist first (source of truth), so a job queued by any
    thread or process is always visible. A claim that goes quiet for longer
    than the render could plausibly take goes back to pending."""
    state.reload_jobs()
    now = time.time()
    # One write for the whole sweep: save_now() is a gist PATCH under a lock
    # and this runs on a worker poll, so saving per job made a poll that found
    # several stale claims wait through that many sequential round trips.
    stale = False
    for job in state.STATE["jobs"]:
        quiet = now - job["updated"]
        if job["status"] == "claimed" and quiet > _claim_ttl(job):
            job["status"] = "pending"
            job["updated"] = now
            stale = True
            comms.log(f"job {job['id']} reclaimed after "
                      f"{int(quiet / 60)} min quiet — worker presumed dead")
    if stale:
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
                             ("msg", "video_url", "video_urls", "files")}
            state.save_now()
            return job
    return None


def pending_count():
    return sum(1 for j in state.STATE["jobs"] if j["status"] == "pending")


def prune_done():
    """Drop done/failed jobs older than a day."""
    now = time.time()
    keep, drop = [], []
    for j in state.STATE["jobs"]:
        fresh = (j["status"] in ("pending", "claimed")
                 or now - j.get("updated", 0) < 86400)
        (keep if fresh else drop).append(j)
    if drop:
        state.STATE["jobs"] = keep
        # tombstone, or the pre-save merge would resurrect them
        state.tombstone_jobs(j["id"] for j in drop)
        state.save_soon()
