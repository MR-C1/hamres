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


def scene_visual(clips, duration, w, h, label=""):
    """Build the visual track for one scene: stock clips (or gradient)."""
    if not clips:
        return gradient_clip(duration, w, h, label)
    segments, remaining, i, opened = [], duration, 0, []
    while remaining > 0.05:
        src = VideoFileClip(str(clips[i % len(clips)]))
        opened.append(src)
        take = min(remaining, max(src.duration - 0.5, 0.5))
        start = max(0, (src.duration - take) / 2)
        seg = fit(src.subclipped(start, start + take), w, h)
        segments.append(seg)
        remaining -= take
        i += 1
    if not segments:
        return gradient_clip(duration, w, h, label)
    if len(segments) == 1:
        return segments[0]
    return concatenate_videoclips(segments, method="chain")


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
        chosen, total = [], 0.0
        for idx, s in enumerate(scenes):
            d = audio_durations.get(f"scene{idx}", 0)
            if s.get("in_short", True) and (total + d < 42 or not chosen):
                chosen.append((f"scene{idx}", s))
                total += d
        blocks += chosen
        if script.get("outro"):
            blocks.append(("outro", {"narration": script["outro"],
                                     "visual_keywords": []}))
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
    for fmt, w, h in [("short", rconf["short_width"], rconf["short_height"]),
                      ("long", rconf["long_width"], rconf["long_height"])]:
        blocks = pick_scenes(script, fmt, durations)
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
            visual = scene_visual(ordered, d, w, h,
                                  label=kws[0] if kws else "")
            video_layers.append(visual.with_start(t).with_duration(d))
            audio_clips.append(AudioFileClip(str(mp3)).with_start(t + 0.1))
            scene_no += 1

            # captions
            words = json.loads(words_path.read_text(encoding="utf-8"))
            cap_imgs = render_caption_images(
                f"{sid}_{bid}", words, w, h,
                rconf.get("caption_max_words", 4))
            for cs, ce, png in cap_imgs:
                img = (ImageClip(str(png))
                       .with_start(t + cs).with_duration(max(ce - cs, 0.15))
                       .with_position(("center", h * 0.72)))
                video_layers.append(img)
            t += d

        final_audio = CompositeAudioClip(audio_clips)
        music = music_track(t, rconf.get("music_volume", 0.08))
        if music:
            final_audio = CompositeAudioClip([final_audio, music])

        final = (CompositeVideoClip(video_layers, size=(w, h))
                 .with_audio(final_audio)
                 .with_duration(t))

        REVIEW.mkdir(parents=True, exist_ok=True)
        out_path = REVIEW / f"{sid}_{fmt}.mp4"
        log.info("rendering %s (%.1fs) -> %s", fmt, t, out_path.name)
        final.write_videofile(
            str(out_path), codec="libx264", audio_codec="aac",
            fps=rconf.get("fps", 30), preset="medium", threads=4,
            temp_audiofile_path=str(REVIEW),  # temp audio next to output, not CWD
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
    meta = (f"title: {script['title']}\n"
            f"description: {desc}\n"
            f"tags: {', '.join(script.get('tags', []))}\n"
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
