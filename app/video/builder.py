"""Prepare the creator's video for the TikTok Content Posting API.

Two hard rules:

* **The source file is never modified.** A processed copy is produced in the
  job's working directory.
* **Nothing is added to the video.** No generated cover, no synthetic intro
  frame, no overlay, no `video_cover_timestamp_ms`. The creator picks the
  cover manually inside TikTok. The uploaded file contains exactly the
  creator's content.

The video is only touched when that is technically necessary for TikTok
compatibility:

============  ==========================================================
passthrough   already compatible -> byte-identical copy, nothing re-encoded
remux         compatible streams in a different/unoptimised container ->
              stream copy into MP4 with +faststart (no quality loss, no
              re-encode, duration unchanged)
transcode     incompatible codec / pixel format / aspect ratio ->
              H.264 High + AAC re-encode
============  ==========================================================
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.config import QualityMode, Settings
from app.video.ffmpeg import FFmpegError, MediaInfo, find_ffmpeg, probe, run

log = logging.getLogger(__name__)

#: how far the source aspect may differ from 9:16 before we pad instead of crop
CROP_TOLERANCE = 0.34
#: codecs TikTok accepts without re-encoding
OK_VIDEO_CODECS = {"h264", "hevc"}
OK_AUDIO_CODECS = {"aac"}
OK_CONTAINERS = ("mov,mp4,m4a,3gp,3g2,mj2",)


@dataclass
class BuildResult:
    output_path: Path
    info: MediaInfo
    #: "passthrough" | "remux" | "transcode"
    processing: str
    #: "native" | "crop" | "pad_blur" - only meaningful when transcoding
    scaling_mode: str = "native"
    reasons: List[str] = field(default_factory=list)
    command: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"output_path": str(self.output_path), "processing": self.processing,
                "scaling_mode": self.scaling_mode, "reasons": self.reasons,
                "cover_embedded": False, "info": self.info.as_dict()}


def choose_scaling(info: MediaInfo, tw: int, th: int) -> str:
    target = tw / th
    src = info.aspect or target
    if abs(src - target) < 0.02:
        return "native"
    if abs(src - target) / target <= CROP_TOLERANCE:
        return "crop"
    return "pad_blur"


def _scale_filter(mode: str, tw: int, th: int) -> str:
    if mode == "native":
        return f"scale={tw}:{th}:flags=lanczos,setsar=1"
    if mode == "crop":
        return (f"scale={tw}:{th}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={tw}:{th},setsar=1")
    return (f"split=2[bg][fg];"
            f"[bg]scale={tw}:{th}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={tw}:{th},boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.12[bgb];"
            f"[fg]scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1")


def crf_for(quality: QualityMode, configured: int) -> int:
    return {QualityMode.FAST: min(28, configured + 4),
            QualityMode.BALANCED: configured,
            QualityMode.HIGH_QUALITY: max(16, configured - 3)}[quality]


def preset_for(quality: QualityMode, configured: str) -> str:
    return {QualityMode.FAST: "veryfast",
            QualityMode.BALANCED: configured,
            QualityMode.HIGH_QUALITY: "slow"}[quality]


def plan_processing(info: MediaInfo, settings: Settings) -> tuple[str, List[str]]:
    """Decide the least invasive processing that makes the file uploadable."""
    policy = (settings.video_normalization or "auto").strip().lower()
    reasons: List[str] = []

    if policy == "always":
        return "transcode", ["VIDEO_NORMALIZATION=always"]

    if info.video_codec.lower() not in OK_VIDEO_CODECS:
        reasons.append(f"video codec '{info.video_codec}' is not H.264/HEVC")
    if info.has_audio and (info.audio_codec or "").lower() not in OK_AUDIO_CODECS:
        reasons.append(f"audio codec '{info.audio_codec}' is not AAC")
    if settings.enforce_vertical:
        scaling = choose_scaling(info, settings.target_width, settings.target_height)
        if scaling != "native":
            reasons.append(
                f"source is {info.width}x{info.height}; TikTok target is "
                f"{settings.target_width}x{settings.target_height} ({scaling})")

    if reasons:
        if policy == "never":
            reasons.append("VIDEO_NORMALIZATION=never - uploading without re-encoding")
            return ("remux" if info.container not in OK_CONTAINERS else "passthrough"), reasons
        return "transcode", reasons

    if info.container not in OK_CONTAINERS or info.path.suffix.lower() not in (".mp4", ".mov"):
        return "remux", [f"container '{info.container}' remuxed to MP4 (stream copy)"]
    return "passthrough", ["already TikTok compatible - uploaded unchanged"]


def build_tiktok_video(source: Path, output: Path, settings: Settings) -> BuildResult:
    """Produce the file that will be uploaded. Never alters `source`."""
    if not source.is_file():
        raise FFmpegError(f"source video not found: {source}")

    info = probe(source, settings)
    if info.duration <= 0.2:
        raise FFmpegError(f"source video is too short ({info.duration:.2f}s)")

    processing, reasons = plan_processing(info, settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(settings)
    cmd: List[str] = []
    scaling = "native"

    if processing == "passthrough":
        # the creator's file is already uploadable - copy it verbatim
        log.info("video is already TikTok compatible; copying unchanged -> %s", output.name)
        shutil.copy2(source, output)

    elif processing == "remux":
        log.info("remuxing streams into MP4 (no re-encode) -> %s", output.name)
        cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-i", str(source),
               "-map", "0:v:0", "-c", "copy"]
        if info.has_audio:
            cmd += ["-map", "0:a:0"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(output)]
        run(cmd, timeout=3600)

    else:
        scaling = (choose_scaling(info, settings.target_width, settings.target_height)
                   if settings.enforce_vertical else "native")
        tw, th = ((settings.target_width, settings.target_height)
                  if settings.enforce_vertical else (info.width, info.height))
        fps = info.fps if 1 <= info.fps <= 120 else 30.0
        log.info("transcoding (%s, %s) -> %s", scaling, "; ".join(reasons), output.name)
        cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-i", str(source),
               "-vf", _scale_filter(scaling, tw, th) if settings.enforce_vertical
               else "format=yuv420p",
               "-map", "0:v:0"]
        if info.has_audio:
            cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", settings.audio_bitrate, "-ar", "48000"]
        else:
            cmd += ["-an"]
        cmd += [
            "-c:v", "libx264",
            "-preset", preset_for(settings.quality_mode, settings.video_preset),
            "-crf", str(crf_for(settings.quality_mode, settings.video_crf)),
            "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-g", str(int(max(2, round(fps * 2)))),
            "-movflags", "+faststart",
            "-max_muxing_queue_size", "1024",
            str(output),
        ]
        run(cmd, timeout=7200)

    out_info = probe(output, settings)
    return BuildResult(output_path=output, info=out_info, processing=processing,
                       scaling_mode=scaling, reasons=reasons, command=cmd)


def extract_frame(video: Path, timestamp_ms: int, dest: Path, settings: Settings) -> Path:
    """Extract a single frame (used by tests/inspection, never for covers)."""
    ffmpeg = find_ffmpeg(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg, "-y", "-hide_banner", "-nostdin", "-ss", f"{timestamp_ms / 1000:.3f}",
         "-i", str(video), "-frames:v", "1", "-q:v", "2", str(dest)], timeout=300)
    return dest
