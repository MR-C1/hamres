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
    state.save_soon()
    return job


def next_job():
    """Claim the oldest pending job for the worker. A job claimed but not
    reported on for 40 minutes goes back to pending (worker crashed)."""
    now = time.time()
    for job in state.STATE["jobs"]:
        if job["status"] == "claimed" and now - job["updated"] > 2400:
            job["status"] = "pending"
            job["updated"] = now
            state.save_soon()
    for job in state.STATE["jobs"]:
        if job["status"] == "pending":
            job["status"] = "claimed"
            job["updated"] = now
            state.save_soon()
            return job
    return None


def complete_job(job_id, result):
    for job in state.STATE["jobs"]:
        if job["id"] == job_id:
            job["status"] = "done" if result.get("ok") else "failed"
            job["updated"] = time.time()
            job["result"] = {k: result.get(k) for k in
                             ("msg", "video_url", "files")}
            state.save_soon()
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
