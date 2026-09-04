"""YouTube API access for the brain, authenticated with a refresh token
(one-time extraction via extract_refresh_token.py on the PC).

Covers: channel stats, own-video listing with statistics, title updates,
comment reading and replies. Heavy operations (upload) stay on the PC.
"""

import config

_service = None


def get_service():
    global _service
    if _service is not None:
        return _service
    if not config.YT_REFRESH_TOKEN:
        raise RuntimeError("YT_REFRESH_TOKEN not set — run "
                           "extract_refresh_token.py and add it on Render")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=config.YT_REFRESH_TOKEN,
        client_id=config.YT_CLIENT_ID,
        client_secret=config.YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=config.YT_SCOPES,
    )
    creds.refresh(Request())
    _service = build("youtube", "v3", credentials=creds)
    return _service


def channel_stats():
    """Channel-level numbers: subscribers, views, video count."""
    yt = get_service()
    r = yt.channels().list(
        part="snippet,statistics,contentDetails", mine=True).execute()
    items = r.get("items", [])
    if not items:
        return None
    c = items[0]
    st = c.get("statistics", {})
    return {
        "title": c["snippet"]["title"],
        "subs": int(st.get("subscriberCount", 0)),
        "views": int(st.get("viewCount", 0)),
        "videos": int(st.get("videoCount", 0)),
        # uploads playlist — some channels hide it, so .get() not []
        "uploads_playlist": (c.get("contentDetails", {})
                             .get("relatedPlaylists", {})
                             .get("uploads", "")),
    }


def my_videos(max_results=30):
    """Own videos with statistics, newest first. Uses the uploads playlist
    (cheap) instead of search.list (100 quota units)."""
    yt = get_service()
    ch = channel_stats()
    if not ch:
        return []
    r = yt.playlistItems().list(
        part="snippet,contentDetails",
        playlistId=ch["uploads_playlist"], maxResults=max_results,
    ).execute()
    ids = [i["contentDetails"]["videoId"] for i in r.get("items", [])]
    if not ids:
        return []
    v = yt.videos().list(part="snippet,statistics,status",
                         id=",".join(ids)).execute()
    out = []
    for item in v.get("items", []):
        st = item.get("statistics", {})
        out.append({
            "id": item["id"],
            "title": item["snippet"]["title"],
            "published": item["snippet"]["publishedAt"][:10],
            "views": int(st.get("viewCount", 0)),
            "likes": int(st.get("likeCount", 0)),
            "comments": int(st.get("commentCount", 0)),
            "privacy": item.get("status", {}).get("privacyStatus", ""),
            "publish_at": item.get("status", {}).get("publishAt", ""),
        })
    return out


def update_title(video_id, new_title, category_id="27"):
    """Rewrite a video's title (keeps description and tags intact)."""
    yt = get_service()
    v = yt.videos().list(part="snippet", id=video_id).execute()
    items = v.get("items", [])
    if not items:
        raise RuntimeError(f"video {video_id} not found")
    snip = items[0]["snippet"]
    snip["title"] = new_title[:100]
    snip["categoryId"] = snip.get("categoryId") or category_id
    yt.videos().update(part="snippet", body={
        "id": video_id, "snippet": snip}).execute()
    return new_title


def new_comments(video_ids, already_replied):
    """Recent top-level comments on the given videos, skipping ones we
    already handled. Returns [{comment_id, text, author, video_id,
    video_title, published}]."""
    yt = get_service()
    out = []
    for vid in video_ids[:5]:  # quota-friendly: newest 5 videos
        try:
            r = yt.commentThreads().list(
                part="snippet", videoId=vid,
                order="time", maxResults=15,
            ).execute()
        except Exception:
            continue  # video has comments disabled
        for thread in r.get("items", []):
            top = thread["snippet"]["topLevelComment"]
            cid = top["id"]
            if cid in already_replied:
                continue
            out.append({
                "comment_id": cid,
                "text": top["snippet"]["textDisplay"][:500],
                "text_plain": top["snippet"]["textOriginal"][:500],
                "author": top["snippet"]["authorDisplayName"],
                "published": top["snippet"]["publishedAt"][:10],
            })
    return out


def reply_to_comment(comment_id, text):
    """Post a public reply under a top-level comment on our own video."""
    yt = get_service()
    yt.comments().insert(part="snippet", body={
        "snippet": {
            "parentId": comment_id,
            "textOriginal": text,
        }
    }).execute()


def make_private(video_url):
    """Strip any publishAt schedule and keep the video private — for
    approval-first cleanups of videos uploaded with the old scheduled
    flow (they'd otherwise go public on their own)."""
    yt = get_service()
    video_id = video_url.rstrip("/").split("/")[-1]
    v = yt.videos().list(part="status", id=video_id).execute()
    if not v.get("items"):
        raise RuntimeError(f"video {video_id} not found")
    status = v["items"][0]["status"]
    status["privacyStatus"] = "private"
    status.pop("publishAt", None)
    yt.videos().update(part="status", body={
        "id": video_id, "status": status}).execute()


def make_public(video_url):
    """Flip a private upload to public (owner's ✅ on an upload-first
    preview — the video already sits safely on YouTube)."""
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
    """Delete an uploaded video (owner's ❌). True if deleted or gone."""
    yt = get_service()
    video_id = video_url.rstrip("/").split("/")[-1]
    try:
        yt.videos().delete(id=video_id).execute()
        return True
    except Exception:
        v = yt.videos().list(part="id", id=video_id).execute()
        return not v.get("items")
