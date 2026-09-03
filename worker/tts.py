"""Text-to-speech via edge-tts (free Microsoft neural voices).

Produces one MP3 per text block plus word-level timings (from edge-tts
WordBoundary events) used to animate the burned-in captions.
Falls back to gTTS (no word timings) if edge-tts is broken.
"""
import asyncio
import json
from pathlib import Path

import edge_tts

from common import CACHE, setup_logging

log = setup_logging("tts")


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


def tts_block(block_id, text, voice, rate="+0%", volume="+0%"):
    """Synthesize one text block. Returns (mp3_path, words_path)."""
    CACHE.mkdir(exist_ok=True)
    mp3 = CACHE / f"tts_{block_id}.mp3"
    words = CACHE / f"tts_{block_id}.words.json"
    if mp3.exists() and words.exists():
        return mp3, words
    log.info("TTS: %s (%d chars)", block_id, len(text))
    asyncio.run(_synthesize(text, voice, rate, volume, mp3, words))
    return mp3, words


def tts_block_gtts_fallback(block_id, text):
    """gTTS fallback: works even if edge-tts changes, but no word timings."""
    from gtts import gTTS
    mp3 = CACHE / f"tts_{block_id}.mp3"
    words = CACHE / f"tts_{block_id}.words.json"
    if not (mp3.exists() and words.exists()):
        gTTS(text=text, lang="en").save(str(mp3))
        words.write_text("[]", encoding="utf-8")
    return mp3, words


def tts(block_id, text, voice, rate="+0%", volume="+0%"):
    try:
        return tts_block(block_id, text, voice, rate, volume)
    except Exception as e:
        log.warning("edge-tts failed (%s) — falling back to gTTS for %s", e, block_id)
        return tts_block_gtts_fallback(block_id, text)
