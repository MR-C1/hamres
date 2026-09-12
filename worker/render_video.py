"""Render a script JSON into a Short (1080x1920) and a long-form (1920x1080)
video with voiceover, stock footage, and burned-in captions.

Usage:
    python render_video.py <path-to-script.json>
"""
import json
import re
import sys
from pathlib import Path

from moviepy import (AudioFileClip, CompositeAudioClip, CompositeVideoClip,
                     ImageClip, VideoFileClip, concatenate_audioclips,
                     concatenate_videoclips)
from moviepy.audio.fx import AudioLoop, MultiplyVolume
from moviepy.video.fx import Crop, Resize

import archives
import tts as tts_mod
import visuals
from captions import render_caption_images
from common import (ASSETS, CACHE, REVIEW, build_timeline, load_config,
                    setup_logging, validate_script)

log = setup_logging("render")

MUSIC_DIR = ASSETS / "music"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def fit(clip, w, h):
    """Scale clip to fully cover w x h, center-crop the overflow."""
    scale = max(w / clip.w, h / clip.h)
    clip = clip.with_effects([Resize(scale)])
    return clip.with_effects([Crop(x_center=clip.w / 2, y_center=clip.h / 2,
                                    width=w, height=h)])


def _loudnorm(path):
    """Normalize to YouTube's loudness target (-16 LUFS) with a
    stream-copy pass: video bytes untouched, only the audio track
    re-encoded — costs seconds, not minutes. MoviePy 2.1.2 has no
    audio-filter hook on write_videofile (audio_ffmpeg_params does not
    exist — that TypeError killed every render), so this runs after."""
    import subprocess
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    tmp = path.with_suffix(".norm.mp4")
    r = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(path),
         "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:a", "aac", "-b:a", "192k", str(tmp)],
        capture_output=True, text=True, timeout=1800)
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
        tmp.replace(path)
    else:
        log.warning("loudnorm pass failed (keeping original): %s",
                    r.stderr.strip()[:120])
        tmp.unlink(missing_ok=True)


def _render_valid(path):
    """A finished render is worth keeping only if it fully decodes —
    MoviePy killed mid-write leaves a truncated mp4 that still opens.
    Full-decode costs ~seconds; a needless re-render costs ~15 min."""
    if not path.exists() or path.stat().st_size < 50_000:
        return False
    try:
        from upload import validate_video
        validate_video(path)
        return True
    except Exception:
        return False


def gradient_clip(duration, w, h, label=""):
    """Fallback background when no stock footage is available (no API key)."""
    from PIL import Image
    from captions import _font
    img = Image.new("RGB", (w, h))
    top, bottom = (18, 18, 38), (60, 20, 90)
    for y in range(h):
        t = y / h
        img.paste(tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
                  (0, y, w, y + 1))
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    if label:
        f = _font(int(w * 0.05))
        tw = d.textbbox((0, 0), label, font=f)[2]
        d.text(((w - tw) / 2, h * 0.45), label, font=f, fill=(220, 220, 240))
    p = CACHE / f"bg_{w}x{h}_{abs(hash(label)) % 99999}.png"
    img.save(p)
    return ImageClip(str(p)).with_duration(duration)


