"""Text-to-speech via edge-tts (free Microsoft neural voices).

Produces one MP3 per text block plus word-level timings (from edge-tts
WordBoundary events) used to animate the burned-in captions.
Falls back to gTTS (no word timings) if edge-tts is broken.

Hang-proofing: edge-tts can hang indefinitely inside stream() (Microsoft
endpoint churn). Synthesis runs on a daemon thread with a hard deadline,
3 attempts, per-attempt temp files (a killed attempt can never corrupt a
previous one or block cleanup on Windows), and cache entries are only
trusted when they pass content validation.
"""
import asyncio
import json
import queue
import threading
from pathlib import Path

import edge_tts

from common import CACHE, setup_logging

log = setup_logging("tts")

TTS_TIMEOUT = 30.0   # seconds per attempt — a normal block finishes in ~2-8s
TTS_RETRIES = 3


async def _synthesize(text, voice, rate, volume, out_mp3, out_words):
    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume,
                                       boundary="WordBoundary")
    words = []
    with open(out_mp3, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # offset/duration are in 100-nanosecond units
                words.append({
                    "word": chunk["text"],
                    "start": chunk["offset"] / 1e7,
                    "duration": chunk["duration"] / 1e7,
                })
    with open(out_words, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False)
    if not words:
        log.warning("edge-tts returned no word boundaries for: %s...", text[:40])


def _synthesize_deadline(text, voice, rate, volume, out_mp3, out_words,
                         timeout=TTS_TIMEOUT):
    """Run one synthesis attempt with a hard wall-clock deadline. The
    worker thread is a daemon: on timeout we abandon it and delete the
    artifacts it may still be writing (unique per-attempt temp names
    mean it can't collide with the next attempt)."""
    done = queue.Queue()

    def run():
        try:
            asyncio.run(_synthesize(text, voice, rate, volume,
                                    out_mp3, out_words))
            done.put(True)
        except BaseException as e:  # includes CancelledError
            done.put(e)

    threading.Thread(target=run, daemon=True, name=f"tts-{out_mp3.stem}").start()
    try:
        r = done.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"edge-tts did not finish within {timeout:.0f}s")
    if r is not True:
        raise r


def _cache_ok(mp3, words):
    """Both artifacts exist AND look like real output: the mp3 starts
    with an MP3 frame sync byte (not a partial file or an error body)
    and the words JSON parses. Without this, a killed run's 0-byte mp3
    is reused forever."""
    try:
        if mp3.stat().st_size < 100:
            return False
        if mp3.read_bytes()[0] != 0xFF:  # MP3 frame sync
            return False
        json.loads(words.read_text(encoding="utf-8"))
        return True
    except (OSError, ValueError):
        return False


def tts_block(block_id, text, voice, rate="+0%", volume="+0%"):
    """Synthesize one text block. Returns (mp3_path, words_path)."""
    CACHE.mkdir(exist_ok=True)
    mp3 = CACHE / f"tts_{block_id}.mp3"
    words = CACHE / f"tts_{block_id}.words.json"
    if _cache_ok(mp3, words):
        return mp3, words

    log.info("TTS: %s (%d chars)", block_id, len(text))
    last_err = None
    for attempt in range(1, TTS_RETRIES + 1):
        # unique per-attempt names: a hung previous attempt holding an
        # open handle can never corrupt or block this one
        tmp_mp3 = mp3.with_suffix(f".try{attempt}.mp3")
        tmp_words = words.with_suffix(f".try{attempt}.json")
        try:
            _synthesize_deadline(text, voice, rate, volume,
                                 tmp_mp3, tmp_words)
            if not _cache_ok(tmp_mp3, tmp_words):
                raise RuntimeError("synthesis output failed validation")
            tmp_mp3.replace(mp3)
            tmp_words.replace(words)
            return mp3, words
        except Exception as e:
            last_err = e
            for p in (tmp_mp3, tmp_words):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass  # Windows: still held by the abandoned thread
            log.warning("TTS attempt %d/%d failed for %s: %s",
                        attempt, TTS_RETRIES, block_id, e)
    raise RuntimeError(f"TTS failed after {TTS_RETRIES} attempts for "
                       f"{block_id}: {last_err}")


def tts_block_gtts_fallback(block_id, text):
    """gTTS fallback: works even if edge-tts changes, but no word timings."""
    from gtts import gTTS
    mp3 = CACHE / f"tts_{block_id}.mp3"
    words = CACHE / f"tts_{block_id}.words.json"
    if not _cache_ok(mp3, words):
        for p in (mp3, words):
            p.unlink(missing_ok=True)
        gTTS(text=text, lang="en").save(str(mp3))
        words.write_text("[]", encoding="utf-8")
    return mp3, words


def tts(block_id, text, voice, rate="+0%", volume="+0%"):
    try:
        return tts_block(block_id, text, voice, rate, volume)
    except Exception as e:
        log.warning("edge-tts failed (%s) — falling back to gTTS for %s", e, block_id)
        return tts_block_gtts_fallback(block_id, text)
