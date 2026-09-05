"""Upload approved videos to YouTube via the Data API v3 (free, OAuth).

First run: a browser window opens → log into the Google account that owns
your channel → allow access. The token is saved to token.json and reused.

Usage:
    python upload.py            # uploads everything in output/approved/
    python upload.py --dry-run  # shows what would upload, does nothing
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from common import (APPROVED, ROOT, load_config, load_state, save_state,
                    setup_logging)

log = setup_logging("upload")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]


def get_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    secret = ROOT / "client_secret.json"
    if not secret.exists() or not secret.read_text(encoding="utf-8").strip():
        raise FileNotFoundError(
            "client_secret.json not found or empty. Follow the Google Cloud "
            "steps in START_HERE.md (enable YouTube Data API v3, create OAuth "
            "Desktop client, download the JSON here).")

    token_path = ROOT / "token.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return build("youtube", "v3", credentials=creds)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return build("youtube", "v3", credentials=creds)
    # no usable token: the interactive browser flow only works on a
    # desktop with a human — on a headless runner it would hang forever
    # waiting for a browser that can't open, burning the whole job window
    import os
    if os.environ.get("WORKER_MAX_MINUTES"):
        raise RuntimeError(
            "YouTube token missing/unrefreshable and no browser available "
            "on this headless runner — regenerate token.json on the PC and "
            "update the YT_TOKEN secret.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def set_thumbnail(video_url, thumb_path, config=None):
    """Attach a custom thumbnail to an uploaded video (long-form only —
    Shorts ignore thumbnails)."""
    yt = get_service()
    video_id = video_url.rstrip("/").split("/")[-1]
    yt.thumbnails().set(videoId=video_id,
                        media_body=str(thumb_path)).execute()


def make_public(video_url):
    """Flip a private upload to public (used when the owner taps ✅ —
    the video is already safely on YouTube by then)."""
    yt = get_service()
    video_id = video_url.rstrip("/").split("/")[-1]
    v = yt.videos().list(part="status", id=video_id).execute()
    if not v.get("items"):
        raise RuntimeError(f"video {video_id} not found")
    status = v["items"][0]["status"]
    status["privacyStatus"] = "public"
    status.pop("publishAt", None)  # a schedule is meaningless once public
    yt.videos().update(part="status", body={
        "id": video_id, "status": status}).execute()


def delete_video(video_url):
    """Delete an uploaded video (the owner's ❌ on an already-uploaded
    preview). Returns True if it was deleted or already gone."""
    yt = get_service()
    video_id = video_url.rstrip("/").split("/")[-1]
    try:
        yt.videos().delete(id=video_id).execute()
        return True
    except Exception:
        # already deleted (double-tap) — check existence
        v = yt.videos().list(part="id", id=video_id).execute()
        return not v.get("items")


def parse_metadata(meta_path):
    meta = {}
    for line in meta_path.read_text(encoding="utf-8").splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            meta[k.strip()] = v.strip()
    return meta


def validate_video(mp4):
    """Full-decode check before upload — a truncated/corrupt render
    would otherwise land on YouTube as an unwatchable video. Cheap:
    decodes once, no output file. Raises on failure."""
    import subprocess
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(mp4), "-f", "null", "-"],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or r.stderr.strip():
        # a few muxing notes are normal; real errors fail the decode
        errors = [l for l in r.stderr.strip().splitlines()
                  if "error" in l.lower() or r.returncode != 0]
        if errors:
            raise RuntimeError(f"video validation failed for {mp4.name}: "
                               f"{'; '.join(errors[:3])[:200]}")


def _iso_duration_seconds(iso):
    """PT#H#M#S -> seconds (YouTube's contentDetails duration format)."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return None
    h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + sec


