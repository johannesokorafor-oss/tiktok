"""Validation of the processed video before anything is uploaded."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.config import Settings
from app.video.ffmpeg import FFmpegError, MediaInfo, find_ffmpeg, probe, run

#: TikTok documented limits for the Content Posting API
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024        # 4 GB
MAX_DURATION_S = 600                            # 10 minutes (account dependent)
MIN_DURATION_S = 3
MIN_SHORT_SIDE = 360


@dataclass
class VideoCheck:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: Optional[MediaInfo] = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "info": self.info.as_dict() if self.info else None}


def validate_video(path: Path, settings: Settings, *, source_had_audio: bool = True,
                   expect_width: Optional[int] = None,
                   expect_height: Optional[int] = None,
                   deep: bool = True) -> VideoCheck:
    check = VideoCheck(ok=True)
    if not path.is_file():
        return VideoCheck(ok=False, errors=[f"output file missing: {path}"])
    size = path.stat().st_size
    if size < 10_000:
        return VideoCheck(ok=False, errors=[f"output file is only {size} bytes"])

    try:
        info = probe(path, settings)
    except FFmpegError as exc:
        return VideoCheck(ok=False, errors=[f"ffprobe rejected the file: {exc}"])
    check.info = info

    if info.duration < MIN_DURATION_S:
        check.warnings.append(
            f"duration {info.duration:.2f}s is below TikTok's usual 3s minimum")
    if info.duration > MAX_DURATION_S:
        check.ok = False
        check.errors.append(f"duration {info.duration:.1f}s exceeds the API maximum of 600s")
    if size > MAX_FILE_BYTES:
        check.ok = False
        check.errors.append("file exceeds the 4 GB API maximum")
    if info.width <= 0 or info.height <= 0:
        check.ok = False
        check.errors.append("invalid video dimensions")
    elif min(info.width, info.height) < MIN_SHORT_SIDE:
        check.ok = False
        check.errors.append(f"short side {min(info.width, info.height)}px is below 360px")
    if expect_width and expect_height and (info.width, info.height) != (expect_width, expect_height):
        check.warnings.append(
            f"resolution {info.width}x{info.height} differs from target {expect_width}x{expect_height}")
    if info.video_codec.lower() not in {"h264", "hevc", "vp9", "av1"}:
        check.warnings.append(f"unusual video codec '{info.video_codec}' for TikTok")
    if not (15 <= info.fps <= 70):
        check.warnings.append(f"frame rate {info.fps:.2f} is outside the typical 23-60 fps range")
    if source_had_audio and not info.has_audio:
        check.ok = False
        check.errors.append("source had audio but the processed file has none")
    if info.has_audio and info.audio_codec and info.audio_codec.lower() != "aac":
        check.warnings.append(f"audio codec is '{info.audio_codec}', AAC is recommended")

    if deep:
        # decode pass: catches truncated/corrupt streams that ffprobe accepts
        try:
            ffmpeg = find_ffmpeg(settings)
            run([ffmpeg, "-v", "error", "-nostdin", "-xerror", "-i", str(path),
                 "-f", "null", "-"], timeout=3600)
        except FFmpegError as exc:
            check.ok = False
            check.errors.append(f"decode check failed (corrupt stream): {exc}")
    return check
