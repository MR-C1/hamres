"""Generate a 1280x720 thumbnail for a rendered video.

FOOTNOTE brand style: a darkened frame from the actual video as the
background (faces/scenes outperform plain text in CTR), with the cream
editorial type on top — every video is the footnote everyone skipped.

Usage:
    python make_thumbnails.py <script.json> [video.mp4]
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from common import REVIEW, setup_logging

log = setup_logging("thumb")

W, H = 1280, 720
PAPER = (250, 246, 238)
INK = (20, 20, 30)
CREAM = (250, 246, 238, 235)
RED = (178, 24, 24)
FONT_PATHS = [r"C:\Windows\Fonts\ariblk.ttf", r"C:\Windows\Fonts\impact.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]


def _font(size):
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _background(video_path):
    """A dramatic frame from the video (2/3 in — past any fade-in),
    darkened and desaturated so the text pops. Falls back to paper."""
    if not video_path or not Path(video_path).exists():
        return Image.new("RGB", (W, H), PAPER), False
    try:
        from moviepy import VideoFileClip
        src = VideoFileClip(str(video_path))
        t = min(src.duration * 0.66, max(src.duration - 0.5, 0))
        frame = src.get_frame(t)
        src.close()
        img = Image.fromarray(frame).resize((W, H)).convert("RGB")
        img = ImageEnhance.Brightness(img).enhance(0.45)   # darken
        img = ImageEnhance.Color(img).enhance(0.55)        # desaturate
        return img, True
    except Exception as e:
        log.warning("frame background failed (%s) — paper fallback", e)
        return Image.new("RGB", (W, H), PAPER), False


def make_thumbnail(script, out_path, video_path=None):
    img, has_bg = _background(video_path)
    if has_bg:
        # bottom-third scrim so the title sits on darkness, plus a thin
        # cream border for the editorial frame feel
        img = img.convert("RGBA")
        overlay = Image.new("RGBA", (W, H), (10, 8, 12, 0))
        d2 = ImageDraw.Draw(overlay)
        for y in range(H):
            a = int(min(215, max(0, (y - H * 0.22) / (H * 0.78) * 235)))
            d2.line([(0, y), (W, y)], fill=(10, 8, 12, a))
        img = Image.alpha_composite(img, overlay).convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")
    if has_bg:
        d.rectangle([14, 14, W - 15, H - 15], outline=CREAM, width=3)

    font = _font(100 if has_bg else 100)
    text_fill = (250, 246, 238, 255) if has_bg else INK
    lines = wrap(d, script["title"].upper(), font, W - 220)
    line_h = 116
    y0 = (H - line_h * len(lines)) // 2
    d.rectangle([70, y0 - 40, W - 70, y0 - 32], fill=CREAM if has_bg else INK)
    for i, line in enumerate(lines):
        words = line.split()
        x = (W - d.textbbox((0, 0), line, font=font)[2]) // 2
        for j, wd in enumerate(words):
            # last word of the FIRST line gets the red accent
            color = RED if (i == 0 and j == len(words) - 1) else text_fill
            stroke = (0, 0, 0, 255) if has_bg else None
            if stroke:
                d.text((x, y0 + i * line_h), wd, font=font, fill=color,
                       stroke_width=4, stroke_fill=stroke)
            else:
                d.text((x, y0 + i * line_h), wd, font=font, fill=color)
            x += d.textbbox((0, 0), wd + " ", font=font)[2]
    # red footnote asterisk, superscript after the last line
    d.text((x + 6, y0 + (len(lines) - 1) * line_h - 6), "*",
           font=_font(72), fill=RED)
    d.rectangle([70, y0 + line_h * len(lines) + 16, W - 70,
                 y0 + line_h * len(lines) + 24],
                fill=CREAM if has_bg else INK)

    d.text((70, H - 84), "FOOTNOTE", font=_font(46),
           fill=CREAM if has_bg else INK)
    d.text((W - 320, H - 78), "the part they skipped *", font=_font(28),
           fill=RED)

    img.save(out_path)
    log.info("thumbnail: %s%s", out_path.name,
             " (video-frame bg)" if has_bg else " (paper)")
    return out_path


if __name__ == "__main__":
    script = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    video = sys.argv[2] if len(sys.argv) > 2 else None
    make_thumbnail(script, REVIEW / f"{script['id']}_thumb.png", video)
