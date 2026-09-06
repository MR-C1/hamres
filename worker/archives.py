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

# polite global pacing between ALL Commons requests (searches AND
# downloads): a full render fires dozens of them and Wikimedia's
# per-client burst limiter is strict
_last_commons_req = [0.0]
COMMONS_MIN_INTERVAL = 1.0  # seconds between Commons hits


def _commons_pace():
    wait = COMMONS_MIN_INTERVAL - (time.monotonic() - _last_commons_req[0])
    if wait > 0:
        time.sleep(wait)
    _last_commons_req[0] = time.monotonic()

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


def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 3]


def _relevance(title, query):
    """How well a result's own title matches what we searched for, 0-2.

    Search engines match descriptions and categories too, so a hit whose
    title shares no word with the query is usually about something else —
    the wrong photo over the right narration is the single most damaging
    thing this pipeline can do. Ranking by title overlap (and rewarding a
    whole-phrase hit) puts the genuinely-about-this material first.
    """
    qw = set(_words(query))
    if not qw:
        return 0.0
    tl = (title or "").lower()
    score = len(qw & set(_words(tl))) / len(qw)
    if query.lower().strip() in tl:
        score += 1.0
    return score


def search_commons(query, max_images=8, min_width=640):
    """Search Wikimedia Commons for real photos matching a query.
    Returns [{title, url, page, license, author, path}] — files are
    downloaded once and cached per query, best match first."""
    d = _cache_dir(query)
    marker = d / "done.json"
    if marker.exists():
        cached = json.loads(marker.read_text(encoding="utf-8"))
        return [c for c in cached if Path(c["path"]).exists()]

    results = []
    try:
        # search with 429 backoff: a 10-scene script fires ~20 archive
        # searches in quick succession and Wikimedia rate-limits bursts
        # (the Bell Witch run lost most of its searches to this)
        r = None
        for attempt in (1, 2, 3):
            _commons_pace()
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
            if r.status_code == 429:
                time.sleep(2 * attempt)  # back off, then retry the search
                continue
            break
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
        # Collect every usable candidate FIRST, then download the most
        # relevant ones. The old code downloaded in raw search order, so a
        # loosely-related file could take the one slot a scene had.
        cands = []
        for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
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
            url = ii.get("thumburl") or ii.get("url")
            if not url:
                continue
            title = _clean(p.get("title", "").replace("File:", ""))
            cands.append({
                "pageid": p["pageid"], "url": url, "title": title,
                "license": lic,
                "author": _clean((meta.get("Artist") or {}).get("value", "")
                                 or "unknown"),
                "page": ii.get("descriptionurl", ""),
                "index": p.get("index", 99),
                "score": _relevance(title, query),
            })
        # keep only on-topic hits when there are enough of them; fall back
        # to everything for obscure subjects where nothing scores
        strong = [c for c in cands if c["score"] > 0]
        ranked = sorted(strong if len(strong) >= 3 else cands,
                        key=lambda c: (-c["score"], c["index"]))
        for c in ranked:
            if len(results) >= max_images:
                break
            dest = d / f"commons_{c['pageid']}.jpg"
            if not dest.exists():
                for attempt in (1, 2):
                    _commons_pace()
                    rr = requests.get(c["url"], headers=UA, timeout=60)
                    if rr.status_code == 429 and attempt == 1:
                        time.sleep(3)  # wikimedia rate-limits bursts
                        continue
                    rr.raise_for_status()
                    break
                if len(rr.content) < 20_000:  # tiny/decorative junk
                    continue
                dest.write_bytes(rr.content)
            results.append({
                "title": c["title"], "page": c["page"],
                "license": c["license"], "author": c["author"],
                "path": str(dest),
            })
            time.sleep(0.3)  # be polite to the API
    except Exception as e:
        log.warning("commons search failed for '%s': %s", query, e)

    marker.write_text(json.dumps(results), encoding="utf-8")
    if results:
        log.info("archives: %d real images for '%s'", len(results), query)
    return results


# archive.org period footage — curated public-domain film collections:
# Prelinger (ephemeral/industrial film), FedFlix + US National Archives
# (government film, newsreels, war footage), NASA (space program). All
# US-government or explicitly PD, so monetization-safe.
PD_COLLECTIONS = ("(prelinger OR fedflix OR usnationalarchives OR nasa "
                  "OR nationalarchives)")
VIDEO_MAX_BYTES = 120 << 20   # skip whole-movie rips; we want clips/reels
VIDEO_PREF_BYTES = 45 << 20   # best quality we'll spend bandwidth on


