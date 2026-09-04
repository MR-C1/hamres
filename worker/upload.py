"""Upload approved videos to YouTube via the Data API v3 (free, OAuth).

First run: a browser window opens → log into the Google account that owns
your channel → allow access. The token is saved to token.json and reused.

Usage:
    python upload.py            # uploads everything in output/approved/
    python upload.py --dry-run  # shows what would upload, does nothing
"""
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


def parse_metadata(meta_path):
    meta = {}
    for line in meta_path.read_text(encoding="utf-8").splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            meta[k.strip()] = v.strip()
    return meta


def upload_video(mp4, meta, config, publish_hour=17, privacy=None):
    """Upload one video file. Private + scheduled at publish_hour by
    default (never instant-public). Returns the video URL. Used by the
    agent worker and the run() flow."""
    yt = get_service()
    uconf = config.get("upload", {})
    body = {
        "snippet": {
            "title": meta.get("title", Path(mp4).stem)[:100],
            "description": meta.get("description", ""),
            "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
            "categoryId": uconf.get("category_id", "27"),
        },
        "status": {
            "privacyStatus": privacy or uconf.get("privacy", "private"),
            "selfDeclaredMadeForKids": uconf.get("made_for_kids", False),
        },
    }
    if body["status"]["privacyStatus"] == "private":
        pub = datetime.now().replace(hour=int(publish_hour), minute=0,
                                     second=0, microsecond=0)
        if pub <= datetime.now():
            pub += timedelta(days=1)
        body["status"]["publishAt"] = pub.strftime("%Y-%m-%dT%H:%M:%S+06:00")

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
