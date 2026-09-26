"""Automatic quality control for generated covers."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageStat

from app.cover.compose import CoverLayout


@dataclass
class CoverCheck:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    fmt: str = ""
    mean_luma: float = 0.0
    stddev_luma: float = 0.0
    contrast_ratio: float = 0.0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "width": self.width, "height": self.height, "format": self.fmt,
                "mean_luma": round(self.mean_luma, 2), "stddev_luma": round(self.stddev_luma, 2),
                "contrast_ratio": round(self.contrast_ratio, 2)}


def validate_cover(path: Path, *, expected_width: int = 1080, expected_height: int = 1920,
                   layout: Optional[CoverLayout] = None) -> CoverCheck:
    check = CoverCheck(ok=True)
    if not path.is_file() or path.stat().st_size < 1024:
        check.ok = False
        check.errors.append("cover file missing or suspiciously small")
        return check
    try:
        with Image.open(path) as probe:
            probe.verify()          # detects truncation/corruption
        with Image.open(path) as img:
            img.load()
            check.width, check.height = img.size
            check.fmt = (img.format or "").upper()
            gray = img.convert("L")
    except Exception as exc:  # noqa: BLE001 - any decode failure is a hard fail
        check.ok = False
        check.errors.append(f"cover is not a readable image: {exc}")
        return check

    if check.fmt not in {"PNG", "JPEG"}:
        check.ok = False
        check.errors.append(f"unsupported cover format {check.fmt}")
    if (check.width, check.height) != (expected_width, expected_height):
        check.ok = False
        check.errors.append(
            f"cover is {check.width}x{check.height}, expected {expected_width}x{expected_height}")

    stat = ImageStat.Stat(gray)
    check.mean_luma = stat.mean[0]
    check.stddev_luma = stat.stddev[0]
    if check.stddev_luma < 6:
        check.ok = False
        check.errors.append("cover looks blank/flat (no tonal variation)")
    if check.mean_luma < 6:
        check.ok = False
        check.errors.append("cover is almost completely black")
    if check.mean_luma > 249:
        check.ok = False
        check.errors.append("cover is almost completely white")

    if layout is not None:
        check.contrast_ratio = layout.contrast_ratio
        x0, y0, x1, y1 = layout.text_box
        sx0, sy0, sx1, sy1 = layout.safe_box
        if x0 < sx0 or y0 < sy0 or x1 > sx1 or y1 > sy1:
            check.ok = False
            check.errors.append("headline leaves the TikTok-safe region")
        if y1 > check.height * 0.80:
            check.warnings.append("headline sits low; TikTok UI may overlap it")
        if layout.font_size < check.height * 0.05:
            check.warnings.append("headline font is small for mobile viewing")
        if layout.contrast_ratio < 3.0:
            check.warnings.append(
                f"low background contrast ({layout.contrast_ratio:.1f}:1) - stroke/scrim applied")
        if len(layout.lines) > 3:
            check.warnings.append("headline wraps to more than 3 lines")
    return check
