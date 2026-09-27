"""Validate the final MP4 against the current documented Instagram Reels specs.

Meta's published Reels requirements (verified 2026-09-27):

* container MOV or MP4 (MPEG-4 Part 14), no edit lists, moov atom at the front
* video codec H.264 (progressive scan, closed GOP, 4:2:0 chroma) or HEVC
* audio codec AAC, 48 kHz, up to 2 channels (stereo)
* duration 3 s – 15 min (5–90 s is the range eligible for the Reels tab)
* frame rate 23–60 fps
* resolution: minimum 540x960, recommended 1080x1920, max width 1920
* aspect ratio 0.01:1 – 10:1 (9:16 recommended; other ratios get cropped/padded
  by Instagram)
* maximum file size 1 GB
* video bitrate up to 25 Mbps, audio bitrate up to 128 kbps

TikTok's limits are different, so this is checked separately. If the existing
final_tiktok.mp4 satisfies both, it is reused - no second transcode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.config import Settings
from app.video.ffmpeg import FFmpegError, MediaInfo, probe

MAX_FILE_BYTES = 1 * 1024 * 1024 * 1024        # 1 GB
MIN_DURATION_S = 3.0
MAX_DURATION_S = 15 * 60                        # 15 minutes
REELS_TAB_MIN_S = 5.0
REELS_TAB_MAX_S = 90.0
MIN_WIDTH, MIN_HEIGHT = 540, 960
MAX_WIDTH = 1920
MIN_FPS, MAX_FPS = 23.0, 60.0
OK_VIDEO_CODECS = {"h264", "hevc"}
OK_AUDIO_CODECS = {"aac"}
OK_CONTAINERS = ("mov,mp4,m4a,3gp,3g2,mj2",)
MIN_ASPECT, MAX_ASPECT = 0.01, 10.0


@dataclass
class ReelsCheck:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: Optional[MediaInfo] = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "info": self.info.as_dict() if self.info else None}


def validate_reel(path: Path, settings: Settings) -> ReelsCheck:
    check = ReelsCheck(ok=True)
    if not path.is_file():
        return ReelsCheck(ok=False, errors=[f"file missing: {path}"])
    size = path.stat().st_size
    try:
        info = probe(path, settings)
    except FFmpegError as exc:
        return ReelsCheck(ok=False, errors=[f"ffprobe rejected the file: {exc}"])
    check.info = info

    if path.suffix.lower() not in {".mp4", ".mov"} or info.container not in OK_CONTAINERS:
        check.ok = False
        check.errors.append(f"Instagram Reels require MP4/MOV (got '{info.container}')")
    if size > MAX_FILE_BYTES:
        check.ok = False
        check.errors.append(f"file is {size / 1e9:.2f} GB, Instagram allows max 1 GB")
    if info.video_codec.lower() not in OK_VIDEO_CODECS:
        check.ok = False
        check.errors.append(f"video codec '{info.video_codec}' is not H.264/HEVC")
    if info.has_audio and (info.audio_codec or "").lower() not in OK_AUDIO_CODECS:
        check.ok = False
        check.errors.append(f"audio codec '{info.audio_codec}' is not AAC")
    if info.duration < MIN_DURATION_S:
        check.ok = False
        check.errors.append(f"duration {info.duration:.1f}s is below the 3 s minimum")
    if info.duration > MAX_DURATION_S:
        check.ok = False
        check.errors.append(f"duration {info.duration:.1f}s exceeds the 15 min maximum")
    if not (REELS_TAB_MIN_S <= info.duration <= REELS_TAB_MAX_S):
        check.warnings.append(
            f"duration {info.duration:.1f}s is outside 5-90 s, so the Reel may not be "
            "eligible for the Reels tab")
    if info.width < MIN_WIDTH or info.height < MIN_HEIGHT:
        check.ok = False
        check.errors.append(
            f"resolution {info.width}x{info.height} is below the 540x960 minimum")
    if info.width > MAX_WIDTH:
        check.warnings.append(f"width {info.width}px exceeds the recommended maximum of 1920px")
    if info.fps and not (MIN_FPS <= info.fps <= MAX_FPS):
        check.ok = False
        check.errors.append(f"frame rate {info.fps:.2f} is outside the documented 23-60 fps")
    aspect = info.aspect or 0
    if aspect and not (MIN_ASPECT <= aspect <= MAX_ASPECT):
        check.ok = False
        check.errors.append(f"aspect ratio {aspect:.3f} is outside the supported range")
    elif abs(aspect - 9 / 16) > 0.02:
        check.warnings.append(
            f"aspect ratio {aspect:.3f} is not 9:16; Instagram may crop or pad the Reel")
    if info.bitrate and info.bitrate > 25_000_000:
        check.warnings.append(f"video bitrate {info.bitrate / 1e6:.1f} Mbps exceeds 25 Mbps")
    return check
