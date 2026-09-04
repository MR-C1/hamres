"""Fetch free stock video clips from Pexels (primary) or Pixabay (fallback).

Clips are cached in cache/clips/<keyword-hash>/ so each clip downloads once
and is reused across videos. If no API key is configured (or all sources
fail), a generated gradient clip keeps rendering working — good for testing.
"""
import hashlib
import json
import time
from pathlib import Path

import requests

from common import CACHE, setup_logging

log = setup_logging("visuals")

CLIPS = CACHE / "clips"

# Generic/abstract terms return random popular footage on Pexels, not
# scene-matching footage. Map them to concrete, filmable subjects. The
# keywords from well-written scripts never hit this table.
GENERIC_KEYWORDS = {
    # abstractions → their concrete consequence
    "mystery": "dark abandoned hallway",
    "mysterious": "dark abandoned hallway",
    "history": "ancient ruins stones",
    "historical": "ancient ruins stones",
    "science": "laboratory microscope closeup",
    "facts": "library books closeup",
    "time": "clock hands closeup",
    "time lapse": "clouds time lapse sky",
    "strange": "foggy empty street night",
    "weird": "foggy empty street night",
    "unknown": "foggy empty road",
    "unexplained": "foggy empty road",
    "secret": "locked old door",
    "hidden": "locked old door",
    "mystery history": "ancient ruins stones",
    # vague people/places
    "people": "crowd walking street",
    "person": "man standing silhouette",
    "man": "man standing silhouette",
    "woman": "woman standing silhouette",
    "city": "city aerial skyline",
    "world": "earth from space",
    "earth": "earth from space",
    "space": "stars galaxy night sky",
    "ocean": "ocean waves aerial",
    "night": "night city lights",
    "day": "sunny sky clouds",
    "old": "antique object closeup",
    "ancient": "ancient ruins stones",
    "story": "old book pages turning",
    "book": "old book pages turning",
    "death": "candle burning dark",
    "died": "candle burning dark",
    "illness": "person lying bed sick",
    "doctor": "doctor hospital corridor",
    "food": "food closeup table",
    "animal": "wild animal closeup",
    "water": "river flowing closeup",
    "fire": "fire flames closeup",
    "dark": "dark forest night",
    "light": "sun rays through window",
    "video": "film camera reel",
    "money": "cash bills closeup",
    "war": "soldiers marching historical",
    "music": "orchestra playing instruments",
    "dance": "people dancing crowd",
    "dancing": "people dancing crowd",
    "study": "researcher writing notes",
    "research": "laboratory microscope closeup",
    "brain": "human brain anatomy 3d",
    "mind": "human brain anatomy 3d",
    "body": "human anatomy 3d render",
    "eye": "human eye macro closeup",
    "hand": "human hand closeup macro",
    "face": "human face closeup portrait",
}


def _concrete(keyword):
    """Translate a generic keyword into a concrete filmable search.
    Two-word+ keywords that aren't in the table pass through — the script
    prompt already pushes Gemini toward specific phrases."""
    k = keyword.strip().lower()
    if k in GENERIC_KEYWORDS:
        return GENERIC_KEYWORDS[k]
    words = k.split()
    # one generic word alone ("history", "people") → expand
    if len(words) == 1 and words[0] in GENERIC_KEYWORDS:
        return GENERIC_KEYWORDS[words[0]]
    return k