def _asterisk_mark(size, alpha=140):
    """The brand asterisk as a transparent PNG (cream, semi-opaque) for
    the corner watermark on every frame. Cached."""
    import math
    from PIL import Image, ImageDraw
    p = CACHE / f"mark_{size}_{alpha}.png"
    if p.exists():
        return p
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = size // 2
    r, width = int(size * 0.34), max(4, int(size * 0.11))
    color = (250, 246, 238, alpha)
    for i in range(6):
        a = i * (2 * math.pi / 6) - math.pi / 2
        x1, y1 = cx + r * math.cos(a), cy + r * math.sin(a)
        d.line([(cx, cy), (x1, y1)], fill=color, width=width)
        cap = width // 2 - 1
        d.ellipse([x1 - cap, y1 - cap, x1 + cap, y1 + cap], fill=color)
    d.ellipse([cx - width // 2 + 1, cy - width // 2 + 1,
               cx + width // 2 - 1, cy + width // 2 - 1], fill=color)
    img.save(p)
    return p


def _source_card(text, w):
    """A small translucent citation card: 'SOURCE · <text>'. The on-screen
    citation is the anti-slop differentiator — it says 'a human curated
    this' better than any disclaimer."""
    import hashlib
    from PIL import Image, ImageDraw, ImageFont
    from captions import _font
    key = hashlib.md5(f"{text}|{w}".encode()).hexdigest()[:12]
    p = CACHE / f"srccard_{key}.png"
    if p.exists():
        return p
    fs = max(16, int(w * 0.018))
    font = _font(fs)
    label = f"SOURCE · {text}"
    tmp = Image.new("RGBA", (10, 10))
    td = ImageDraw.Draw(tmp)
    tw = td.textbbox((0, 0), label, font=font)[2]
    img = Image.new("RGBA", (tw + fs * 2, fs * 3), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, img.width - 1, img.height - 1],
                        radius=fs // 2, fill=(20, 20, 30, 175))
    d.text((fs, fs // 2 + 2), label, font=font, fill=(250, 246, 238, 235))
    img.save(p)
    return p


def _end_card(w, h):
    """Branded 12s end card (long-form only): ink field, cream asterisk,
    wordmark, red tagline, subscribe line. Shorts loop instead — no end
    card on them."""
    import hashlib
    from PIL import Image, ImageDraw, ImageFont
    key = hashlib.md5(f"{w}x{h}".encode()).hexdigest()[:10]
    p = CACHE / f"endcard_{key}.png"
    if p.exists():
        return p

    def f(sz):
        for path in (r"C:\Windows\Fonts\ariblk.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            if Path(path).exists():
                return ImageFont.truetype(path, sz)
        return ImageFont.load_default()

    img = Image.new("RGB", (w, h), (20, 20, 30))
    d = ImageDraw.Draw(img)
    import math
    # faint oversized asterisk backdrop
    cx, cy, r, wd = w // 2, h // 2, int(h * 0.52), int(h * 0.16)
    for i in range(6):
        a = i * (2 * math.pi / 6) - math.pi / 2
        d.line([(cx, cy), (cx + r * math.cos(a), cy + r * math.sin(a))],
               fill=(32, 32, 44), width=wd)
    # crisp cream asterisk mark
    r2, w2 = int(h * 0.13), int(h * 0.042)
    cream = (250, 246, 238)
    for i in range(6):
        a = i * (2 * math.pi / 6) - math.pi / 2
        d.line([(cx, cy), (cx + r2 * math.cos(a), cy + r2 * math.sin(a))],
               fill=cream, width=w2)
    # wordmark
    word, fw = "FOOTNOTE", f(int(h * 0.13))
    tw = d.textbbox((0, 0), word, font=fw)[2]
    d.text(((w - tw) // 2, h * 0.66), word, font=fw, fill=cream)
    d.text(((w - tw) // 2 + tw + 6, h * 0.645), "*",
           font=f(int(h * 0.085)), fill=(178, 24, 24))
    tag = "the part they skipped"
    ft = f(int(h * 0.052))
    tw2 = d.textbbox((0, 0), tag, font=ft)[2]
    d.text(((w - tw2) // 2, h * 0.84), tag, font=ft, fill=(178, 24, 24))
    img.save(p)
    return p


def scene_visual(clips, duration, w, h, label="", motion=True):
    """Build the visual track for one scene from stock clips.

    Kept as a thin wrapper: montage() is the one place that decides how a
    block is cut, so there is no second copy of that logic to drift.
    """
    return montage([("video", c) for c in (clips or [])],
                   duration, w, h, label=label, motion=motion,
                   shot=3.0 if motion else 3.4)


def short_hook_text(script):
    """The trimmed hook used by the Shorts format (see render_from_dict)."""
    import re as _re
    if len(script["hook"].split()) <= 22:
        return script["hook"]
    cut = ""
    for sent in _re.split(r"(?<=[.!?])\s+", script["hook"]):
        if cut and len((cut + " " + sent).split()) > 22:
            break
        cut = (cut + " " + sent).strip()
    return cut


def still_visual(path, duration, w, h, zoom=0.13, variant=0):
    """Documentary Ken Burns: a slow zoom over a REAL archival still —
    the actual case photo, portrait, document, or newspaper scan.

    variant alternates the move (push in / pull out) so the same photo can
    come back later in a block without reading as a freeze-frame.
    """
    base = ImageClip(str(path)).with_duration(duration)
    scale0 = max(w / base.w, h / base.h) * 1.02
    if variant % 2:
        scaled = base.resized(
            lambda t: scale0 * (1 + zoom - zoom * (t / max(duration, 0.1))))
    else:
        scaled = base.resized(
            lambda t: scale0 * (1 + zoom * (t / max(duration, 0.1))))
    return CompositeVideoClip([scaled.with_position("center")],
                              size=(w, h))


def _video_shot(path, want, w, h, seq=0, motion=True, keep=None):
    """One shot cut out of a video file — a different moment on each reuse.

    A 12-minute newsreel used to become ONE unbroken take because the old
    code asked for the whole block in a single subclip. Asking for ~3s at a
    time, from a different offset per reuse, turns that same file into five
    different scenes. Returns (clip, actual_length).
    """
    src = VideoFileClip(str(path))
    if keep is not None:
        keep.append(src)          # reader must outlive this function
    take = min(want, max(src.duration - 0.3, 0.4))
    span = max(0.0, src.duration - take)
    # spread reuses over the middle 80% — leader frames and end slates are
    # usually black or a title card. Golden-ratio steps never cluster.
    frac = 0.5 if span < 1.0 else (0.1 + 0.8 * ((seq * 0.618) % 1.0))
    start = span * frac
    seg = src.subclipped(start, start + take)
    if motion:
        # alternate zoom directions per shot; 5% over the shot
        amt = 0.05
        if seq % 2 == 0:
            seg = seg.resized(lambda t, d=take: 1 + amt * (t / d))
        else:
            seg = seg.resized(lambda t, d=take: 1 + amt - amt * (t / d))
        # animated size needs a fixed-size canvas: center it and let the
        # composite clip off the overflow
        seg = CompositeVideoClip([seg.with_position("center")],
                                 size=(max(w, seg.w), max(h, seg.h))
                                 ).with_position("center")
        seg = CompositeVideoClip([seg], size=(w, h))
    else:
        seg = fit(seg, w, h)
    return seg, take


def shot_count(sources, duration, shot=3.0, cap_shots=10):
    """How many shots one narration block should be cut into.

    Pure arithmetic so it can be checked in milliseconds instead of a
    15-minute render (see --plan). A film can honestly supply several
    different moments, a photograph two different moves — asking for more
    shots than the material has would just repeat the same frames.
    """
    if not sources:
        return 0
    cap = sum(3 if k == "video" else 2 for k, _ in sources)
    if len(sources) == 1 and sources[0][0] == "still":
        # a lone photograph is one continuous move; cutting it against
        # itself reads as a bounce, not as an edit
        cap = 1
    return max(1, min(int(round(duration / max(shot, 1.2))), cap, cap_shots))


def shot_order(sources):
    """Endless round-robin over sources, numbering each reuse.

    The reuse number is what makes a second visit to the same file look
    different: another moment of a film, the opposite Ken Burns move on a
    photograph.
    """
    uses, i = {}, 0
    while True:
        kind, path = sources[i % len(sources)]
        key = str(path)
        seq = uses.get(key, 0)
        uses[key] = seq + 1
        i += 1
        yield kind, key, seq


def montage(sources, duration, w, h, label="", motion=True, shot=3.0,
            used=None, readers=None):
    """Cut ONE narration block into several shots instead of one long take.

    sources: ordered [(kind, path)] where kind is "video" or "still".

    Why this exists: a 20s Short came out as literally one clip and one
    photograph. The old code took the FIRST archival hit for a block and
    stretched it across the whole block — one subclip of a long film, or
    one slow zoom on one image. Here every ~3s gets its own shot, taken
    from a different source (or a different moment of the same long
    source), and sources are used round-robin so real footage and real
    photographs interleave instead of one winning the block outright.

    Paths actually used are appended to `used`, so only material that made
    it on screen gets credited in the description.
    """
    sources = [(k, p) for k, p in sources if p]
    if not sources:
        return gradient_clip(duration, w, h, label)
    n = shot_count(sources, duration, shot)
    # crossfades overlap by 0.25s, so the shots have to add up to slightly
    # more than the block for the concatenation to still fill it exactly
    need = duration + 0.25 * (n - 1)
    order = shot_order(sources)
    # readers (opened VideoFileClips) collect into the CALLER's list when
    # given: per-block rendering closes them after the block is written —
    # a VideoFileClip left open holds an ffmpeg process (and its buffers)
    # alive for the rest of the render
    keep = readers if readers is not None else []
    shots, filled, tries = [], 0.0, 0
    while filled < need - 0.05 and tries < 3 * n:
        tries += 1
        left = max(1, n - len(shots))
        want = max(1.2, (need - filled) / left)
        kind, path, seq = next(order)
        try:
            if kind == "still":
                shots.append(still_visual(path, want, w, h, variant=seq))
                got = want
            else:
                clip, got = _video_shot(path, want, w, h, seq, motion, keep)
                shots.append(clip)
        except Exception as e:      # corrupt download: skip, keep cutting
            log.warning("shot failed (%s): %s", Path(path).name, e)
            continue
        filled += got
        if used is not None and path not in used:
            used.append(path)
    if not shots:
        return gradient_clip(duration, w, h, label)
    if len(shots) == 1:
        return shots[0]
    from moviepy.video.fx.CrossFadeIn import CrossFadeIn
    faded = [shots[0]] + [s.with_effects([CrossFadeIn(0.25)])
                          for s in shots[1:]]
    return concatenate_videoclips(faded, method="compose", padding=-0.25)


def _release(clips):
    """Close a block's whole object graph and force a GC pass. The
    per-block architecture only pays off if RSS actually returns to
    baseline between blocks — a leaked VideoFileClip keeps its ffmpeg
    reader process (and its decode buffers) alive for the rest of the
    render, and enough of those is what OOM-killed the Actions runner."""
    import gc
    for c in clips:
        try:
            c.close()
        except Exception:
            pass
    gc.collect()


def _silence(duration, fps=44100):
    """A silent AudioClip — the end-card block needs an audio stream or
    the concat (stream copy) would see mixed audio/no-audio inputs."""
    from moviepy import AudioClip
    return AudioClip(lambda t: [0.0], duration=duration, fps=fps)


def _mark_layer(w, h, fmt, duration):
    """Corner brand mark on every frame: top-right on Shorts (captions
    + UI occupy the bottom), bottom-right on long-form."""
    mark = (ImageClip(str(_asterisk_mark(int(w * 0.045))))
            .with_start(0).with_duration(duration))
    return mark.with_position((w - int(w * 0.045) - int(w * 0.025),
                               int(h * 0.03) if "short" in fmt
                               else h - int(w * 0.045) - int(h * 0.04)))


def _concat(blocks, dest):
    """Join per-block files into one video. Stream copy first — the
    blocks all come from the same writer with the same parameters, so it
    is lossless and takes seconds — with a re-encode fallback for any
    container-level disagreement. Returns True only when the result
    fully decodes (a concat that silently drops half the blocks is worse
    than a failure the caller can raise on)."""
    import subprocess
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    lst = dest.with_suffix(".blocks.txt")
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in blocks),
                   encoding="utf-8")
    for args in (["-c", "copy"],
                 ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                  "-c:a", "aac", "-b:a", "192k"]):
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(lst), *args, str(dest)],
            capture_output=True, text=True, timeout=7200)
        if r.returncode == 0 and _render_valid(dest):
            lst.unlink(missing_ok=True)
            return True
        log.warning("concat %s failed: %s", args[1], (r.stderr or "")[-300:])
    lst.unlink(missing_ok=True)
    return False


def pick_scenes(script, fmt, audio_durations):
    """Which narration blocks go into which format.

    Every returned block is (block_id, scene_dict) — hook and outro are
    normalized into the same shape as scenes.
    """
    hook_visuals = script.get("hook_visuals") or script["scenes"][0].get(
        "visual_keywords", [])
    blocks = [("hook", {"narration": script["hook"],
                        "visual_keywords": hook_visuals})]
    scenes = script["scenes"]
    if fmt == "short":
        # 18-25s sweet spot: the average Short watch is ~16s, and
        # completion-per-second is the distribution currency. The hook
        # is always block 1 (cold open); no outro in shorts — a
        # "subscribe" line wastes 2-4s and reads as template
        chosen, total = [], audio_durations.get(
            "hookshort", audio_durations.get("hook", 0.0))
        # Shorts are built from the trimmed hook + the in_short scenes'
        # SHORT narrations (40-60 words) when present — full scenes run
        # 30-40s and can never fit (one full scene = a 46s Short).
        # Legacy scripts without short_narration take at most ONE full
        # scene, budget be damned.
        for idx, s in enumerate(scenes):
            if not s.get("in_short", True):
                continue
            if s.get("short_narration"):
                d = audio_durations.get(f"shortscene{idx}", 0)
                if total + d < 20 or not chosen:
                    chosen.append((f"shortscene{idx}", s))
                    total += d
            else:
                d = audio_durations.get(f"scene{idx}", 0)
                if total + d < 20 or not chosen:
                    chosen.append((f"scene{idx}", s))
                    total += d
        return [("hookshort", {"narration": short_hook_text(script),
                                "visual_keywords": hook_visuals})] + chosen
    elif fmt.startswith("xshort"):
        # a STANDALONE scene-Short: one scene's short_narration as its
        # own complete vertical video (see pick_extra_shorts)
        idx = int(fmt[len("xshort"):])
        return [(f"shortscene{idx}", scenes[idx])]
    else:
        blocks += [(f"scene{i}", s) for i, s in enumerate(scenes)]
        if script.get("outro"):
            blocks.append(("outro", {"narration": script["outro"],
                                     "visual_keywords": []}))
        return blocks


def pick_extra_shorts(script, audio_durations, count):
    """Which scenes become STANDALONE scene-Shorts ("xshort{n}" formats).

    One script's products used to be one hook-Short + the long-form;
    now each in_short scene that did NOT make it into the main Short can
    ship as its own vertical video — one video becomes several shelf
    products from the same render budget (the TTS is already paid for).

    Constraints that keep this safe:
    - only scenes with a short_narration qualify (a full 30-40s scene
      narration is too long for a standalone Short);
    - scenes already inside the main Short are excluded — the same audio
      shipping twice on one channel reads as duplicate content;
    - `count` is capped by config so the YouTube upload quota
      (10,000 API units/day, videos.insert = 1,600) is never blown by
      default: 1 long + 1 hook-Short + 1 scene-Short = 3 uploads per
      script, ~6/day at two scripts.
    Returns [(scene_index, scene_dict)].
    """
    if count <= 0:
        return []
    in_main = {bid for bid, _ in pick_scenes(script, "short",
                                             audio_durations)}
    out = []
    for idx, s in enumerate(script["scenes"]):
        if not (s.get("short_narration") or "").strip():
            continue
        if not s.get("in_short", True):
            continue
        if f"shortscene{idx}" in in_main:
            continue
        out.append((idx, s))
        if len(out) >= count:
            break
    return out


def music_track(total_duration, volume):
    files = list(MUSIC_DIR.glob("*.mp3")) if MUSIC_DIR.exists() else []
    if not files or volume <= 0:
        return None
    try:
        m = AudioFileClip(str(files[0]))
        if m.duration < total_duration:
            m = m.with_effects([AudioLoop(duration=total_duration)])
        else:
            m = m.subclipped(0, total_duration)
        return m.with_effects([MultiplyVolume(volume)])
    except Exception as e:
        log.warning("music skipped: %s", e)
        return None


# ----------------------------------------------------------------------
# main render
# ----------------------------------------------------------------------
def render(script_path, config):
    script = validate_script(json.loads(Path(script_path).read_text(encoding="utf-8")))
    return render_from_dict(script, config)


def render_from_dict(script, config):
    """Render from an in-memory script dict (used by the agent worker)."""
    script = validate_script(script)
    sid = script["id"]
    vconf, rconf = config["voice"], config["render"]
    keys = config.get("keys", {})
    max_clips = rconf.get("max_clips_per_keyword", 8)

    # 1) voiceover for hook, scenes, outro
    audio = {}   # block_id -> (mp3_path, words_path)
    audio["hook"] = tts_mod.tts(f"{sid}_hook", script["hook"],
                                vconf["long"], vconf.get("rate", "+0%"))
    for i, s in enumerate(script["scenes"]):
        audio[f"scene{i}"] = tts_mod.tts(f"{sid}_scene{i}", s["narration"],
                                         vconf["long"], vconf.get("rate", "+0%"))
    if script.get("outro"):
        audio["outro"] = tts_mod.tts(f"{sid}_outro", script["outro"],
                                     vconf["long"], vconf.get("rate", "+0%"))

    # SHORTS HOOK TRIM: the 8-12 min format's cold-open hook is 80-120
    # words (~40-60s of audio) — used raw it blows the 25s Shorts budget
    # and every Short lands at 70-90s. The Short opens on the hook's
    # first sentence(s) instead.
    short_hook = short_hook_text(script)
    if short_hook and short_hook != script["hook"]:
        audio["hookshort"] = tts_mod.tts(f"{sid}_hookshort", short_hook,
                                         vconf["long"], vconf.get("rate", "+0%"))
    elif short_hook:
        audio["hookshort"] = audio["hook"]

    # SHORT SCENE NARRATIONS: in_short scenes carry a compressed 40-60
    # word version for the Short — full scenes run 30-40s and can never
    # fit after the trimmed hook (one full scene = a 46s Short)
    for idx, s_ in enumerate(script["scenes"]):
        sn = (s_.get("short_narration") or "").strip()
        if sn and s_.get("in_short"):
            audio[f"shortscene{idx}"] = tts_mod.tts(
                f"{sid}_shortscene{idx}", sn,
                vconf["long"], vconf.get("rate", "+0%"))

    durations = {}
    for k, (mp3, _) in audio.items():
        a = AudioFileClip(str(mp3))
        durations[k] = a.duration
        a.close()  # reader process — never leave it parked for the render

    outputs = []
    credits = []      # archival attributions for the description
    blocks_long = []  # long-form scene order, for the description chapters
    # formats: the hook-Short, the long-form, then one STANDALONE
    # scene-Short per pick_extra_shorts pick ("xshort{scene_index}").
    # render.shorts_per_video caps them (default 1 — see pick_extra_shorts
    # for the upload-quota arithmetic).
    extra_shorts = pick_extra_shorts(
        script, durations, int(rconf.get("shorts_per_video", 1)))
    formats = [("short", rconf["short_width"], rconf["short_height"]),
               ("long", rconf["long_width"], rconf["long_height"])]
    for idx, _s in extra_shorts:
        formats.append((f"xshort{idx}", rconf["short_width"],
                        rconf["short_height"]))
    if extra_shorts:
        log.info("extra scene-shorts: %s",
                 ", ".join(f"scene {i}" for i, _ in extra_shorts))
    for fmt, w, h in formats:
        blocks = pick_scenes(script, fmt, durations)
        if fmt == "long":
            blocks_long = blocks
            # scene timings for the retention mapping — written before any
            # rendering starts, so even a crashed render leaves the map
            (REVIEW / f"{sid}_timeline.json").write_text(
                json.dumps({"scenes": build_timeline(blocks, durations)},
                           ensure_ascii=False), encoding="utf-8")
        portrait = "short" in fmt  # hook-Short and scene-Shorts alike

        # RESUME: a format whose output already exists and fully decodes
        # is never re-rendered — a minute-35 crash used to redo both
        # formats (and the TTS/footage below are cached too, so a retry
        # costs only the step that failed)
        REVIEW.mkdir(parents=True, exist_ok=True)
        out_path = REVIEW / f"{sid}_{fmt}.mp4"
        if _render_valid(out_path):
            log.info("resume: %s already rendered — skipping", out_path.name)
            outputs.append(out_path)
            continue
        # 2) stock clips per unique keyword (both orientations cached)
        keyword_clips = {}
        for _, s in blocks:
            for kw in s.get("visual_keywords", []):
                if kw not in keyword_clips:
                    keyword_clips[kw] = visuals.fetch_clips(
                        kw, "portrait" if portrait else "landscape",
                        keys, max_clips)

        # 3) PER-BLOCK assembly + render. The old code built ONE
        # CompositeVideoClip for the whole video: every caption ImageClip
        # (hundreds of full-frame RGBA buffers) and every stock-video
        # reader stayed open until the final write — that graph
        # OOM-killed the 16GB Actions runner mid-assembly four runs in a
        # row (exit 137, "runner has received a shutdown signal"). Each
        # block now renders to its own file and its whole object graph is
        # closed before the next block starts: peak memory is one block's
        # worth, a crash resumes at the failed block, and the block files
        # concat losslessly at the end.
        block_paths = []
        used_clips = set()   # clip filenames already shown in THIS video
        for bi, (bid, s) in enumerate(blocks):
            bpath = REVIEW / f"{sid}_{fmt}_{bi:02d}_{bid}.mp4"
            block_paths.append(bpath)
            if _render_valid(bpath):
                log.info("resume: block %s already rendered — skipping",
                         bpath.name)
                continue
            mp3, words_path = audio[bid]
            d = durations[bid] + 0.35  # small pause between blocks
            kws = s.get("visual_keywords", [])

            # REAL archival, in documentary order: period FOOTAGE of the
            # actual event (newsreels, government film) → real PHOTOS/
            # documents → stock footage ONLY as the last resort. Owner
            # directive: real material must dominate. Multiple search
            # terms are all tried; stock fills only genuinely abstract
            # connective moments.
            terms = s.get("archive_search", [])
            vids_pool, stills_pool, seen_paths = [], [], set()
            for term in terms:
                for v in archives.search_archive_video(term):
                    if v["path"] not in seen_paths:
                        seen_paths.add(v["path"])
                        vids_pool.append(v)
                for a in archives.search_commons(term):
                    if a["path"] not in seen_paths:
                        seen_paths.add(a["path"])
                        stills_pool.append(a)
            by_path = {x["path"]: x for x in vids_pool + stills_pool}
            # alternate footage and photographs so a block cuts between
            # kinds of real material, not just between frames of one film
            sources = []
            for i in range(max(len(vids_pool), len(stills_pool))):
                if i < len(vids_pool):
                    sources.append(("video", vids_pool[i]["path"]))
                if i < len(stills_pool):
                    sources.append(("still", stills_pool[i]["path"]))

            # pool: this scene's keyword clips, deduped
            clips, seen = [], set()
            for kw in kws:
                for c in keyword_clips.get(kw, []):
                    if c.name not in seen:
                        seen.add(c.name)
                        clips.append(c)
            # VARIETY: prefer clips not used by earlier scenes, then
            # rotate the order per scene so even a small pool doesn't
            # repeat the same first clip every time
            fresh = [c for c in clips if c.name not in used_clips]
            stale = [c for c in clips if c.name in used_clips]
            ordered = fresh + stale
            if ordered and bi:
                rot = bi % len(ordered)
                ordered = ordered[rot:] + ordered[:rot]

            if sources:
                # start each block at a different point in its own pool so
                # consecutive blocks don't open on the same photograph
                sources = (sources[bi % len(sources):]
                           + sources[:bi % len(sources)])
                if len(sources) < 2 and ordered:
                    # one lonely photo can only carry two moves; let stock
                    # take the connective moments so the block still cuts
                    sources += [("video", str(c)) for c in ordered[:2]]
                    used_clips.update(c.name for c in ordered[:2])
            else:
                sources = [("video", str(c)) for c in ordered]
                used_clips.update(c.name for c in ordered[:3])

            # ONE BLOCK, SEVERAL SHOTS: this used to hand the whole block
            # to a single subclip or a single still, which is why a Short
            # arrived as one clip plus one photo
            shown, readers = [], []
            visual = montage(sources, d, w, h,
                             label=kws[0] if kws else "",
                             motion=portrait,
                             shot=2.6 if portrait else 3.4,
                             used=shown, readers=readers)
            for p in shown:
                if p in by_path and by_path[p] not in credits:
                    credits.append(by_path[p])
            layers = [visual]

            # captions — times are block-relative (identical to the old
            # global construction, where audio led each caption by 0.1s)
            words = json.loads(words_path.read_text(encoding="utf-8"))
            cap_imgs = render_caption_images(
                f"{sid}_{bid}", words, w, h,
                rconf.get("caption_max_words", 3))
            for cs, ce, png in cap_imgs:
                img = (ImageClip(str(png))
                       .with_start(cs).with_duration(max(ce - cs, 0.15))
                       .with_position(("center", h * 0.66)))
                layers.append(img)

            # source card: on-screen citation for the scene's anchor fact
            src = (s.get("source") or "").strip()
            if src:
                card = _source_card(src[:80], w)
                cw = ImageClip(str(card)).with_start(0.4).with_duration(
                    min(4.0, d - 0.5))
                cw = cw.with_position((int(w * 0.03), int(h * 0.86)))
                layers.append(cw)
            layers.append(_mark_layer(w, h, fmt, d))

            # write THIS block, then free everything it allocated before
            # the next one starts from a clean slate — this is the fix:
            # the old single composite kept every caption layer and every
            # stock-video reader open until the final write
            narr = AudioFileClip(str(mp3)).with_start(0.1)
            block_audio = CompositeAudioClip([narr])
            music = music_track(d, rconf.get("music_volume", 0.08))
            if music:
                block_audio = CompositeAudioClip([narr, music])
            comp = (CompositeVideoClip(layers, size=(w, h))
                    .with_audio(block_audio).with_duration(d))
            log.info("rendering %s block %d/%d (%.1fs) -> %s", fmt, bi + 1,
                     len(blocks), d, bpath.name)
            comp.write_videofile(
                str(bpath), codec="libx264", audio_codec="aac",
                fps=rconf.get("fps", 30), preset="medium", threads=4,
                temp_audiofile_path=str(REVIEW),  # temp audio next to output
                logger=None)
            _release(layers + [comp, narr, block_audio] + readers)

        # end card as its own silent block (long-form only — Shorts loop
        # instead). It needs an audio stream to keep the concat copyable.
        if fmt == "long":
            ec = REVIEW / f"{sid}_{fmt}_{len(blocks):02d}_endcard.mp4"
            block_paths.append(ec)
            if _render_valid(ec):
                log.info("resume: end card already rendered — skipping")
            else:
                layers = [ImageClip(str(_end_card(w, h))).with_start(0)
                          .with_duration(12),
                          _mark_layer(w, h, fmt, 12)]
                comp = (CompositeVideoClip(layers, size=(w, h))
                        .with_audio(_silence(12)).with_duration(12))
                log.info("rendering %s end card -> %s", fmt, ec.name)
                comp.write_videofile(
                    str(ec), codec="libx264", audio_codec="aac",
                    fps=rconf.get("fps", 30), preset="medium", threads=4,
                    temp_audiofile_path=str(REVIEW),
                    logger=None)
                _release(layers + [comp])

        # 4) join the blocks — lossless stream copy, re-encode fallback
        if not _concat(block_paths, out_path):
            raise RuntimeError(
                f"{fmt} concat failed for {sid} — blocks kept for resume")
        log.info("concatenated %d blocks -> %s", len(block_paths),
                 out_path.name)
        _loudnorm(out_path)  # YouTube's loudness target, audio-only pass
        outputs.append(out_path)
        # the blocks are dead weight once the final exists
        for p in block_paths:
            p.unlink(missing_ok=True)

    # 4) metadata file for the uploader
    desc = script.get("description", script["hook"])
    # chapters for the long-form description (YouTube shows them in the
    # UI and they're a real SEO surface) — pipe-separated because the
    # metadata parser splits on the FIRST ": " only
    chapters = []
    t = 0.0
    for bid, s in blocks_long:
        label = "Intro" if bid == "hook" else (
            " ".join(s.get("narration", "").split()[:6]) + "…")
        chapters.append(f"{int(t // 60)}:{int(t % 60):02d} {label}")
        t += durations.get(bid, 0) + 0.35
    credit_lines = [
        f"{c['title']} by {c['author']} ({c['license']}) {c['page']}"
        for c in credits]
    meta = (f"title: {script['title']}\n"
            f"description: {desc}\n"
            f"tags: {', '.join(script.get('tags', []))}\n"
            f"chapters: {'|'.join(chapters)}\n"
            f"credits: {'|'.join(credit_lines)}\n"
            f"id: {sid}\n")
    (REVIEW / f"{sid}_metadata.txt").write_text(meta, encoding="utf-8")
    log.info("done: %s", ", ".join(p.name for p in outputs))
    return outputs


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cfg = load_config()
    render(sys.argv[1], cfg)
