"""PREPARE_ONLY: get everything ready for a *manual* Instagram upload.

This is the default Instagram behaviour and it touches **no Meta API at all**:

1. validate the finished video against the current documented Reels requirements
2. reuse ``final_tiktok.mp4`` when it already satisfies them
   (``instagram_ready.mp4`` is then a byte-identical copy - no second transcode)
3. only if it genuinely does not satisfy them, encode an Instagram-specific
   variant with FFmpeg
4. write ``caption_instagram.txt`` and ``instagram_preparation.json``

Result status: ``READY_FOR_MANUAL_UPLOAD``. You then upload the file in the
Instagram app yourself and pick your own cover there - this application never
creates a cover and never modifies the video for one.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import PlatformStatus, QualityMode, Settings
from app.instagram.validate import (MAX_DURATION_S, MAX_FILE_BYTES, MIN_DURATION_S,
                                    REELS_TAB_MAX_S, REELS_TAB_MIN_S, ReelsCheck, validate_reel)
from app.video.builder import crf_for, preset_for
from app.video.ffmpeg import FFmpegError, find_ffmpeg, probe, run

log = logging.getLogger(__name__)

MANUAL_NOTE = (
    "Instagram does not provide the same visible upload-to-inbox draft flow as TikTok. "
    "Prepare the video and caption here, then upload/publish manually in Instagram."
)


@dataclass
class PreparationResult:
    status: PlatformStatus
    video_path: Optional[Path] = None
    caption_path: Optional[Path] = None
    report_path: Optional[Path] = None
    caption: str = ""
    reused_tiktok_file: bool = True
    transcoded: bool = False
    reasons: List[str] = field(default_factory=list)
    validation: Dict[str, Any] = field(default_factory=dict)
    requirements: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    prepared_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "platform": "instagram",
            "mode": "PREPARE_ONLY",
            "status": self.status.value,
            "video_path": str(self.video_path) if self.video_path else None,
            "caption_path": str(self.caption_path) if self.caption_path else None,
            "caption": self.caption,
            "cover_policy": "MANUAL",
            "auto_publish": False,
            "api_calls_made": 0,
            "reused_tiktok_file": self.reused_tiktok_file,
            "transcoded_for_instagram": self.transcoded,
            "reasons": self.reasons,
            "validation": self.validation,
            "requirements": self.requirements,
            "note": MANUAL_NOTE,
            "error": self.error,
            "prepared_at": self.prepared_at,
        }


def reels_requirements(check: Optional[ReelsCheck] = None) -> Dict[str, Any]:
    """The documented requirements, shown to the user next to the result."""
    info = check.info if check else None
    return {
        "container": "MP4 or MOV (moov atom at the front, no edit lists)",
        "video_codec": "H.264 (progressive, closed GOP, 4:2:0) or HEVC",
        "audio_codec": "AAC, 48 kHz, up to 2 channels",
        "duration": f"{MIN_DURATION_S:.0f} s - {MAX_DURATION_S / 60:.0f} min "
                    f"({REELS_TAB_MIN_S:.0f}-{REELS_TAB_MAX_S:.0f} s for the Reels tab)",
        "frame_rate": "23-60 fps",
        "resolution": "min 540x960, recommended 1080x1920, max width 1920",
        "aspect_ratio": "0.01:1 - 10:1 (9:16 recommended)",
        "video_bitrate": "up to 25 Mbps",
        "audio_bitrate": "up to 128 kbps",
        "max_file_size": f"{MAX_FILE_BYTES // (1024 ** 3)} GB",
        "cover": "chosen by you inside Instagram - this app never generates one",
        "your_file": info.as_dict() if info else None,
    }


def _encode_for_instagram(source: Path, dest: Path, settings: Settings,
                          reasons: List[str]) -> None:
    """Instagram-specific encode, used only when the existing file is rejected."""
    info = probe(source, settings)
    ffmpeg = find_ffmpeg(settings)
    target_w, target_h = 1080, 1920
    fps = info.fps if 23 <= (info.fps or 0) <= 60 else 30.0
    vf = (f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
          f"fps={fps:.4f},format=yuv420p,setsar=1")
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-i", str(source),
           "-vf", vf, "-map", "0:v:0"]
    if info.has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264",
        "-preset", preset_for(settings.quality_mode, settings.video_preset),
        "-crf", str(crf_for(settings.quality_mode, settings.video_crf)),
        "-maxrate", "25M", "-bufsize", "50M",
        "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
        "-g", str(int(max(2, round(fps * 2)))),
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
        str(dest),
    ]
    log.info("encoding an Instagram-specific variant (%s)", "; ".join(reasons))
    run(cmd, timeout=7200)


def prepare_for_instagram(video: Path, caption: str, out_dir: Path,
                          settings: Settings) -> PreparationResult:
    """Produce instagram_ready.mp4 + caption_instagram.txt + instagram_preparation.json."""
    result = PreparationResult(status=PlatformStatus.FAILED, caption=caption)
    if not video.is_file():
        result.error = f"processed video not found: {video}"
        return result
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "instagram_ready.mp4"

    check = validate_reel(video, settings)
    result.validation = check.as_dict()

    try:
        if check.ok:
            # already fine for Instagram -> reuse, never transcode twice
            if dest.resolve() != video.resolve():
                shutil.copy2(video, dest)
            result.reused_tiktok_file = True
            result.reasons = ["final_tiktok.mp4 already meets the Instagram Reels "
                              "requirements - reused unchanged"]
        else:
            result.reasons = list(check.errors)
            _encode_for_instagram(video, dest, settings, result.reasons)
            result.reused_tiktok_file = False
            result.transcoded = True
            recheck = validate_reel(dest, settings)
            result.validation = recheck.as_dict()
            if not recheck.ok:
                result.error = ("the video still does not meet the Instagram Reels "
                                "requirements after conversion: " + "; ".join(recheck.errors))
                result.video_path = dest
                return result
    except FFmpegError as exc:
        result.error = f"Instagram conversion failed: {exc}"
        return result

    caption_path = out_dir / "caption_instagram.txt"
    caption_path.write_text(caption, encoding="utf-8")

    result.video_path = dest
    result.caption_path = caption_path
    result.requirements = reels_requirements(
        ReelsCheck(ok=True, info=probe(dest, settings)))
    result.status = PlatformStatus.READY_FOR_MANUAL_UPLOAD

    report_path = out_dir / "instagram_preparation.json"
    report_path.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False,
                                      default=str), encoding="utf-8")
    result.report_path = report_path
    return result
