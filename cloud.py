"""Wake the GitHub Actions render worker the moment a job is queued.

The workflow (.github/workflows/render-worker.yml) listens for a
repository_dispatch event of type "render-wake". The brain calls
wake_cloud_worker() right after queueing a render (or upload) job, so
the cloud runner starts within seconds instead of waiting for the next
cron slot (09:23 / 17:23 / 01:23 Dhaka).

Why not workflow_dispatch: it can only be triggered with a token that
has actions:write on the repo. repository_dispatch is the same
permission but lets us pass a payload, and it's the standard
"service-to-service nudge" pattern.

Fails silently by design — if the token is missing or GitHub is down,
nothing breaks: the cron schedule still fires 3x daily and the PC worker
still polls. This is an accelerator, not a dependency.
"""

import threading

import requests

import comms
import config

DISPATCHED = {}  # debounce: event kind -> monotonic time
_lock = threading.Lock()


def _already_dispatched_recently(kind, seconds=300):
    """The workflow takes a few seconds to spin up; queueing two jobs in
    a burst doesn't need two dispatches. One wake-up every 5 min is plenty."""
    import time
    with _lock:
        now = time.monotonic()
        last = DISPATCHED.get(kind, 0)
        if now - last < seconds:
            return True
        DISPATCHED[kind] = now
        return False


def wake_cloud_worker(kind="render"):
    """Fire the render-wake event. Non-blocking, never raises."""
    if not config.GITHUB_DISPATCH_TOKEN:
        return  # not configured — cron + PC worker still cover everything
    if _already_dispatched_recently(kind):
        return
    try:
        r = requests.post(
            f"https://api.github.com/repos/{config.GITHUB_REPO}/dispatches",
            headers={"Authorization": f"Bearer {config.GITHUB_DISPATCH_TOKEN}",
                     "Accept": "application/vnd.github+json"},
            json={"event_type": "render-wake",
                  "client_payload": {"kind": kind}},
            timeout=15)
        if r.status_code == 204:
            comms.log(f"cloud worker woke ({kind} job queued)")
        else:
            # 204 is the only success code; anything else is worth a line
            # in the log but not a Telegram alarm
            comms.log(f"cloud wake failed: HTTP {r.status_code}")
    except Exception as e:
        comms.log(f"cloud wake failed: {str(e)[:80]}")


def wake_soon(kind="render"):
    """Fire-and-forget from request handlers so they never wait on GitHub."""
    threading.Thread(target=wake_cloud_worker, args=(kind,),
                     daemon=True).start()
