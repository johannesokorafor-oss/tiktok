"""Tolerant parser for the human-written metadata sidecar file.

Supports canonical headings:

    TITLE:
    DESCRIPTION:
    IMAGE_PROMPT:

plus flexible variants (Title / Titel / Caption / Beschreibung / Prompt /
Bildprompt / Image Prompt / Visual ...), case-insensitive, with ``:`` or ``-``
separators and inline or block values.  When no headings exist at all, an
intelligent fallback splits the natural text into title / description /
image prompt.

The source file is never modified.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

TITLE_KEYS = ["title", "titel", "video title", "videotitel", "hook", "headline", "überschrift", "ueberschrift"]
DESCRIPTION_KEYS = ["description", "beschreibung", "caption", "content", "content description",
                    "body", "text", "script", "skript", "inhalt", "beschreibungstext"]
PROMPT_KEYS = ["image_prompt", "image prompt", "imageprompt", "prompt", "bildprompt", "bild prompt",
               "bild-prompt", "visual", "visual prompt", "cover prompt", "thumbnail prompt", "bild"]
HASHTAG_KEYS = ["hashtags", "tags", "hashtag"]
HOOK_KEYS = ["cover_text", "cover text", "covertext", "hook_text", "hook text", "cover hook"]
STYLE_KEYS = ["style", "stil", "preset", "style_preset"]

_ALL_KEYS: Dict[str, str] = {}
for k in TITLE_KEYS:
    _ALL_KEYS[k] = "title"
for k in DESCRIPTION_KEYS:
    _ALL_KEYS[k] = "description"
for k in PROMPT_KEYS:
    _ALL_KEYS[k] = "image_prompt"
for k in HASHTAG_KEYS:
    _ALL_KEYS[k] = "hashtags"
for k in HOOK_KEYS:
    _ALL_KEYS[k] = "cover_text"
for k in STYLE_KEYS:
    _ALL_KEYS[k] = "style"

# longest keys first so "image prompt" wins over "prompt"
_SORTED_KEYS = sorted(_ALL_KEYS, key=len, reverse=True)
_KEY_ALT = "|".join(re.escape(k) for k in _SORTED_KEYS)
#: "TITLE:", "Title -", "## Title", "**Titel**" ... separator optional for
#: markdown/bold headings that stand alone on their line.
_HEADING_RE = re.compile(
    r"^\s*(?:(?P<hash>#{1,6})\s*)?\**\s*(?P<key>" + _KEY_ALT + r")\s*\**\s*"
    r"(?:(?P<sep>[:\-\u2013])\s*(?P<inline>.*))?\s*$",
    re.IGNORECASE,
)


class ParseError(ValueError):
    """Raised when the metadata file cannot yield a usable title/description."""


@dataclass
class ParsedMetadata:
    title: str = ""
    description: str = ""
    image_prompt: str = ""
    hashtags: List[str] = field(default_factory=list)
    cover_text: Optional[str] = None
    style: Optional[str] = None
    source_file: str = ""
    video_file: str = ""
    raw_text: str = ""
    used_fallback: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_text", None)
        return d


def _clean_block(lines: List[str]) -> str:
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_hashtags(text: str) -> List[str]:
    tags = re.findall(r"(?<!\w)#([0-9A-Za-zÄÖÜäöüß_]{2,40})", text)
    seen, out = set(), []
    for t in tags:
        low = t.lower()
        if low not in seen:
            seen.add(low)
            out.append(t)
    return out


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def parse_text(raw: str, *, source_file: str = "", video_file: str = "") -> ParsedMetadata:
    """Parse metadata text into a normalized representation."""
    if raw is None:
        raise ParseError("metadata file is empty")
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ParseError("metadata file is empty")

    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    preamble: List[str] = []

    for line in text.split("\n"):
        m = _HEADING_RE.match(line)
        if m and (m.group("sep") or m.group("hash") or "*" in line
                  or line.strip().lower() in _ALL_KEYS):
            current = _ALL_KEYS[m.group("key").lower()]
            sections.setdefault(current, [])
            inline = (m.group("inline") or "").strip()
            if inline:
                sections[current].append(inline)
            continue
        if current is None:
            preamble.append(line)
        else:
            sections[current].append(line)

    meta = ParsedMetadata(source_file=source_file, video_file=video_file, raw_text=raw)
    for key, lines in sections.items():
        value = _clean_block(lines)
        if key == "hashtags":
            meta.hashtags = _extract_hashtags(value) or [
                t.strip().lstrip("#") for t in re.split(r"[,\s]+", value) if t.strip()
            ]
        elif key == "cover_text":
            meta.cover_text = value or None
        elif key == "style":
            meta.style = (value or "").strip().upper() or None
        else:
            setattr(meta, key, value)

    leftover = _clean_block(preamble)

    # ---------------- intelligent fallback ----------------
    if not meta.title and not meta.description and not meta.image_prompt:
        meta.used_fallback = True
        body = leftover or text.strip()
        sentences = _split_sentences(body)
        if not sentences:
            raise ParseError("metadata file contains no usable text")
        lines = [l.strip() for l in body.split("\n") if l.strip()]
        meta.title = lines[0][:200] if lines else sentences[0][:200]
        rest = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        meta.description = rest or body.strip()
    else:
        if not meta.title:
            meta.used_fallback = True
            base = meta.description or leftover or meta.image_prompt
            first = _split_sentences(base)
            meta.title = (first[0] if first else base)[:200]
        if not meta.description:
            meta.used_fallback = True
            meta.description = leftover.strip() or meta.title
        if not meta.image_prompt:
            meta.used_fallback = True
            meta.image_prompt = ""  # the prompt builder derives one from title+description

    if not meta.hashtags:
        meta.hashtags = _extract_hashtags(text)

    meta.title = meta.title.strip()
    meta.description = meta.description.strip()
    meta.image_prompt = meta.image_prompt.strip()

    if not meta.title or not meta.description:
        raise ParseError("could not determine both a title and a description")
    return meta


def parse_file(path: Path, *, video_file: str = "") -> ParsedMetadata:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    return parse_text(raw, source_file=str(path), video_file=video_file)
