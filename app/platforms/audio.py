"""Shared audio master for the music platforms (SoundCloud / Spotify / Apple Music).

The audio is extracted from the finished video **once** into
``output/<job>/audio_master.flac`` and then reused by every enabled music
platform. Nothing is re-transcoded when a compatible master already exists.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import Settings
from app.video.ffmpeg import find_ffmpeg, find_ffprobe, run

log = logging.getLogger(__name__)

MASTER_NAME = "audio_master"


@dataclass
class AudioMaster:
    path: Path
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bit_depth: Optional[int] = None
    duration: float = 0.0
    size_bytes: int = 0
    reused: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"path": str(self.path), "codec": self.codec, "sample_rate": self.sample_rate,
                "channels": self.channels, "bit_depth": self.bit_depth,
                "duration": round(self.duration, 3), "size_bytes": self.size_bytes,
                "reused": self.reused, **self.extra}


def probe_audio(path: Path, settings: Settings) -> Dict[str, Any]:
    out = run([find_ffprobe(settings), "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", str(path)], timeout=180).stdout
    data = json.loads(out or "{}")
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = data.get("format", {})
    bits = audio.get("bits_per_raw_sample") or audio.get("bits_per_sample")
    return {"codec": audio.get("codec_name", ""),
            "sample_rate": int(audio.get("sample_rate") or 0),
            "channels": int(audio.get("channels") or 0),
            "bit_depth": int(bits) if bits and str(bits).isdigit() and int(bits) > 0 else None,
            "duration": float(fmt.get("duration") or 0.0),
            "size_bytes": path.stat().st_size}


def build_audio_master(video: Path, out_dir: Path, settings: Settings,
                       fmt: str = "flac") -> AudioMaster:
    """Extract (or reuse) the lossless audio master for all music platforms."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = {"flac": ".flac", "wav": ".wav"}.get(fmt.lower(), ".flac")
    master = out_dir / f"{MASTER_NAME}{ext}"

    if master.is_file() and master.stat().st_size > 1024 and \
            master.stat().st_mtime >= video.stat().st_mtime:
        info = probe_audio(master, settings)
        return AudioMaster(path=master, reused=True, **info)

    ffmpeg = find_ffmpeg(settings)
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-i", str(video), "-vn", "-map", "0:a:0"]
    if ext == ".flac":
        cmd += ["-c:a", "flac", "-compression_level", "8"]
    else:
        cmd += ["-c:a", "pcm_s24le"]
    cmd += [str(master)]
    log.info("extracting the shared audio master -> %s", master.name)
    run(cmd, timeout=3600)
    return AudioMaster(path=master, reused=False, **probe_audio(master, settings))
