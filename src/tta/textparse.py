"""Tolerant parser for the metadata text file that accompanies each video.

Accepted shapes (case-insensitive, `:` or `=` separators, optional markdown
heading markers, values inline or on following lines)::

    TITLE:
    My great video

    DESCRIPTION:
    Something longer ...

    IMAGE_PROMPT:
    a misty forest at dawn

    ---

    Title: My great video
    Description: Something longer
    Prompt: a misty forest at dawn

If no keys are found at all, the first non-empty line becomes the title and
the rest becomes the description.  The source file is opened read-only and is
never modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# canonical key <- accepted aliases (lowercase)
_KEY_ALIASES = {
    "title": {"title", "titel"},
    "description": {"description", "beschreibung", "desc", "caption"},
    "image_prompt": {
        "image_prompt",
        "imageprompt",
        "image prompt",
        "prompt",
        "bildprompt",
        "bild_prompt",
        "cover_prompt",
        "coverprompt",
    },
}

_ALIAS_TO_KEY = {alias: key for key, aliases in _KEY_ALIASES.items() for alias in aliases}

# "TITLE:" / "## Title:" / "Title =" / "**Title:**" at the start of a line
_KEY_LINE = re.compile(
    r"""^\s{0,3}
        (?:\#{1,6}\s*)?              # optional markdown heading
        (?:\*{1,2})?                 # optional bold marker
        (?P<key>[A-Za-zÄÖÜäöü_ ]{2,30}?)
        (?:\*{1,2})?                 # optional closing bold marker
        \s*[:=]\s*
        (?P<value>.*)$""",
    re.VERBOSE,
)


@dataclass
class ParsedMetadata:
    title: str = ""
    description: str = ""
    image_prompt: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "image_prompt": self.image_prompt,
        }


def parse_text(raw: str) -> ParsedMetadata:
    """Parse raw metadata text into normalized fields."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    # strip a UTF-8 BOM if present
    raw = raw.lstrip("\ufeff")

    sections: dict[str, list[str]] = {}
    current: str | None = None
    free_lines: list[str] = []

    for line in raw.split("\n"):
        match = _KEY_LINE.match(line)
        if match:
            key_raw = match.group("key").strip().lower().replace("-", "_")
            key_norm = re.sub(r"\s+", " ", key_raw)
            canonical = _ALIAS_TO_KEY.get(key_norm) or _ALIAS_TO_KEY.get(
                key_norm.replace(" ", "_")
            )
            if canonical:
                current = canonical
                sections.setdefault(current, [])
                # drop a trailing bold marker, e.g. "**Description:** value"
                value = re.sub(r"^\*{1,2}\s*", "", match.group("value").strip())
                if value:
                    sections[current].append(value)
                continue
            # An unknown "Key:" line - treat as content of the active section.
        if current is not None:
            sections[current].append(line)
        else:
            free_lines.append(line)

    def _join(key: str) -> str:
        lines = sections.get(key, [])
        text = "\n".join(lines).strip()
        # collapse >2 consecutive blank lines
        return re.sub(r"\n{3,}", "\n\n", text)

    meta = ParsedMetadata(
        title=_squash_ws(_join("title")),
        description=_join("description"),
        image_prompt=_squash_ws(_join("image_prompt")),
    )

    # Fallback: no recognizable keys -> first line = title, rest = description
    if not sections:
        cleaned = [ln for ln in free_lines if ln.strip()]
        if cleaned:
            meta.title = _squash_ws(_strip_md(cleaned[0]))
            meta.description = "\n".join(cleaned[1:]).strip()
    return meta


def parse_file(path: str | Path) -> ParsedMetadata:
    """Read the file read-only (never modified) and parse it."""
    data = Path(path).read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return parse_text(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    return parse_text(data.decode("utf-8", errors="replace"))


def _squash_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_md(text: str) -> str:
    return re.sub(r"^[#>\-\*\s]+", "", text).strip()