def find_existing(title, want_short=None, yt=None):
    """Duplicate-upload guard: if the channel already has a video with
    this title AND the right format, return its URL instead of
    uploading a second copy. The short and the long share a title, so
    they are told apart by duration bucket (< 3 min = short) — absolute
    seconds proved brittle (an 8-min long and an 11-min long differ)."""
    yt = yt or get_service()
    norm = " ".join(title.lower().split())
    try:
        ch = yt.channels().list(part="contentDetails", mine=True).execute()
        uploads = (ch["items"][0]["contentDetails"]
                   .get("relatedPlaylists", {}).get("uploads", ""))
        if not uploads:
            return None
        r = yt.playlistItems().list(part="snippet,contentDetails",
                                    playlistId=uploads, maxResults=50).execute()
        ids = [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        if not ids:
            return None
        parts = "snippet" + (",contentDetails" if want_short is not None else "")
        v = yt.videos().list(part=parts, id=",".join(ids)).execute()
        for item in v.get("items", []):
            if " ".join(item["snippet"]["title"].lower().split()) != norm:
                continue
            if want_short is None:
                return f"https://youtu.be/{item['id']}"
            dur = _iso_duration_seconds(
                item.get("contentDetails", {}).get("duration", ""))
            if dur is not None and ((dur < 180) == want_short):
                return f"https://youtu.be/{item['id']}"
    except Exception as e:
        log.warning("duplicate-check failed (continuing): %s", e)
    return None


def upload_video(mp4, meta, config, publish_hour=None, privacy=None,
                  want_short=None):
    """Upload one video file, PRIVATE with no schedule — approval-first:
    only the owner's ✅ (brain → yt.make_public) ever makes it public.
    Returns the video URL. Used by the agent worker and the run() flow."""
    yt = get_service()
    uconf = config.get("upload", {})
    title = meta.get("title", Path(mp4).stem)[:100]

    # reconciliation: never upload a title+format that already exists
    # on the channel (crash + re-queue used to produce exact duplicates)
    existing = find_existing(title, want_short, yt)
    if existing:
        log.info("  '%s' already on channel — reusing %s", title[:50], existing)
        return existing

    validate_video(mp4)

    body = {
        "snippet": {
            "title": title,
            "description": meta.get("description", ""),
            "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
            "categoryId": uconf.get("category_id", "27"),
        },
        "status": {
            "privacyStatus": privacy or uconf.get("privacy", "private"),
            "selfDeclaredMadeForKids": uconf.get("made_for_kids", False),
            # honest AI disclosure: synthetic voiceover + stock assembly
            "containsSyntheticMedia": True,
        },
    }

    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(mp4), chunksize=8 << 20, resumable=True,
                            mimetype="video/mp4")
    request = yt.videos().insert(part="snippet,status", body=body,
                                 media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("  uploading %s: %d%%", Path(mp4).name,
                     int(status.progress() * 100))
    return f"https://youtu.be/{response.get('id')}"


def upload_file(yt, mp4, meta, config):
    uconf = config.get("upload", {})
    body = {
        "snippet": {
            "title": meta.get("title", mp4.stem),
            "description": meta.get("description", ""),
            "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
            "categoryId": uconf.get("category_id", "27"),
        },
        "status": {
            "privacyStatus": uconf.get("privacy", "private"),
            "selfDeclaredMadeForKids": uconf.get("made_for_kids", False),
        },
    }
    # schedule publication later today at the configured hour
    hour = int(str(uconf.get("schedule_at", "17:00")).split(":")[0])
    pub = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    if pub <= datetime.now():
        pub += timedelta(days=1)
    if body["status"]["privacyStatus"] == "private":
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = pub.strftime("%Y-%m-%dT%H:%M:%S+06:00")

    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(mp4), chunksize=8 << 20, resumable=True,
                            mimetype="video/mp4")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("  uploading %s: %d%%", mp4.name, int(status.progress() * 100))
    return response


def run(dry_run=False):
    config = load_config()
    state = load_state()
    if not any(APPROVED.glob("*.mp4")):
        log.info("nothing in output/approved/ to upload")
        return

    yt = None
    if not dry_run:
        yt = get_service()

    for mp4 in sorted(APPROVED.glob("*.mp4")):
        sid = mp4.stem.rsplit("_", 1)[0]
        if f"{mp4.stem}" in state["uploaded"]:
            log.info("already uploaded, skipping: %s", mp4.name)
            continue
        meta_path = APPROVED / f"{sid}_metadata.txt"
        meta = parse_metadata(meta_path) if meta_path.exists() else {}
        # thumbnail for long-form
        thumb = APPROVED / f"{sid}_thumb.png"

        log.info("uploading %s ...", mp4.name)
        if dry_run:
            log.info("  [dry-run] title: %s | privacy: %s",
                     meta.get("title", "?"), config.get("upload", {}).get("privacy"))
            continue
        try:
            resp = upload_file(yt, mp4, meta, config)
            vid = resp.get("id")
            log.info("  uploaded: https://youtu.be/%s", vid)
            if thumb.exists() and vid and mp4.stem.endswith("_long"):
                try:
                    yt.thumbnails().set(videoId=vid, media_body=str(thumb)).execute()
                    log.info("  thumbnail set")
                except Exception as e:
                    log.warning("  thumbnail failed: %s", e)
            state["uploaded"].append(mp4.stem)
            save_state(state)
            mp4.unlink()  # remove uploaded file so it can't double-upload
        except Exception as e:
            log.error("  upload failed for %s: %s", mp4.name, e)


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