def _ia_query(q, rows):
    r = requests.get(
        "https://archive.org/advancedsearch.php",
        params={"q": q, "fl[]": ["identifier", "title", "year", "downloads"],
                "rows": rows, "page": 1, "output": "json"},
        headers=UA, timeout=30)
    r.raise_for_status()
    return (r.json().get("response") or {}).get("docs") or []


def search_archive_video(query, max_clips=3):
    """Search archive.org's public-domain film collections for real
    period FOOTAGE (newsreels, government film, war photography).
    Returns [{title, page, license, author, path}] like search_commons,
    cached per query, best match first. Sparse by nature — famous events
    hit, obscure cases return nothing (then stills/stock take over)."""
    d = _cache_dir("video:" + query)
    marker = d / "done.json"
    if marker.exists():
        cached = json.loads(marker.read_text(encoding="utf-8"))
        return [c for c in cached if Path(c["path"]).exists()]

    results = []
    try:
        # TITLE match, not full-text: archive.org's full-text search
        # matches loose description words ("nuclear test" returned 1950s
        # classroom films) — an unrelated film over the narration is worse
        # than stock, so precision beats recall here. Pass 1 is the exact
        # phrase; pass 2 asks for every significant word in the title,
        # which catches "Roanoke Colony Mystery" for "roanoke colony" —
        # still title-anchored, just not word-for-word. Ranked by title
        # overlap so the closest film downloads first.
        docs, seen_ids = [], set()
        passes = [f'title:("{query}") AND mediatype:movies '
                  f"AND collection:{PD_COLLECTIONS}"]
        words = _words(query)
        if len(words) > 1:
            passes.append("title:(" + " AND ".join(words) + ") "
                          f"AND mediatype:movies "
                          f"AND collection:{PD_COLLECTIONS}")
        for pass_no, q in enumerate(passes):
            if len(docs) >= max_clips * 4:
                break
            for doc in _ia_query(q, max_clips * 4):
                ident = doc.get("identifier")
                if not ident or ident in seen_ids:
                    continue
                seen_ids.add(ident)
                doc["_pass"] = pass_no
                doc["_score"] = _relevance(doc.get("title"), query)
                docs.append(doc)
        # a significant query word must appear in the item title
        docs = [x for x in docs
                if not words or any(w in (x.get("title") or "").lower()
                                    for w in words)]
        docs.sort(key=lambda x: (x["_pass"], -x["_score"],
                                 -int(x.get("downloads") or 0)))
        for doc in docs:
            if len(results) >= max_clips:
                break
            ident = doc["identifier"]
            m = requests.get(f"https://archive.org/metadata/{ident}",
                             headers=UA, timeout=30)
            m.raise_for_status()
            meta = m.json()
            # the best-quality playable file we're willing to download:
            # biggest under VIDEO_PREF_BYTES (the old "smallest" rule
            # picked 240p derivatives that look like mud upscaled to 1080p)
            small, big = [], []
            for f in meta.get("files", []):
                fmt = (f.get("format") or "").lower()
                name = (f.get("name") or "").lower()
                if not (name.endswith((".mp4", ".m4v"))
                        or "mpeg4" in fmt or "h.264" in fmt):
                    continue
                try:
                    size = int(f.get("size") or 0)
                except ValueError:
                    continue
                if not (2 << 20 < size <= VIDEO_MAX_BYTES):
                    continue
                (small if size <= VIDEO_PREF_BYTES else big).append(
                    (f["name"], size))
            if small:
                best = max(small, key=lambda t: t[1])
            elif big:
                best = min(big, key=lambda t: t[1])
            else:
                continue
            fname, _size = best
            import urllib.parse
            url = (f"https://archive.org/download/{ident}/"
                   + urllib.parse.quote(fname))
            dest = d / f"ia_{ident}_{Path(fname).stem}.mp4"
            if not dest.exists():
                rr = requests.get(url, headers=UA, timeout=600)
                rr.raise_for_status()
                if len(rr.content) < 1 << 20:
                    continue
                dest.write_bytes(rr.content)
            results.append({
                "title": _clean(doc.get("title") or ident),
                "page": f"https://archive.org/details/{ident}",
                "license": "Public domain"
                           f" (archive.org {meta.get('metadata', {}).get('collection', 'collection')})",
                "author": _clean(meta.get("metadata", {})
                                 .get("creator", "archive.org")),
                "path": str(dest),
            })
            time.sleep(0.5)
    except Exception as e:
        log.warning("archive.org video search failed for '%s': %s",
                    query, e)

    marker.write_text(json.dumps(results), encoding="utf-8")
    if results:
        log.info("archives: %d period films for '%s'", len(results), query)
    return results
