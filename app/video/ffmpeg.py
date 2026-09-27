"""FFmpeg / ffprobe discovery and invocation helpers."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from app.config import Settings


class FFmpegError(RuntimeError):
    pass


def _bundled(tool: str = "ffmpeg") -> Optional[str]:
    """Static binaries shipped as Python wheels - a fallback on clean systems.

    ``ffmpeg-binaries`` ships both ffmpeg and ffprobe (Windows/Linux/macOS);
    ``imageio-ffmpeg`` only ships ffmpeg.
    """
    try:
        import ffmpeg as ffmpeg_binaries  # type: ignore
        path = (ffmpeg_binaries.FFPROBE_PATH if tool == "ffprobe"
                else ffmpeg_binaries.FFMPEG_PATH)
        if path and Path(path).exists():
            return str(path)
    except Exception:
        pass
    if tool == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def find_ffmpeg(settings: Optional[Settings] = None) -> str:
    if settings and settings.ffmpeg_path and Path(settings.ffmpeg_path).exists():
        return str(settings.ffmpeg_path)
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    bundled = _bundled("ffmpeg")
    if bundled:
        return bundled
    raise FFmpegError("ffmpeg not found. Install it and/or set FFMPEG_PATH in .env")


def find_ffprobe(settings: Optional[Settings] = None) -> str:
    if settings and settings.ffprobe_path and Path(settings.ffprobe_path).exists():
        return str(settings.ffprobe_path)
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    bundled = _bundled("ffprobe")
    if bundled:
        return bundled
    ff = find_ffmpeg(settings)
    cand = Path(ff).with_name("ffprobe" + (".exe" if ff.lower().endswith(".exe") else ""))
    if cand.exists():
        return str(cand)
    raise FFmpegError("ffprobe not found. Install FFmpeg (full build) or set FFPROBE_PATH in .env")


def run(cmd: List[str], timeout: float = 3600) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        raise FFmpegError(f"command failed ({proc.returncode}): {' '.join(cmd[:3])} ...\n"
                          + "\n".join(tail))
    return proc


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: Optional[str]
    has_audio: bool
    size_bytes: int
    container: str
    bitrate: Optional[int] = None
    raw: Optional[dict] = None

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    def as_dict(self) -> dict:
        return {"path": str(self.path), "duration": round(self.duration, 3),
                "width": self.width, "height": self.height, "fps": round(self.fps, 3),
                "video_codec": self.video_codec, "audio_codec": self.audio_codec,
                "has_audio": self.has_audio, "size_bytes": self.size_bytes,
                "container": self.container, "bitrate": self.bitrate}


def _parse_fps(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/")
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path, settings: Optional[Settings] = None) -> MediaInfo:
    exe = find_ffprobe(settings)
    cmd = [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = run(cmd, timeout=180)
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise FFmpegError(f"{path.name}: no video stream found")
    duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    return MediaInfo(
        path=path,
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"),
        video_codec=video.get("codec_name", ""),
        audio_codec=audio.get("codec_name") if audio else None,
        has_audio=audio is not None,
        size_bytes=int(fmt.get("size") or (path.stat().st_size if path.exists() else 0)),
        container=fmt.get("format_name", ""),
        bitrate=int(fmt["bit_rate"]) if fmt.get("bit_rate", "").isdigit() else None,
        raw=data,
    )
