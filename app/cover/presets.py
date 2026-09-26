"""Cover design presets (typography + treatment parameters)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

from app.config import StylePreset


@dataclass(frozen=True)
class CoverStyle:
    name: str
    text_color: Tuple[int, int, int] = (255, 255, 255)
    accent_color: Tuple[int, int, int] = (255, 214, 140)
    stroke_color: Tuple[int, int, int] = (0, 0, 0)
    stroke_ratio: float = 0.055          # of font size
    shadow: bool = True
    shadow_opacity: int = 150
    gradient_scrim: bool = True          # dark gradient behind the headline block
    scrim_strength: float = 0.72
    vignette: bool = True
    grain: float = 0.035
    glow: float = 0.30                   # soft light bloom behind the headline
    letter_spacing: float = 0.02         # of font size
    line_spacing: float = 1.06
    uppercase: bool = True
    accent_last_word: bool = True
    accent_bar: bool = False
    top_ratio: float = 0.17              # headline block top position
    max_font_ratio: float = 0.135        # of image height
    min_font_ratio: float = 0.055
    safe_margins: Tuple[float, float, float, float] = (0.08, 0.10, 0.08, 0.22)  # l, t, r, b
    kicker: bool = False
    extras: dict = field(default_factory=dict)


PRESETS = {
    StylePreset.CINEMATIC_MYSTICAL: CoverStyle(
        name="CINEMATIC_MYSTICAL",
        text_color=(248, 246, 255),
        accent_color=(255, 214, 150),
        gradient_scrim=True,
        scrim_strength=0.70,
        glow=0.38,
        grain=0.04,
        letter_spacing=0.035,
        max_font_ratio=0.132,
    ),
    StylePreset.DARK_LUXURY: CoverStyle(
        name="DARK_LUXURY",
        text_color=(255, 252, 244),
        accent_color=(214, 170, 78),
        gradient_scrim=True,
        scrim_strength=0.80,
        glow=0.18,
        grain=0.025,
        letter_spacing=0.09,
        line_spacing=1.16,
        accent_bar=True,
        max_font_ratio=0.115,
        top_ratio=0.19,
    ),
    StylePreset.CLEAN_MODERN: CoverStyle(
        name="CLEAN_MODERN",
        text_color=(255, 255, 255),
        accent_color=(94, 214, 255),
        gradient_scrim=True,
        scrim_strength=0.58,
        glow=0.10,
        grain=0.015,
        letter_spacing=0.0,
        line_spacing=1.02,
        accent_last_word=True,
        max_font_ratio=0.14,
        top_ratio=0.15,
    ),
}


def get_preset(style: StylePreset) -> CoverStyle:
    if style == StylePreset.AUTO:
        style = StylePreset.CINEMATIC_MYSTICAL
    return PRESETS[style]
