"""Stage B of the cover pipeline: deterministic professional typography.

The AI model only produces a *background*; every glyph on the final cover is
rendered here with Pillow, which guarantees correct spelling, umlauts and
predictable layout.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

from app.cover.fonts import find_bold_font, find_regular_font
from app.cover.presets import CoverStyle


@dataclass
class CoverLayout:
    lines: List[str]
    font_size: int
    text_box: Tuple[int, int, int, int]
    safe_box: Tuple[int, int, int, int]
    contrast_ratio: float


@dataclass
class ComposedCover:
    image: Image.Image
    layout: CoverLayout

    def save(self, path: Path, fmt: str = "PNG", quality: int = 95) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt.upper() in {"JPG", "JPEG"}:
            self.image.convert("RGB").save(path, format="JPEG", quality=quality, subsampling=0)
        else:
            self.image.save(path, format="PNG", optimize=True)
        return path


# --------------------------------------------------------------------------
def _rel_luminance(rgb: Sequence[float]) -> float:
    def ch(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(rgb[0]), ch(rgb[1]), ch(rgb[2]))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: Sequence[float], bg: Sequence[float]) -> float:
    l1, l2 = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                tracking: float) -> int:
    if not text:
        return 0
    base = draw.textlength(text, font=font)
    return int(base + tracking * max(0, len(text) - 1))


def wrap_words(draw: ImageDraw.ImageDraw, words: Sequence[str], font: ImageFont.FreeTypeFont,
               max_width: int, tracking: float, max_lines: int = 3) -> Optional[List[str]]:
    """Greedy wrap; returns None if a single word does not fit."""
    lines: List[str] = []
    current = ""
    for w in words:
        if _text_width(draw, w, font, tracking) > max_width:
            return None
        candidate = (current + " " + w).strip()
        if _text_width(draw, candidate, font, tracking) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        return None
    return lines


def balance_lines(words: Sequence[str]) -> List[List[str]]:
    """Preferred line groupings for 2-4 word hooks (visual hierarchy)."""
    n = len(words)
    w = list(words)
    if n <= 1:
        return [w]
    if n == 2:
        return [[w[0]], [w[1]]]
    if n == 3:
        return [[w[0], w[1]], [w[2]]]
    return [[w[0], w[1]], [w[2], w[3]]]


def _draw_line(draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
               font: ImageFont.FreeTypeFont, fill, tracking: float,
               stroke_width: int = 0, stroke_fill=None) -> int:
    """Draw text with manual letter spacing; returns the advance width."""
    if tracking <= 0.01:
        draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width,
                  stroke_fill=stroke_fill)
        return int(draw.textlength(text, font=font))
    cx = x
    for ch in text:
        draw.text((cx, y), ch, font=font, fill=fill, stroke_width=stroke_width,
                  stroke_fill=stroke_fill)
        cx += draw.textlength(ch, font=font) + tracking
    return int(cx - x - tracking)


class CoverComposer:
    def __init__(self, style: CoverStyle, font_bold: Optional[Path] = None,
                 font_regular: Optional[Path] = None) -> None:
        self.style = style
        self.font_bold_path = find_bold_font(font_bold)
        self.font_regular_path = find_regular_font(font_regular)

    # ----------------------------------------------------------------
    def _prepare_background(self, data_or_image, width: int, height: int) -> Image.Image:
        if isinstance(data_or_image, Image.Image):
            img = data_or_image.convert("RGB")
        else:
            img = Image.open(io.BytesIO(data_or_image)).convert("RGB")
        # cover-fit crop to the exact target aspect
        target = width / height
        w, h = img.size
        if abs(w / h - target) > 0.001:
            if w / h > target:
                new_w = int(h * target)
                left = (w - new_w) // 2
                img = img.crop((left, 0, left + new_w, h))
            else:
                new_h = int(w / target)
                top = int((h - new_h) * 0.35)  # bias towards the upper part of the frame
                img = img.crop((0, top, w, top + new_h))
        return img.resize((width, height), Image.LANCZOS)

    # ----------------------------------------------------------------
    def compose(self, background, hook_words: Sequence[str], *, width: int = 1080,
                height: int = 1920, kicker: Optional[str] = None) -> ComposedCover:
        st = self.style
        img = self._prepare_background(background, width, height)

        words = [w.upper() if st.uppercase else w for w in hook_words if w.strip()]
        if not words:
            raise ValueError("cover hook is empty")

        ml, mt, mr, mb = st.safe_margins
        safe_l, safe_t = int(width * ml), int(height * mt)
        safe_r, safe_b = int(width * (1 - mr)), int(height * (1 - mb))
        max_text_w = safe_r - safe_l
        max_text_h = int(height * 0.42)

        draw_probe = ImageDraw.Draw(img)
        grouping = balance_lines(words)
        best: Optional[Tuple[int, List[str], ImageFont.FreeTypeFont, float]] = None

        hi = int(height * st.max_font_ratio)
        lo = int(height * st.min_font_ratio)
        hard_lo = max(18, int(height * 0.030))   # absolute floor for very long words
        for size in range(hi, hard_lo - 1, -2):
            font = ImageFont.truetype(str(self.font_bold_path), size)
            tracking = st.letter_spacing * size
            lines = [" ".join(g) for g in grouping]
            if any(_text_width(draw_probe, ln, font, tracking) > max_text_w for ln in lines):
                wrapped = wrap_words(draw_probe, words, font, max_text_w, tracking, max_lines=3)
                if wrapped is None:
                    continue
                lines = wrapped
            line_h = int(size * st.line_spacing)
            if line_h * len(lines) > max_text_h:
                continue
            best = (size, lines, font, tracking)
            if size < lo:
                # very long words: keep readable by tightening tracking instead of
                # shrinking further, and record that we went below the preset minimum
                pass
            break

        if best is None:
            size = hard_lo
            font = ImageFont.truetype(str(self.font_bold_path), size)
            tracking = 0.0
            lines = (wrap_words(draw_probe, words, font, max_text_w, tracking, max_lines=4)
                     or [" ".join(words)])
            best = (size, lines, font, tracking)

        size, lines, font, tracking = best
        line_h = int(size * st.line_spacing)
        block_h = line_h * len(lines)
        block_top = max(safe_t, int(height * st.top_ratio))
        if block_top + block_h > safe_b:
            block_top = max(safe_t, safe_b - block_h)

        # -------- adaptive contrast treatment ----------------
        region = img.crop((safe_l, block_top, safe_r, min(height, block_top + block_h)))
        bg_mean = ImageStat.Stat(region).mean[:3]
        ratio_plain = contrast_ratio(st.text_color, bg_mean)

        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)

        if st.gradient_scrim:
            strength = st.scrim_strength
            if ratio_plain < 4.5:
                strength = min(0.92, strength + 0.18)
            grad_bottom = min(height, block_top + block_h + int(height * 0.12))
            for y in range(0, grad_bottom):
                t = y / max(1, grad_bottom)
                # strongest at the very top, easing out below the headline block
                a = int(255 * strength * (1.0 - t) ** 1.25)
                od.line([(0, y), (width, y)], fill=(0, 0, 0, a))

        if st.glow > 0:
            glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow_layer)
            cx = width // 2
            cy = block_top + block_h // 2
            rx, ry = int(width * 0.52), int(block_h * 0.95)
            gd.ellipse([cx - rx, cy - ry, cx + rx, cy + ry],
                       fill=(*st.accent_color, int(70 * st.glow)))
            glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(width * 0.10))
            overlay = Image.alpha_composite(overlay, glow_layer)
            od = ImageDraw.Draw(overlay)

        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # recompute contrast after the scrim and escalate the treatment if needed
        region = img.crop((safe_l, block_top, safe_r, min(height, block_top + block_h)))
        bg_mean = ImageStat.Stat(region).mean[:3]
        ratio = contrast_ratio(st.text_color, bg_mean)
        stroke_w = int(size * st.stroke_ratio)
        if ratio < 4.5:
            stroke_w = max(stroke_w, int(size * 0.075))

        # -------- shadow pass ----------------
        if st.shadow:
            shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            sd = ImageDraw.Draw(shadow)
            y = block_top
            for ln in lines:
                lw = _text_width(draw, ln, font, tracking)
                x = (width - lw) // 2
                _draw_line(sd, x, y + int(size * 0.035), ln, font,
                           (0, 0, 0, st.shadow_opacity), tracking)
                y += line_h
            shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.06))
            img = Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB")
            draw = ImageDraw.Draw(img)

        # -------- accent bar ----------------
        if st.accent_bar:
            bar_w = int(width * 0.16)
            bar_y = block_top - int(size * 0.42)
            draw.rectangle([(width - bar_w) // 2, bar_y, (width + bar_w) // 2, bar_y + max(3, size // 26)],
                           fill=st.accent_color)

        # -------- headline ----------------
        y = block_top
        min_x, max_x = width, 0
        last_line_idx = len(lines) - 1
        for idx, ln in enumerate(lines):
            lw = _text_width(draw, ln, font, tracking)
            x = (width - lw) // 2
            min_x, max_x = min(min_x, x), max(max_x, x + lw)
            use_accent = st.accent_last_word and idx == last_line_idx and len(lines) > 1
            fill = st.accent_color if use_accent else st.text_color
            _draw_line(draw, x, y, ln, font, fill, tracking,
                       stroke_width=stroke_w, stroke_fill=st.stroke_color)
            y += line_h

        # -------- kicker (optional small label) ----------------
        if kicker:
            kfont = ImageFont.truetype(str(self.font_regular_path), max(18, int(size * 0.22)))
            kw = draw.textlength(kicker, font=kfont)
            draw.text(((width - kw) // 2, max(8, block_top - int(size * 0.55))), kicker,
                      font=kfont, fill=st.accent_color)

        # -------- vignette + grain ----------------
        if st.vignette:
            mask = Image.new("L", (width, height), 0)
            md = ImageDraw.Draw(mask)
            md.ellipse([-int(width * 0.30), -int(height * 0.15),
                        int(width * 1.30), int(height * 1.15)], fill=255)
            mask = mask.filter(ImageFilter.GaussianBlur(min(width, height) * 0.10))
            img = Image.composite(img, Image.new("RGB", (width, height), (0, 0, 0)), mask)
        if st.grain > 0:
            noise = Image.effect_noise((width, height), 16).convert("L")
            img = Image.blend(img, Image.merge("RGB", (noise, noise, noise)), st.grain)

        layout = CoverLayout(
            lines=lines,
            font_size=size,
            text_box=(min_x, block_top, max_x, block_top + block_h),
            safe_box=(safe_l, safe_t, safe_r, safe_b),
            contrast_ratio=ratio,
        )
        return ComposedCover(image=img, layout=layout)
