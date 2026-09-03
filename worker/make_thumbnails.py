"""Generate a 1280x720 thumbnail for a rendered video.

FOOTNOTE brand style: cream editorial "book page" paper, heavy black type,
red footnote asterisk after the title — every video is the footnote
everyone skipped.

Usage:
    python make_thumbnails.py <script.json>
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common import REVIEW, setup_logging

log = setup_logging("thumb")

W, H = 1280, 720
PAPER = (250, 246, 238)
PAPER_LINE = (238, 232, 220)
INK = (20, 20, 30)
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


def make_thumbnail(script, out_path):
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    for y in range(0, H, 24):  # newsprint texture
        d.line([(0, y), (W, y)], fill=PAPER_LINE, width=1)

    font = _font(100)
    lines = wrap(d, script["title"].upper(), font, W - 200)
    line_h = 116
    y0 = H // 2 - line_h * len(lines) // 2
    d.rectangle([60, y0 - 40, W - 60, y0 - 32], fill=INK)
    for i, line in enumerate(lines):
        words = line.split()
        x = (W - d.textbbox((0, 0), line, font=font)[2]) // 2
        for j, wd in enumerate(words):
            # last word of the FIRST line gets the red accent
            color = RED if (i == 0 and j == len(words) - 1) else INK
            d.text((x, y0 + i * line_h), wd, font=font, fill=color)
            x += d.textbbox((0, 0), wd + " ", font=font)[2]
    # red footnote asterisk, superscript after the last line
    d.text((x + 6, y0 + (len(lines) - 1) * line_h - 6), "*",
           font=_font(72), fill=RED)
    d.rectangle([60, y0 + line_h * len(lines) + 16, W - 60,
                 y0 + line_h * len(lines) + 24], fill=INK)

    d.text((60, H - 80), "FOOTNOTE", font=_font(46), fill=INK)
    d.text((W - 300, H - 74), "the part they skipped *", font=_font(28),
           fill=RED)

    img.save(out_path)
    log.info("thumbnail: %s", out_path.name)
    return out_path


if __name__ == "__main__":
    script = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    make_thumbnail(script, REVIEW / f"{script['id']}_thumb.png")
