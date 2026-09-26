"""Font discovery.

No font files are shipped in the repository. We use fonts that are already
present on the operating system (Windows system fonts, or DejaVu/Liberation/
Noto on Linux), all of which support German umlauts.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

WINDOWS_BOLD = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\SegoeUIBlack.ttf",
    r"C:\Windows\Fonts\seguibl.ttf",
    r"C:\Windows\Fonts\bahnschrift.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
    r"C:\Windows\Fonts\impact.ttf",
]
WINDOWS_REGULAR = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
]
UNIX_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
UNIX_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


class FontNotFoundError(RuntimeError):
    pass


def _first_existing(paths: List[str]) -> Optional[Path]:
    for p in paths:
        path = Path(p)
        if path.is_file():
            return path
    return None


def _scan_font_dirs(needle: str) -> Optional[Path]:
    roots = [Path(r"C:\Windows\Fonts"), Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
             Path.home() / ".fonts", Path.home() / ".local/share/fonts"]
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith((".ttf", ".otf")) and needle in f.lower():
                    return Path(dirpath) / f
    return None


def find_bold_font(configured: Optional[Path] = None) -> Path:
    if configured and Path(configured).is_file():
        return Path(configured)
    hit = _first_existing(WINDOWS_BOLD) or _first_existing(UNIX_BOLD) or _scan_font_dirs("bold")
    if hit:
        return hit
    raise FontNotFoundError(
        "No bold TrueType font found. Install DejaVu/Liberation fonts or set FONT_BOLD in .env.")


def find_regular_font(configured: Optional[Path] = None) -> Path:
    if configured and Path(configured).is_file():
        return Path(configured)
    hit = (_first_existing(WINDOWS_REGULAR) or _first_existing(UNIX_REGULAR)
           or _scan_font_dirs("dejavusans") or _scan_font_dirs("regular"))
    if hit:
        return hit
    return find_bold_font()
