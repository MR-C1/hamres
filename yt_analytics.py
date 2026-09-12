"""YouTube Analytics API (v2) — the learning loop's eyes.

yt.py (Data API) counts views and likes; it cannot see retention or
watch time — the numbers that actually say which format works. Those
live here, behind the yt-analytics.readonly scope, which is a SEPARATE
consent from the upload token. Until the owner re-runs the (updated)
extract_refresh_token.py, this module degrades to "analytics
unavailable" and everything else keeps working.

Error contract: video_report() NEVER raises — it returns
(report_dict, reason) so a missing consent can never kill the daily
report. reason says what the owner must do:
  "ok"       — report has data
  "empty"    — API works, no rows in range yet
  "scope"    — re-run extract_refresh_token.py (it now asks for
               analytics) and replace YT_REFRESH_TOKEN on Render
  "disabled" — enable the "YouTube Analytics API" in the Google Cloud
               project (console.cloud.google.com → APIs & Services)
"""

import config

SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

_service = None


def get_service():
    global _service
    if _service is not None:
        return _service
    if not config.YT_REFRESH_TOKEN:
        raise RuntimeError("YT_REFRESH_TOKEN not set")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    # deliberately NOT config.YT_SCOPES: the upload token was consented
    # for force-ssl + readonly only, and asking a refresh token for a
    # scope it never got is an instant invalid_scope. If that happens
    # here it is caught by the caller — yt.py's flows stay untouched.
    creds = Credentials(
        token=None,
        refresh_token=config.YT_REFRESH_TOKEN,
        client_id=config.YT_CLIENT_ID,
        client_secret=config.YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[SCOPE],
    )
    creds.refresh(Request())
    # static_discovery=False: not every google-api-python-client build
    # bundles a youtubeanalytics v2 discovery doc (Render's cached one
    # raised UnknownApiNameOrVersion — "name: youtubeanalytics  version:
    # v2"), so fetch the document remotely. One HTTPS GET per process;
    # _service caches the built client after that.
    _service = build("youtubeanalytics", "v2", credentials=creds,
                     static_discovery=False)
    return _service


_last_error = ""


def last_error():
    """The text behind the most recent 'other' — for the panel to quote,
    so a garbled env value says what Google actually said."""
    return _last_error


def _classify(e):
    """Turn any failure into the owner-action reason."""
    global _last_error
    msg = str(e)
    _last_error = msg[:160]
    if "invalid_scope" in msg or "insufficient" in msg.lower():
        return "scope"
    if "has not been used" in msg or "is disabled" in msg \
            or "SERVICE_DISABLED" in msg:
        return "disabled"
    return "other"


def video_report(days=28):
    """Per-video analytics for the last N days, keyed by video id:
    {id: {views, minutes, avg_duration, avg_pct, subs_gained,
          impressions?, ctr?}}. Returns (report, reason)."""
    from datetime import date, timedelta
    end = date.today() - timedelta(days=2)  # YT analytics lands ~2-3d late
    start = end - timedelta(days=days)
    core = ("views,estimatedMinutesWatched,averageViewDuration,"
            "averageViewPercentage,subscribersGained")
    try:
        r = get_service().reports().query(
            ids="channel==MINE",
            startDate=start.isoformat(), endDate=end.isoformat(),
            metrics=core, dimensions="video",
            sort="-views", maxResults=50).execute()
    except Exception as e:
        return {}, _classify(e)

    def rows_as_dicts(resp):
        headers = [c["name"] for c in resp.get("columnHeaders", [])]
        return [dict(zip(headers, row)) for row in resp.get("rows", [])]

    out = {}
    for d in rows_as_dicts(r):
        out[d["video"]] = {
            "views": int(d.get("views", 0) or 0),
            "minutes": float(d.get("estimatedMinutesWatched", 0) or 0),
            "avg_duration": float(d.get("averageViewDuration", 0) or 0),
            "avg_pct": float(d.get("averageViewPercentage", 0) or 0),
            "subs_gained": int(d.get("subscribersGained", 0) or 0),
        }
    # impressions/CTR are not exposed to every channel — feature-detect
    # them with a second query and drop them silently when refused
    try:
        r2 = get_service().reports().query(
            ids="channel==MINE",
            startDate=start.isoformat(), endDate=end.isoformat(),
            metrics="impressions,clickThroughRate",
            dimensions="video", sort="-impressions",
            maxResults=50).execute()
        for d in rows_as_dicts(r2):
            vid = d.get("video")
            if vid in out:
                out[vid]["impressions"] = int(d.get("impressions", 0) or 0)
                out[vid]["ctr"] = float(d.get("clickThroughRate", 0) or 0)
    except Exception:
        pass
    return out, ("ok" if out else "empty")


def retention_curve(video_id):
    """The elapsed-time retention curve for ONE video: up to 100 points
    of {elapsed, watching} — position in the video (0..1 of the
    timeline) vs share of the audience still watching at that position
    (rewinds can push it over 1.0). This is the curve YouTube Studio
    draws; it is what makes scene-aware retention possible (project it
    onto the scene timeline and each scene's drop becomes visible).

    Same error contract as video_report: returns (points, reason) and
    never raises. reason "empty" also covers too-few-points curves
    (a video with barely any views returns a sparse or empty set)."""
    from datetime import date, timedelta
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=28)
    try:
        r = get_service().reports().query(
            ids="channel==MINE",
            startDate=start.isoformat(), endDate=end.isoformat(),
            metrics="audienceWatchRatio",
            dimensions="elapsedVideoTimeRatio",
            filters=f"video=={video_id}",
            sort="elapsedVideoTimeRatio", maxResults=100).execute()
    except Exception as e:
        return [], _classify(e)
    headers = [c["name"] for c in r.get("columnHeaders", [])]
    pts = []
    for row in r.get("rows", []):
        d = dict(zip(headers, row))
        try:
            elapsed = float(d["elapsedVideoTimeRatio"])
            watching = float(d["audienceWatchRatio"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < elapsed <= 1 and watching >= 0:
            pts.append({"elapsed": elapsed, "watching": watching})
    pts.sort(key=lambda p: p["elapsed"])
    return pts, ("ok" if len(pts) >= 10 else "empty")
