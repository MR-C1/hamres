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


def _download(url, dest):
    if dest.exists():
        return dest
    log.info("download: %s -> %s", url.split("?")[0][-60:], dest.name)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return dest


def fetch_clips(keyword, orientation="landscape", api_keys=None, max_clips=6):
    """Return local file paths of stock clips for a keyword (may be [])."""
    api_keys = api_keys or {}
    d = _cache_dir(keyword)
    marker = d / "done.json"
    if marker.exists():
        clips = sorted(p for p in d.glob("*.mp4"))
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
