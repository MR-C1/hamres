"""Instagram Reels publishing for the brain — "Instagram API with
Instagram Login" (graph.instagram.com).

Why the brain and not the worker: Instagram's publishing API fetches the
video from a public URL (it accepts no file upload on this login path),
and the worker already stages each Short at one (a GitHub release asset,
see worker/ghassets.py). The long-lived token lives in the gist state —
env vars are immutable at runtime, so refreshed copies must persist
somewhere the next boot reads, which is exactly what the gist is for.

Token lifecycle: extract_ig_token.py provides a 60-day long-lived token;
refresh_token() resets it to a fresh 60 days whenever it is at least 24h
old. The daily scheduler calls it, so the token never expires while the
brain wakes at least once a day.
"""

import time

import requests

import config
import state

API = "https://graph.instagram.com/v21.0"

sleep = time.sleep  # module-level so tests can skip the polling delays


def _token():
    # state first (may hold a refreshed copy), env as the initial value
    return (state.STATE.get("ig") or {}).get("token") or config.IG_ACCESS_TOKEN


def _user_id():
    return ((state.STATE.get("ig") or {}).get("user_id")
            or config.IG_USER_ID)


def configured():
    return bool(_token() and _user_id())


def refresh_token():
    """Reset the 60-day expiry window (allowed once the token is 24h+
    old). Returns True only when the stored token actually changed, so
    the caller can tell a silent no-op from a dead grant."""
    tok = _token()
    if not tok:
        return False
    try:
        r = requests.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": tok},
            timeout=20)
        d = r.json()
        if "access_token" not in d:
            return False
        state.STATE["ig"] = {
            "token": d["access_token"],
            "user_id": _user_id(),
            # default 60 days; refreshable again after 24h
            "expires": time.time() + int(d.get("expires_in", 5184000)),
        }
        state.save_soon()
        return True
    except Exception:
        return False


def publish_reel(video_url, caption):
    """Publish one Reel from a public video URL. Three steps: create the
    media container, wait for Instagram to fetch and process the video
    (it downloads asynchronously — typically seconds for a 20 MB Short),
    then publish. Returns (ok, reel_link_or_reason); every failure is a
    clean (False, reason) because cross-posting must never take the
    approval flow down. Bounded ~3-minute worst case; the winner path is
    one container poll."""
    tok, uid = _token(), _user_id()
    if not (tok and uid):
        return False, "not configured"
    try:
        r = requests.post(f"{API}/{uid}/media",
                          data={"media_type": "REELS",
                                "video_url": video_url,
                                "caption": caption[:2200],
                                "access_token": tok},
                          timeout=30)
        d = r.json()
        if "id" not in d:
            err = d.get("error", {}).get("message") or str(d)
            return False, str(err)[:200]
        cid = d["id"]
        for _ in range(30):          # 30 x 6s ≈ 3 min ceiling
            sleep(6)
            s = requests.get(f"{API}/{cid}",
                             params={"fields": "status_code",
                                     "access_token": tok},
                             timeout=15).json()
            st = s.get("status_code")
            if st == "FINISHED":
                break
            if st == "ERROR":
                return False, "Instagram could not process the video"
        else:
            return False, "processing timed out (try again later)"
        p = requests.post(f"{API}/{uid}/media_publish",
                          data={"creation_id": cid, "access_token": tok},
                          timeout=30).json()
        if "id" not in p:
            err = p.get("error", {}).get("message") or str(p)
            return False, str(err)[:200]
        return True, f"https://www.instagram.com/reel/{p['id']}/"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
