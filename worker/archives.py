"""Fetch REAL archival material — the documentary layer.

Wikimedia Commons (free API, no key): actual case photographs, portraits,
documents, newspaper scans for the real people/places/events in each
script. Every asset carries license + author for attribution (CC BY /
CC BY-SA / public domain — all monetization-safe).

This is the difference between "AI slop over stock video" and a curated
documentary: the viewer sees the REAL Isdal Woman belongings photo, the
REAL Dyatlov tent, the REAL newspaper front page.
"""
import hashlib
import json
import re
import time
from pathlib import Path

import requests

from common import CACHE, setup_logging

log = setup_logging("archives")

ARCHIVES = CACHE / "archives"
UA = {"User-Agent": "FOOTNOTE-channel-pipeline/1.0 (documentary research)"}

# licenses safe for monetized YouTube (everything on Commons is free,
# but we record the exact license string for the credits block)
OK_LICENSE_RE = re.compile(
    r"^(cc0|public domain|pd|copyrighted free use|cc by([-. ]sa)?[ 0-9.]*)",
    re.I)


def _cache_dir(query):
    d = ARCHIVES / hashlib.md5(query.lower().encode()).hexdigest()[:16]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clean(html):
    """Strip wiki markup from metadata strings."""
    s = re.sub(r"<[^>]+>", "", html or "")
    s = re.sub(r"\{\{[^}]*\}\}", "", s).strip()
    return s[:180]


def search_commons(query, max_images=4, min_width=640):
    """Search Wikimedia Commons for real photos matching a query.
    Returns [{title, url, page, license, author, path}] — files are
    downloaded once and cached per query."""
    d = _cache_dir(query)
    marker = d / "done.json"
    if marker.exists():
        cached = json.loads(marker.read_text(encoding="utf-8"))
        return [c for c in cached if Path(c["path"]).exists()]

    results = []
    try:
        r = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "format": "json",
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {query}",
                "gsrnamespace": 6, "gsrlimit": max_images * 3,
                "prop": "imageinfo",
                "iiprop": "url|size|mime|extmetadata",
                "iiurlwidth": 1920,
            },
            headers=UA, timeout=30)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
        for p in sorted(pages.values(),
                        key=lambda x: x.get("index", 99)):
            if len(results) >= max_images:
                break
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if (ii.get("width") or 0) < min_width:
                continue
            meta = ii.get("extmetadata") or {}
            lic = _clean((meta.get("LicenseShortName") or {})
                         .get("value", ""))
            if not OK_LICENSE_RE.match(lic):
                continue  # skip odd licenses (fair use etc.)
            author = _clean((meta.get("Artist") or {}).get("value", "")
                            or "unknown")
            url = ii.get("thumburl") or ii.get("url")
            if not url:
                continue
            dest = d / f"commons_{p['pageid']}.jpg"
            if not dest.exists():
                for attempt in (1, 2):
                    rr = requests.get(url, headers=UA, timeout=60)
                    if rr.status_code == 429 and attempt == 1:
                        time.sleep(3)  # wikimedia rate-limits bursts
                        continue
                    rr.raise_for_status()
                    break
                if len(rr.content) < 20_000:  # tiny/decorative junk
                    continue
                dest.write_bytes(rr.content)
            results.append({
                "title": _clean(p.get("title", "").replace("File:", "")),
                "page": ii.get("descriptionurl", ""),
                "license": lic, "author": author,
                "path": str(dest),
            })
            time.sleep(0.3)  # be polite to the API
    except Exception as e:
        log.warning("commons search failed for '%s': %s", query, e)

    marker.write_text(json.dumps(results), encoding="utf-8")
    if results:
        log.info("archives: %d real images for '%s'", len(results), query)
    return results