def _cache_dir(keyword):
    d = CLIPS / hashlib.md5(keyword.lower().encode()).hexdigest()[:16]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pexels_search(keyword, api_key, orientation, per_page):
    r = requests.get(
        "https://api.pexels.com/videos/search",
        params={"query": keyword, "per_page": per_page,
                "orientation": "portrait" if orientation == "portrait" else "landscape"},
        headers={"Authorization": api_key},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("videos", [])


def _pexels_best_file(video, orientation):
    # prefer hd files >= 1080 on the short side
    files = [f for f in video.get("video_files", []) if f.get("quality") == "hd"]
    if not files:
        files = video.get("video_files", [])
    want_w = 1080 if orientation == "portrait" else 1920
    files = [f for f in files if (f.get("width") or 0) >= 720]
    files.sort(key=lambda f: abs((f.get("width") or 0) - want_w))
    return files[0]["link"] if files else None


def _pixabay_search(keyword, api_key, orientation, per_page):
    r = requests.get(
        "https://pixabay.com/api/videos/",
        params={"key": api_key, "q": keyword, "per_page": per_page,
                "video_type": "all"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("hits", [])


def _fmt_mb(n):
    return f"{n / (1 << 20):.1f}MB"


def _fmt_eta(seconds):
    if seconds != seconds or seconds == float("inf"):  # nan/inf guard
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m{seconds % 60:02.0f}s"


def _download(url, dest):
    if dest.exists():
        # cached — but only if a previous completed download marked it
        # good; a file left by a killed run is a truncated clip that
        # would be reused forever (partial files are indistinguishable
        # from complete ones by name alone)
        ok_marker = dest.with_suffix(".ok")
        if ok_marker.exists():
            return dest
        dest.unlink()  # unverified/stale — re-download
    log.info("download: %s -> %s", url.split("?")[0][-60:], dest.name)
    t0 = time.monotonic()
    last_log = t0
    done = 0
    tmp = dest.with_suffix(".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                # progress line at most once per second
                if now - last_log >= 1 and total:
                    speed = done / (now - t0)  # bytes/sec (average so far)
                    pct = done / total * 100
                    eta = (total - done) / speed if speed else float("inf")
                    log.info("  %s/%s (%.0f%%) %.1fMB/s ETA %s — %s",
                             _fmt_mb(done), _fmt_mb(total), pct,
                             speed / (1 << 20), _fmt_eta(eta), dest.name)
                    last_log = now
    if total and done != total:
        log.warning("  size mismatch: got %s of %s — discarding %s",
                    _fmt_mb(done), _fmt_mb(total), dest.name)
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    dest.with_suffix(".ok").write_text(str(done))
    log.info("  done: %s in %.1fs (avg %.1fMB/s) — %s",
             _fmt_mb(done), time.monotonic() - t0,
             done / max(time.monotonic() - t0, 0.001) / (1 << 20),
             dest.name)
    return dest


def fetch_clips(keyword, orientation="landscape", api_keys=None, max_clips=6):
    """Return local file paths of stock clips for a keyword (may be [])."""
    api_keys = api_keys or {}
    keyword = _concrete(keyword)
    d = _cache_dir(keyword)
    marker = d / "done.json"
    if marker.exists():
        # only verified-complete clips count (the .ok sidecar is written
        # when a download finished and matched Content-Length)
        clips = sorted(p for p in d.glob("*.mp4")
                       if p.with_suffix(".ok").exists())
        if clips:
            return clips[:max_clips]

    results = []
    # --- Pexels ---
    if api_keys.get("pexels"):
        try:
            for v in _pexels_search(keyword, api_keys["pexels"], orientation, max_clips):
                link = _pexels_best_file(v, orientation)
                if link:
                    dest = d / f"pexels_{v['id']}.mp4"
                    if _download(link, dest):
                        results.append(dest)
                if len(results) >= max_clips:
                    break
        except Exception as e:
            log.warning("Pexels failed for '%s': %s", keyword, e)

    # --- Pixabay fallback ---
    if not results and api_keys.get("pixabay"):
        try:
            for v in _pixabay_search(keyword, api_keys["pixabay"], orientation, max_clips):
                variants = v.get("videos", {})
                files = list(variants.values())
                files.sort(key=lambda f: f.get("width") or 0, reverse=True)
                if files:
                    dest = d / f"pixabay_{v['id']}.mp4"
                    if _download(files[0]["url"], dest):
                        results.append(dest)
                if len(results) >= max_clips:
                    break
        except Exception as e:
            log.warning("Pixabay failed for '%s': %s", keyword, e)

    marker.write_text(json.dumps({"fetched": time.time(), "count": len(results)}))
    return results[:max_clips]
