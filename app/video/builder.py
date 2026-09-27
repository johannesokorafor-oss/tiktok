"""Build the TikTok-ready video with the generated cover as an early frame.

Strategy (documented TikTok behaviour): the Content Posting API selects the
post cover from a frame of the uploaded video via ``video_cover_timestamp_ms``.
A standalone PNG is NOT accepted by the video endpoints, so the generated
cover is baked into the beginning of the processed video as a very short
overlay (default 120 ms) and ``video_cover_timestamp_ms`` points at the middle
of that overlay.

* the source file is never touched - a processed copy is produced,
* audio is preserved and never shifted (the overlay does not add duration),
* 9:16 sources are preserved; other aspect ratios get a safe cinematic crop
  or a blurred-pad conversion when cropping would destroy content.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from app.config import QualityMode, Settings
from app.video.ffmpeg import FFmpegError, MediaInfo, find_ffmpeg, probe, run

log = logging.getLogger(__name__)

#: how far the source aspect may differ from 9:16 before we pad instead of crop
CROP_TOLERANCE = 0.34


@dataclass
class BuildResult:
    output_path: Path
    cover_timestamp_ms: int
    info: MediaInfo
    scaling_mode: str
    command: List[str]

    def as_dict(self) -> dict:
        return {"output_path": str(self.output_path),
                "cover_timestamp_ms": self.cover_timestamp_ms,
                "scaling_mode": self.scaling_mode,
                "info": self.info.as_dict()}


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
    # blurred pad: keep the whole frame, fill the rest with a blurred cover of itself
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


def build_tiktok_video(source: Path, cover: Path, output: Path, settings: Settings,
                       *, hold_ms: Optional[int] = None) -> BuildResult:
    if not source.is_file():
        raise FFmpegError(f"source video not found: {source}")
    if not cover.is_file():
        raise FFmpegError(f"cover image not found: {cover}")

    info = probe(source, settings)
    if info.duration <= 0.2:
        raise FFmpegError(f"source video is too short ({info.duration:.2f}s)")

    tw, th = settings.target_width, settings.target_height
    mode = choose_scaling(info, tw, th)
    if mode == "native":
        tw, th = (info.width, info.height) if (info.width, info.height) == (tw, th) else (tw, th)

    hold = max(40, hold_ms if hold_ms is not None else settings.cover_frame_hold_ms)
    hold = int(min(hold, max(80, info.duration * 1000 * 0.25)))
    hold_s = hold / 1000.0
    fps = info.fps if 1 <= info.fps <= 120 else 30.0

    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(settings)

    scale_chain = _scale_filter(mode, tw, th)
    filter_complex = (
        f"[0:v]{scale_chain}[base];"
        f"[1:v]scale={tw}:{th}:flags=lanczos,setsar=1,format=rgba[cov];"
        f"[base][cov]overlay=0:0:enable='lt(t,{hold_s:.3f})':eof_action=pass,"
        f"format=yuv420p[v]"
    )

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-nostdin",
        "-i", str(source),
        "-loop", "1", "-framerate", f"{fps:.4f}", "-t", f"{hold_s:.3f}", "-i", str(cover),
        "-filter_complex", filter_complex,
        "-map", "[v]",
    ]
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
        "-force_key_frames", "0",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
        str(output),
    ]
    log.info("building tiktok video (%s) -> %s", mode, output.name)
    run(cmd, timeout=7200)

    out_info = probe(output, settings)
    cover_ts = max(0, min(int(hold / 2), int(out_info.duration * 1000) - 1))
    return BuildResult(output_path=output, cover_timestamp_ms=cover_ts, info=out_info,
                       scaling_mode=mode, command=cmd)


def extract_frame(video: Path, timestamp_ms: int, dest: Path, settings: Settings) -> Path:
    """Extract a frame (used to verify the baked-in cover really is there)."""
    ffmpeg = find_ffmpeg(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg, "-y", "-hide_banner", "-nostdin", "-ss", f"{timestamp_ms / 1000:.3f}",
         "-i", str(video), "-frames:v", "1", "-q:v", "2", str(dest)], timeout=300)
    return dest
