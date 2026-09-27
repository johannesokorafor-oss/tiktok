"""Shared release-package builder for the distributor platforms.

Spotify and Apple Music have **no public artist-release ingestion API** - both
direct artists to a music distributor. This application therefore prepares a
complete release package and never pretends to upload. Artwork is never
generated; the user supplies it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import PlatformMode, PlatformStatus, Settings
from app.platforms.audio import AudioMaster, build_audio_master
from app.platforms.base import PlatformResult

ARTWORK_NOTE = ("Artwork must be supplied by the user/distributor - this application never "
                "generates cover images. Typical requirement: square JPG/PNG, 3000x3000 px.")


def missing_release_fields(artist: str, album: str) -> List[str]:
    missing = []
    if not artist.strip():
        missing.append("artist name (set SPOTIFY_ARTIST / APPLE_MUSIC_ARTIST or supply it in "
                       "the distributor form)")
    if not album.strip():
        missing.append("album/release title (optional - the track title is used otherwise)")
    return missing


def build_release_package(platform: str, video: Path, plan, out_dir: Path, settings: Settings,
                          *, artist: str, album: str, genre: str,
                          release_date: str = "", explicit: Optional[bool] = None,
                          distributor_note: str = "") -> Tuple[PlatformResult, AudioMaster]:
    """audio_master.flac -> <platform>/ package with audio, metadata and notes."""
    package = out_dir / platform
    package.mkdir(parents=True, exist_ok=True)

    master = build_audio_master(video, out_dir, settings)
    audio_name = f"{platform}_ready{master.path.suffix}"
    target = package / audio_name
    if not target.exists() or target.stat().st_size != master.path.stat().st_size:
        target.write_bytes(master.path.read_bytes())

    title = (getattr(plan, "tiktok_title", "") or video.stem).strip()
    description = (getattr(plan, "caption", "") or "").strip()
    tags = [t.lstrip("#") for t in (getattr(plan, "hashtags", []) or [])]
    language = getattr(plan, "language", "")
    missing = missing_release_fields(artist, album)

    caption_path = package / f"caption_{platform}.txt"
    caption_path.write_text(
        f"TRACK TITLE\n{title}\n\nARTIST\n{artist or '(missing - supply manually)'}\n\n"
        f"RELEASE / ALBUM\n{album or title}\n\nNOTES / DESCRIPTION\n{description}\n\n"
        f"TAGS\n{' '.join('#' + t for t in tags)}\n\nLANGUAGE\n{language}\n\n"
        f"ARTWORK\n{ARTWORK_NOTE}\n\nHOW TO RELEASE\n{distributor_note}\n",
        encoding="utf-8")

    metadata: Dict[str, Any] = {
        "platform": platform,
        "mode": PlatformMode.PREPARE_ONLY.value,
        "status": PlatformStatus.READY_FOR_DISTRIBUTION.value,
        "track_title": title,
        "artist": artist,
        "album": album or title,
        "description": description,
        "tags": tags,
        "language": language,
        "genre": genre,
        "release_date": release_date,
        "explicit": explicit,
        "audio_path": str(target),
        "audio_master": master.as_dict(),
        "caption_path": str(caption_path),
        "artwork": ARTWORK_NOTE,
        "distribution": distributor_note,
        "api_upload_supported": False,
        "api_calls_made": 0,
        "auto_publish": False,
        "missing_fields": missing,
        "prepared_at": time.time(),
    }
    meta_path = package / f"{platform}_metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    result = PlatformResult(platform, PlatformMode.PREPARE_ONLY,
                            PlatformStatus.READY_FOR_DISTRIBUTION,
                            artifacts=[str(target), str(caption_path), str(meta_path)],
                            metadata=metadata,
                            message=distributor_note)
    if missing:
        result.message += " | supply manually: " + "; ".join(missing)
    return result, master
