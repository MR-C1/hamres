"""TikTok draft (inbox) posting for the brain.

TikTok's direct-post API forces unaudited apps to private-only posting,
and the audit requires a verified business entity — not realistic for a
solo ৳0 channel. The inbox flow instead drops the finished Short into
the owner's TikTok drafts: all the file-transfer and upload work is
done, the owner just taps publish in the app. That is the best TikTok
allows without an audit, so that is what we automate — no pretend
"posted publicly" claims the API can't back up.

Tokens: TikTok access tokens live 24h; the refresh token lives about a
year AND ROTATES on every refresh — the new one must be stored or the
whole grant dies. refresh() handles that; state holds the rotating pair,
env provides the initial values from extract_tiktok_token.py.
"""

import math
import os
import tempfile
import time

import requests

import config
import state

API = "https://open.tiktokapis.com/v2"
# TikTok requires chunks of at least 4 MB (the final chunk may be smaller)
CHUNK = 4 * 1024 * 1024


def _creds():
    t = state.STATE.get("tiktok") or {}
    return {
        "access": t.get("access_token") or config.TIKTOK_ACCESS_TOKEN,
        "refresh": t.get("refresh_token") or config.TIKTOK_REFRESH_TOKEN,
        "open_id": t.get("open_id") or config.TIKTOK_OPEN_ID,
    }


def configured():
    c = _creds()
    return bool(c["access"] and c["open_id"])


def refresh():
    """Exchange the refresh token for a fresh 24h access token. The
    refresh token ROTATES — store the new one or the grant is lost
    forever. Returns True on success."""
    c = _creds()
    if not (c["refresh"] and config.TIKTOK_CLIENT_KEY
            and config.TIKTOK_CLIENT_SECRET):
        return False
    try:
        r = requests.post(
            f"{API}/oauth/token/",
            data={"client_key": config.TIKTOK_CLIENT_KEY,
                  "client_secret": config.TIKTOK_CLIENT_SECRET,
                  "grant_type": "refresh_token",
                  "refresh_token": c["refresh"]},
            timeout=20)
        d = r.json().get("data") or {}
        if not d.get("access_token"):
            return False
        state.STATE["tiktok"] = {
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token", c["refresh"]),
            "open_id": d.get("open_id", c["open_id"]),
            "expires": time.time() + int(d.get("expires_in", 86400)),
        }
        state.save_soon()
        return True
    except Exception:
        return False


def _access():
    """The current access token, refreshed first when it is about to
    expire (TikTok's live 24h, so anything within an hour of the wall
    gets a fresh one rather than a dead call)."""
    c = _creds()
    if not c["access"]:
        return None
    exp = (state.STATE.get("tiktok") or {}).get("expires")
    if exp and exp - time.time() < 3600 and refresh():
        c = _creds()
    return c["access"]


def inbox_post(video_url):
    """Drop the video at `video_url` (a public GitHub release asset) into
    the owner's TikTok drafts. Downloads to a temp file — Render's disk
    is ephemeral but fine for a ~20 MB Short — then uploads in TikTok's
    required chunks. Returns (ok, detail); never raises, because
    cross-posting must never take the approval flow down."""
    tok = _access()
    if not tok:
        return False, "not configured"
    tmp_path = None
    try:
        with requests.get(video_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            tmp_path = tmp.name
            for chunk in r.iter_content(8 * 1024 * 1024):
                tmp.write(chunk)
            tmp.close()
        size = os.path.getsize(tmp_path)
        total = max(1, math.ceil(size / CHUNK))
        init = requests.post(
            f"{API}/post/publish/inbox/video/init/",
            headers={"Authorization": f"Bearer {tok}"},
            json={"source_info": {"source": "FILE_UPLOAD",
                                  "video_size": size,
                                  "chunk_size": CHUNK,
                                  "total_chunk_count": total}},
            timeout=30).json()
        data = init.get("data") or {}
        if not data.get("upload_url"):
            err = (init.get("error") or {}).get("message") or str(init)
            return False, str(err)[:200]
        with open(tmp_path, "rb") as f:
            for i in range(total):
                blob = f.read(CHUNK)
                lo, hi = i * CHUNK, i * CHUNK + len(blob) - 1
                up = requests.put(
                    data["upload_url"], data=blob,
                    headers={"Content-Type": "video/mp4",
                             "Content-Range": f"bytes {lo}-{hi}/{size}"},
                    timeout=180)
                if up.status_code not in (200, 201):
                    return False, (f"chunk {i + 1}/{total} "
                                   f"HTTP {up.status_code}")
        return True, "draft created in your TikTok inbox"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
