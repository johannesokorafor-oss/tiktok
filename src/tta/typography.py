"""Deterministic cover typography.

The hook is rendered programmatically with Pillow - never by the image
model.  Features: adaptive font size, automatic wrapping, safe margins
(TikTok UI zones), stroke + soft shadow, optional scrim gradient for
contrast, full Unicode/umlaut support via bundled DejaVu fonts.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSET_FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"

# Candidate fonts, first match wins.  DejaVu is bundled with the repo so
# umlauts always work; Windows system fonts are preferred when present.
_FONT_CANDIDATES_BOLD = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
    "C:/Windows/Fonts/impact.ttf",
    str(ASSET_FONTS / "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "C:/Windows/Fonts/arial.ttf",
    str(ASSET_FONTS / "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

# Safe area for 1080x1920 TikTok covers (UI overlays bottom + right edge).
SIDE_MARGIN = 90
TOP_SAFE = 250
BOTTOM_SAFE = 500

MAX_LINES = 3
MIN_FONT_SIZE = 54
MAX_FONT_SIZE = 168


class TypographyError(RuntimeError):
    pass


def find_font(bold: bool = True) -> str:
    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR
    for cand in candidates:
        if Path(cand).is_file():
            return cand
    raise TypographyError(
        "No usable TTF font found. Expected bundled font at "
        f"{ASSET_FONTS / 'DejaVuSans-Bold.ttf'}"
    )


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    return " ".join(text.split())


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_width: int) -> list[str] | None:
    """Greedy wrap; None if any single word cannot fit."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if draw.textlength(word, font=font) > max_width:
            return None
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: str,
              max_width: int, max_height: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    for size in range(MAX_FONT_SIZE, MIN_FONT_SIZE - 1, -6):
        font = ImageFont.truetype(font_path, size)
        lines = _wrap(draw, text, font, max_width)
        if lines is None or len(lines) > MAX_LINES:
            continue
        line_height = int(size * 1.18)
        if line_height * len(lines) <= max_height:
            return font, lines
    # Force minimum size and hard-wrap
    font = ImageFont.truetype(font_path, MIN_FONT_SIZE)
    lines = _wrap(draw, text, font, max_width) or [text]
    return font, lines[:MAX_LINES]


def render_cover_text(
    background_path: str | Path,
    text: str,
    out_path: str | Path,
    *,
    accent: bool = True,
    scrim: bool = True,
) -> Path:
    """Compose `text` onto the background image; writes 1080x1920 PNG."""
    text = _clean_text(text)
    if not text:
        raise TypographyError("cover text is empty")

    img = Image.open(background_path).convert("RGB")
    width, height = img.size
    canvas = img.convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    max_text_width = width - 2 * SIDE_MARGIN
    max_text_height = height - TOP_SAFE - BOTTOM_SAFE
    font_path = find_font(bold=True)
    font, lines = _fit_text(draw, text, font_path, max_text_width,
                            min(max_text_height, int(height * 0.35)))

    line_height = int(font.size * 1.18)
    block_height = line_height * len(lines)
    # place the block slightly above the vertical centre (upper-middle zone)
    block_top = int(height * 0.40) - block_height // 2
    block_top = max(TOP_SAFE, min(block_top, height - BOTTOM_SAFE - block_height))

    # ------------------------------------------------------ scrim gradient
    if scrim:
        scrim_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(scrim_layer)
        pad = int(font.size * 1.4)
        top = max(0, block_top - pad)
        bottom = min(height, block_top + block_height + pad)
        for y in range(top, bottom):
            # smooth ramp in/out
            t = (y - top) / max(1, bottom - top)
            alpha = int(120 * (1 - abs(2 * t - 1)) ** 0.8)
            sdraw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        canvas = Image.alpha_composite(canvas, scrim_layer.filter(ImageFilter.GaussianBlur(6)))
        draw = ImageDraw.Draw(canvas)

    # ------------------------------------------------------ shadow layer
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    stroke_w = max(3, font.size // 16)
    y = block_top
    for line in lines:
        line_width = draw.textlength(line, font=font)
        x = (width - line_width) // 2
        shadow_draw.text((x + 6, y + 8), line, font=font, fill=(0, 0, 0, 200))
        y += line_height
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(8)))
    draw = ImageDraw.Draw(canvas)

    # ------------------------------------------------------ main text
    y = block_top
    for line in lines:
        line_width = draw.textlength(line, font=font)
        x = (width - line_width) // 2
        draw.text(
            (x, y), line, font=font, fill=(255, 255, 255, 255),
            stroke_width=stroke_w, stroke_fill=(10, 10, 14, 255),
        )
        y += line_height

    # ------------------------------------------------------ accent bar
    if accent:
        bar_width = min(int(width * 0.16), 220)
        bar_y = block_top + block_height + int(font.size * 0.45)
        if bar_y < height - BOTTOM_SAFE + 80:
            draw.rounded_rectangle(
                [(width - bar_width) // 2, bar_y,
                 (width + bar_width) // 2, bar_y + 10],
                radius=5, fill=(255, 255, 255, 230),
            )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, "PNG")
    return out_path
