"""Render a script JSON into a Short (1080x1920) and a long-form (1920x1080)
video with voiceover, stock footage, and burned-in captions.

Usage:
    python render_video.py <path-to-script.json>
"""
import json
import sys
from pathlib import Path

from moviepy import (AudioFileClip, CompositeAudioClip, CompositeVideoClip,
                     ImageClip, VideoFileClip, concatenate_audioclips,
                     concatenate_videoclips)
from moviepy.audio.fx import AudioLoop, MultiplyVolume
from moviepy.video.fx import Crop, Resize

import tts as tts_mod
import visuals
from captions import render_caption_images
from common import (ASSETS, CACHE, REVIEW, load_config, setup_logging,
                    validate_script)

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


def scene_visual(clips, duration, w, h, label="", motion=True):
    """Build the visual track for one scene: stock clips (or gradient).
    With motion=True each segment gets a slow alternating zoom (Ken
    Burns) — static stock crops feel dead; a 4-6% drift makes them read
    as intentional cinematography."""
    if not clips:
        return gradient_clip(duration, w, h, label)
    segments, remaining, i, opened = [], duration, 0, []
    while remaining > 0.05:
        src = VideoFileClip(str(clips[i % len(clips)]))
        opened.append(src)
        take = min(remaining, max(src.duration - 0.5, 0.5))
        start = max(0, (src.duration - take) / 2)
        seg = src.subclipped(start, start + take)
        if motion:
            # alternate zoom directions per segment; 5% over the clip
            zoom_in = (i % 2 == 0)
            amt = 0.05
            if zoom_in:
                seg = seg.resized(lambda t, d=take: 1 + amt * (t / d))
            else:
                seg = seg.resized(lambda t, d=take: 1 + amt - amt * (t / d))
            # animated size needs a fixed-size canvas: center it and let
            # the composite clip off the overflow
            seg = CompositeVideoClip([seg.with_position("center")],
                                     size=(max(w, seg.w), max(h, seg.h))
                                     ).with_position("center")
            seg = CompositeVideoClip([seg], size=(w, h))
        else:
            seg = fit(seg, w, h)
        segments.append(seg)
        remaining -= take
        i += 1
    if not segments:
        return gradient_clip(duration, w, h, label)
    if len(segments) == 1:
        return segments[0]
    # crossfade between segments — hard cuts between unrelated stock
    # clips read as glitches; 0.25s blends read as editing
    from moviepy.video.fx.CrossFadeIn import CrossFadeIn
    faded = [segments[0]]
    for s in segments[1:]:
        faded.append(s.with_effects([CrossFadeIn(0.25)]))
    return concatenate_videoclips(faded, method="compose",
                                  padding=-0.25)


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
        chosen, total = [], audio_durations.get("hook", 0.0)
        for idx, s in enumerate(scenes):
            d = audio_durations.get(f"scene{idx}", 0)
            if s.get("in_short", True) and (total + d < 25 or not chosen):
                chosen.append((f"scene{idx}", s))
                total += d
        return [("hook", {"narration": script["hook"],
                          "visual_keywords": hook_visuals})] + chosen
    else:
        blocks += [(f"scene{i}", s) for i, s in enumerate(scenes)]
        if script.get("outro"):
            blocks.append(("outro", {"narration": script["outro"],
                                     "visual_keywords": []}))
        return blocks


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

    durations = {k: AudioFileClip(str(mp3)).duration for k, (mp3, _) in audio.items()}

    outputs = []
    blocks_long = []  # long-form scene order, for the description chapters
    for fmt, w, h in [("short", rconf["short_width"], rconf["short_height"]),
                      ("long", rconf["long_width"], rconf["long_height"])]:
        blocks = pick_scenes(script, fmt, durations)
        if fmt == "long":
            blocks_long = blocks

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
        total = sum(durations[bid] for bid, _ in blocks)

        # 2) stock clips per unique keyword (both orientations cached)
        keyword_clips = {}
        for _, s in blocks:
            for kw in s.get("visual_keywords", []):
                if kw not in keyword_clips:
                    keyword_clips[kw] = visuals.fetch_clips(
                        kw, "portrait" if fmt == "short" else "landscape",
                        keys, max_clips)

        # 3) assemble visual + audio + captions
        video_layers, audio_clips = [], []
        t = 0.0
        used_clips = set()   # clip filenames already shown in THIS video
        scene_no = 0
        for bid, s in blocks:
            mp3, words_path = audio[bid]
            d = durations[bid] + 0.35  # small pause between blocks
            kws = s.get("visual_keywords", [])

            # pool: this scene's keyword clips, deduped
            clips, seen = [], set()
            for kw in kws:
                for c in keyword_clips.get(kw, []):
                    if c.name not in seen:
                        seen.add(c.name)
                        clips.append(c)
            # VARIETY: prefer clips not used by earlier scenes, then rotate
            # the order per scene so even a small pool doesn't repeat the
            # same first clip every time
            fresh = [c for c in clips if c.name not in used_clips]
            stale = [c for c in clips if c.name in used_clips]
            ordered = fresh + stale
            if ordered and scene_no:
                rot = scene_no % len(ordered)
                ordered = ordered[rot:] + ordered[:rot]
            used_clips.update(c.name for c in ordered[:3])
            # Ken Burns motion on SHORTS only (where it fights the feed
            # scroll); long-form keeps static crops + crossfades — the
            # animated resize costs ~3.4x render time, which turns an
            # 8-12 min long into a 4-hour cloud job instead of ~1.5h
            visual = scene_visual(ordered, d, w, h,
                                  label=kws[0] if kws else "",
                                  motion=(fmt == "short"))
            video_layers.append(visual.with_start(t).with_duration(d))
            audio_clips.append(AudioFileClip(str(mp3)).with_start(t + 0.1))
            scene_no += 1

            # captions
            words = json.loads(words_path.read_text(encoding="utf-8"))
            cap_imgs = render_caption_images(
                f"{sid}_{bid}", words, w, h,
                rconf.get("caption_max_words", 3))
            for cs, ce, png in cap_imgs:
                img = (ImageClip(str(png))
                       .with_start(t + cs).with_duration(max(ce - cs, 0.15))
                       .with_position(("center", h * 0.66)))
                video_layers.append(img)
            t += d

        final_audio = CompositeAudioClip(audio_clips)
        music = music_track(t, rconf.get("music_volume", 0.08))
        if music:
            final_audio = CompositeAudioClip([final_audio, music])

        final = (CompositeVideoClip(video_layers, size=(w, h))
                 .with_audio(final_audio)
                 .with_duration(t))

        log.info("rendering %s (%.1fs) -> %s", fmt, t, out_path.name)
        final.write_videofile(
            str(out_path), codec="libx264", audio_codec="aac",
            fps=rconf.get("fps", 30), preset="medium", threads=4,
            temp_audiofile_path=str(REVIEW),  # temp audio next to output, not CWD
            # normalize to YouTube's loudness target — raw TTS+music mixes
            # vary by several dB between videos
            audio_ffmpeg_params=["-af",
                                 "loudnorm=I=-16:TP=-1.5:LRA=11"],
            logger=None)
        outputs.append(out_path)

        # free memory between formats
        for layer in video_layers:
            try:
                layer.close()
            except Exception:
                pass
        del video_layers, final

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
    meta = (f"title: {script['title']}\n"
            f"description: {desc}\n"
            f"tags: {', '.join(script.get('tags', []))}\n"
            f"chapters: {'|'.join(chapters)}\n"
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
