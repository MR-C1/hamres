"""Burned-in caption images rendered with Pillow.

Takes word timings from TTS and groups words into short phrases
(3-4 words at a time, the TikTok/Shorts style that keeps retention high).
Each group becomes a transparent PNG shown during its time window.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common import CACHE, setup_logging

log = setup_logging("captions")

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # GitHub Actions / Linux
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]

# highlight color pops; base is white with heavy black stroke
BASE_FILL = (255, 255, 255, 255)
STROKE_FILL = (0, 0, 0, 255)
HIGHLIGHT_FILL = (255, 230, 0, 255)


def _font(size):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def group_words(words, max_words=4):
    """Group word timings into caption phrases."""
    groups = []
    current = []
    for w in words:
        current.append(w)
        end = w["start"] + w["duration"]
        # break on punctuation or when group is full
        if len(current) >= max_words or w["word"].rstrip().endswith((",", ".", "!", "?", ";", ":")):
            groups.append({
                "text": " ".join(x["word"] for x in current),
                "start": current[0]["start"],
                "end": end,
            })
            current = []
    if current:
        groups.append({
            "text": " ".join(x["word"] for x in current),
            "start": current[0]["start"],
            "end": current[-1]["start"] + current[-1]["duration"],
        })
    return groups


def render_caption_images(block_id, words, video_w, video_h, max_words=4, highlight_last=True):
    """Return [(start, end, png_path), ...] for one TTS block."""
    if not words:
        return []
    out_dir = CACHE / "captions" / block_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # scale font to video size
    font_size = int(video_w * 0.075) if video_w < video_h else int(video_w * 0.045)
    font = _font(font_size)
    stroke_w = max(3, font_size // 14)

    result = []
    groups = group_words(words, max_words)
    for i, g in enumerate(groups):
        words_in_group = g["text"].split()
        # measure
        tmp = Image.new("RGBA", (10, 10))
        draw = ImageDraw.Draw(tmp)
        widths = [draw.textbbox((0, 0), w, font=font, stroke_width=stroke_w)[2] for w in words_in_group]
        space_w = draw.textbbox((0, 0), " ", font=font)[2]
        total_w = sum(widths) + space_w * (len(words_in_group) - 1) + font_size
        total_h = int(font_size * 1.6)

        img = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        x = font_size // 2
        for j, wtext in enumerate(words_in_group):
            fill = HIGHLIGHT_FILL if (highlight_last and j == len(words_in_group) - 1) else BASE_FILL
            draw.text((x, total_h // 2 - font_size // 2), wtext, font=font,
                      fill=fill, stroke_width=stroke_w, stroke_fill=STROKE_FILL)
            x += widths[j] + space_w

        png = out_dir / f"{i:03d}.png"
        img.save(png)
        result.append((g["start"], g["end"], png))
    return result
