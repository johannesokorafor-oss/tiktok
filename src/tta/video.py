"""FFmpeg-based video processing.

* probe media with ffprobe (JSON)
* normalize any input to a TikTok-ready 1080x1920 H.264/AAC MP4
  (intelligent crop for near-vertical sources, blurred-pad for wide ones)
* embed the generated cover as real leading video frames and compute the
  matching ``video_cover_timestamp_ms``
* validate the final file

The original input file is never modified - all outputs are new files.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("tta.video")

TARGET_W = 1080
TARGET_H = 1920
TARGET_FPS = 30
AUDIO_RATE = 44100


class VideoError(RuntimeError):
    pass


def find_ffmpeg(configured: str = "") -> str:
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # optional pip-installed binary
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise VideoError("ffmpeg not found (set FFMPEG_PATH or install FFmpeg)")


def find_ffprobe(configured: str = "") -> str:
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("ffprobe")
    if found:
        return found
    raise VideoError("ffprobe not found (set FFPROBE_PATH or install FFmpeg)")


def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
        raise VideoError(f"command failed ({cmd[0]}, rc={proc.returncode}): {stderr}")
    return proc


# ----------------------------------------------------------------------
@dataclass
class ProbeResult:
    width: int = 0
    height: int = 0
    duration: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    pix_fmt: str = ""
    fps: float = 0.0
    has_audio: bool = False
    size_bytes: int = 0
    raw: dict = field(default_factory=dict)


def probe(path: str | Path, ffprobe: str = "") -> ProbeResult:
    path = Path(path)
    if not path.is_file():
        raise VideoError(f"file not found: {path}")
    exe = find_ffprobe(ffprobe)
    proc = _run([
        exe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], timeout=120)
    data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    result = ProbeResult(raw=data, size_bytes=path.stat().st_size)
    fmt = data.get("format", {})
    result.duration = float(fmt.get("duration") or 0.0)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not result.video_codec:
            result.video_codec = stream.get("codec_name", "")
            result.width = int(stream.get("width") or 0)
            result.height = int(stream.get("height") or 0)
            result.pix_fmt = stream.get("pix_fmt", "")
            rate = stream.get("avg_frame_rate") or "0/1"
            try:
                num, den = rate.split("/")
                result.fps = float(num) / float(den) if float(den) else 0.0
            except (ValueError, ZeroDivisionError):
                result.fps = 0.0
            if not result.duration:
                result.duration = float(stream.get("duration") or 0.0)
        elif stream.get("codec_type") == "audio" and not result.audio_codec:
            result.audio_codec = stream.get("codec_name", "")
            result.has_audio = True
    return result


# ----------------------------------------------------------------------
def plan_layout(width: int, height: int) -> str:
    """Decide how to fit a source into 9:16: 'scale', 'crop' or 'pad'.

    * already ~9:16          -> scale
    * mild mismatch (<12% of area would be lost by cropping) -> center crop
    * otherwise (landscape/square) -> blurred background pad
    """
    if width <= 0 or height <= 0:
        return "pad"
    target = TARGET_W / TARGET_H
    aspect = width / height
    if abs(aspect - target) / target < 0.02:
        return "scale"
    if aspect < target:  # narrower than 9:16 -> crop top/bottom slightly
        kept = aspect / target
    else:                # wider than 9:16 -> crop sides
        kept = target / aspect
    return "crop" if kept >= 0.88 else "pad"


def _fit_filter(mode: str) -> str:
    if mode == "scale":
        return f"scale={TARGET_W}:{TARGET_H}"
    if mode == "crop":
        return (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H}"
        )
    # blurred pad: blurred cover-fill background + contained foreground
    return (
        f"split=2[bg][fg];"
        f"[bg]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_W}:{TARGET_H},boxblur=24:4[bgb];"
        f"[fg]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
    )


def normalize_video(source: str | Path, dest: str | Path,
                    ffmpeg: str = "", ffprobe: str = "") -> ProbeResult:
    """Re-encode `source` into a 1080x1920 H.264/AAC MP4 at `dest`.

    Guarantees an audio track (silence is injected when the source has
    none) so that later concatenation is uniform.
    """
    source, dest = Path(source), Path(dest)
    info = probe(source, ffprobe)
    if info.width <= 0 or info.height <= 0:
        raise VideoError(f"no video stream in {source.name}")
    mode = plan_layout(info.width, info.height)
    log.info("normalize %s (%dx%d) -> 1080x1920 via %s", source.name,
             info.width, info.height, mode)

    vf = f"{_fit_filter(mode)},fps={TARGET_FPS},format=yuv420p,setsar=1"
    exe = find_ffmpeg(ffmpeg)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [exe, "-y", "-i", str(source)]
    if not info.has_audio:
        cmd += ["-f", "lavfi", "-t", f"{max(info.duration, 0.1):.3f}",
                "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}"]
        maps = ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        maps = ["-map", "0:v:0", "-map", "0:a:0"]
    cmd += [
        "-filter_complex", f"[0:v]{vf}[v]",
        "-map", "[v]",
    ] + maps[2:] + [
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE), "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
        str(dest),
    ]
    _run(cmd)
    return probe(dest, ffprobe)


def insert_cover(normalized: str | Path, cover_png: str | Path, dest: str | Path,
                 cover_duration_ms: int = 500,
                 ffmpeg: str = "", ffprobe: str = "") -> tuple[ProbeResult, int]:
    """Prepend the cover image as real video frames (with silent audio).

    Returns (probe_of_final, video_cover_timestamp_ms).  The timestamp
    points at the middle of the cover segment so TikTok's frame picker
    lands safely inside it.
    """
    normalized, cover_png, dest = Path(normalized), Path(cover_png), Path(dest)
    duration_s = max(0.1, cover_duration_ms / 1000.0)
    exe = find_ffmpeg(ffmpeg)
    dest.parent.mkdir(parents=True, exist_ok=True)

    filter_complex = (
        f"[0:v]scale={TARGET_W}:{TARGET_H},fps={TARGET_FPS},format=yuv420p,setsar=1,"
        f"trim=duration={duration_s:.3f}[cv];"
        f"[1:a]atrim=duration={duration_s:.3f}[ca];"
        f"[cv][ca][2:v][2:a]concat=n=2:v=1:a=1[v][a]"
    )
    cmd = [
        exe, "-y",
        "-loop", "1", "-framerate", str(TARGET_FPS),
        "-t", f"{duration_s:.3f}", "-i", str(cover_png),
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}",
        "-i", str(normalized),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE), "-ac", "2",
        "-movflags", "+faststart",
        str(dest),
    ]
    _run(cmd)
    final_info = probe(dest, ffprobe)
    timestamp_ms = compute_cover_timestamp(cover_duration_ms, final_info.duration)
    return final_info, timestamp_ms


def compute_cover_timestamp(cover_duration_ms: int, final_duration_s: float) -> int:
    """Middle of the cover segment, clamped into the final video duration."""
    ts = cover_duration_ms // 2
    if final_duration_s > 0:
        upper = max(0, int(final_duration_s * 1000) - 1)
        ts = min(ts, upper)
    return max(0, ts)


# ----------------------------------------------------------------------
@dataclass
class ValidationReport:
    ok: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    probe: ProbeResult | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "warnings": self.warnings,
            "width": self.probe.width if self.probe else None,
            "height": self.probe.height if self.probe else None,
            "duration": self.probe.duration if self.probe else None,
            "video_codec": self.probe.video_codec if self.probe else None,
            "audio_codec": self.probe.audio_codec if self.probe else None,
        }


def validate_final(path: str | Path, cover_timestamp_ms: int | None = None,
                   ffprobe: str = "") -> ValidationReport:
    """ffprobe-based validation of the final TikTok MP4."""
    issues: list[str] = []
    warnings: list[str] = []
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return ValidationReport(ok=False, issues=[f"missing or empty file: {path}"])
    try:
        info = probe(path, ffprobe)
    except VideoError as exc:
        return ValidationReport(ok=False, issues=[f"ffprobe failed: {exc}"])

    if info.video_codec != "h264":
        issues.append(f"video codec is '{info.video_codec}', expected h264")
    if not info.has_audio or info.audio_codec != "aac":
        issues.append(f"audio codec is '{info.audio_codec or 'none'}', expected aac")
    if (info.width, info.height) != (TARGET_W, TARGET_H):
        issues.append(f"resolution {info.width}x{info.height}, expected {TARGET_W}x{TARGET_H}")
    if info.pix_fmt and info.pix_fmt != "yuv420p":
        warnings.append(f"pixel format {info.pix_fmt} (yuv420p preferred)")
    if info.duration <= 0:
        issues.append("duration is 0")
    elif info.duration < 3:
        warnings.append(f"very short video ({info.duration:.2f}s); TikTok minimum is ~3s")
    if info.duration > 600:
        warnings.append(f"video longer than 10 minutes ({info.duration:.0f}s)")
    if cover_timestamp_ms is not None:
        if cover_timestamp_ms < 0 or cover_timestamp_ms >= info.duration * 1000:
            issues.append(
                f"cover timestamp {cover_timestamp_ms}ms outside video duration "
                f"({info.duration * 1000:.0f}ms)"
            )
    return ValidationReport(ok=not issues, issues=issues, warnings=warnings, probe=info)
