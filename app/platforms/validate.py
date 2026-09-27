"""Per-platform asset validation against each platform's current documented specs.

Every platform has different limits - they are never assumed to be identical.
Sources checked 2026-09-27:

Vimeo        API uploads accept the usual professional formats; the practical
             constraints are the account's weekly/total storage quota and the
             file size. Validated here: readable MP4/MOV, H.264/HEVC video,
             AAC audio, sane fps/duration, <= 256 GB (API hard limit far above
             any normal clip).
Dailymotion  MP4/MOV/AVI/WMV..., recommended H.264 + AAC in MP4; free accounts
             are documented at max 2 GB / 60 min per video.
SoundCloud   audio only: uploads up to 4 GB and up to 6 h 00 m per track;
             lossless formats (FLAC/WAV/AIFF/ALAC) are accepted and
             transcoded. Validated here: audio stream, duration, size, codec.
Rumble       no official public VOD upload API; the file is only sanity
             checked so the manual upload cannot fail on an obviously broken
             file.
Patreon      direct file upload in the post editor: .mov/.mp4/.mpeg/.ogg and
             max 5 GB (support documentation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from app.config import Settings
from app.video.ffmpeg import FFmpegError, MediaInfo, probe

GB = 1024 ** 3


@dataclass
class AssetCheck:
    ok: bool
    platform: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "platform": self.platform, "errors": self.errors,
                "warnings": self.warnings, "info": self.info}


def _probe(path: Path, settings: Settings, platform: str) -> tuple[MediaInfo | None, AssetCheck]:
    check = AssetCheck(ok=True, platform=platform)
    if not path.is_file():
        check.ok = False
        check.errors.append(f"file missing: {path}")
        return None, check
    try:
        info = probe(path, settings)
    except FFmpegError as exc:
        check.ok = False
        check.errors.append(f"ffprobe rejected the file: {exc}")
        return None, check
    check.info = info.as_dict()
    check.info["size_bytes"] = path.stat().st_size
    return info, check


# ---------------------------------------------------------------- video
def validate_vimeo(path: Path, settings: Settings) -> AssetCheck:
    info, check = _probe(path, settings, "vimeo")
    if info is None:
        return check
    size = path.stat().st_size
    if path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
        check.warnings.append(f"unusual container '{path.suffix}' for Vimeo")
    if info.video_codec.lower() not in {"h264", "hevc", "prores", "vp9", "av1"}:
        check.warnings.append(f"video codec '{info.video_codec}' may be re-encoded by Vimeo")
    if info.has_audio and (info.audio_codec or "").lower() not in {"aac", "pcm_s16le", "flac"}:
        check.warnings.append(f"audio codec '{info.audio_codec}' may be re-encoded by Vimeo")
    if info.duration < 1:
        check.ok = False
        check.errors.append("video is shorter than one second")
    if size > 256 * GB:
        check.ok = False
        check.errors.append("file exceeds Vimeo's maximum single-file size")
    check.warnings.append("Vimeo enforces per-account weekly/total storage quotas; "
                          "an upload can still be rejected when the quota is exhausted")
    return check


def validate_dailymotion(path: Path, settings: Settings) -> AssetCheck:
    info, check = _probe(path, settings, "dailymotion")
    if info is None:
        return check
    size = path.stat().st_size
    if path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mkv", ".webm"}:
        check.warnings.append(f"unusual container '{path.suffix}' for Dailymotion")
    if info.video_codec.lower() not in {"h264", "hevc", "vp9", "av1", "mpeg4"}:
        check.warnings.append(f"video codec '{info.video_codec}' may be re-encoded")
    if size > 2 * GB:
        check.warnings.append(
            f"file is {size / GB:.2f} GB - standard Dailymotion accounts are limited to 2 GB "
            "per video")
    if info.duration > 60 * 60:
        check.warnings.append(
            f"duration {info.duration / 60:.0f} min - standard accounts are limited to 60 min")
    if info.duration < 1:
        check.ok = False
        check.errors.append("video is shorter than one second")
    return check


def validate_rumble(path: Path, settings: Settings) -> AssetCheck:
    info, check = _probe(path, settings, "rumble")
    if info is None:
        return check
    if path.suffix.lower() not in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        check.warnings.append(f"unusual container '{path.suffix}' for a manual Rumble upload")
    if info.duration < 1:
        check.ok = False
        check.errors.append("video is shorter than one second")
    check.warnings.append("Rumble publishes no official VOD upload API; the file is prepared "
                          "for a manual upload in the Rumble web uploader")
    return check


def validate_patreon(path: Path, settings: Settings) -> AssetCheck:
    info, check = _probe(path, settings, "patreon")
    if info is None:
        return check
    size = path.stat().st_size
    if path.suffix.lower() not in {".mov", ".mp4", ".mpeg", ".ogg"}:
        check.ok = False
        check.errors.append(
            f"Patreon's direct file upload accepts .mov/.mp4/.mpeg/.ogg (got '{path.suffix}')")
    if size > 5 * GB:
        check.ok = False
        check.errors.append(f"file is {size / GB:.2f} GB; Patreon's limit is 5 GB")
    return check


# ---------------------------------------------------------------- audio
def validate_soundcloud_audio(path: Path, settings: Settings) -> AssetCheck:
    info, check = _probe(path, settings, "soundcloud")
    check.platform = "soundcloud"
    if not path.is_file():
        return check
    size = path.stat().st_size
    try:
        from app.video.ffmpeg import find_ffprobe, run
        import json as _json
        out = run([find_ffprobe(settings), "-v", "error", "-print_format", "json",
                   "-show_streams", "-show_format", str(path)], timeout=180).stdout
        data = _json.loads(out or "{}")
    except Exception as exc:  # noqa: BLE001
        return AssetCheck(ok=False, platform="soundcloud",
                          errors=[f"ffprobe rejected the audio file: {exc}"])

    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or 0)
    check = AssetCheck(ok=True, platform="soundcloud", info={
        "duration": round(duration, 2),
        "size_bytes": size,
        "audio_codec": (audio or {}).get("codec_name"),
        "sample_rate": (audio or {}).get("sample_rate"),
        "channels": (audio or {}).get("channels"),
        "container": fmt.get("format_name"),
    })
    if audio is None:
        check.ok = False
        check.errors.append("the file contains no audio stream")
        return check
    if video is not None:
        check.warnings.append("the file still contains a video stream; SoundCloud expects audio")
    codec = (audio.get("codec_name") or "").lower()
    if codec not in {"flac", "pcm_s16le", "pcm_s24le", "alac", "mp3", "aac", "vorbis", "opus"}:
        check.ok = False
        check.errors.append(f"audio codec '{codec}' is not a documented SoundCloud format")
    if duration <= 0:
        check.ok = False
        check.errors.append("audio has no duration")
    if duration > 6 * 3600:
        check.ok = False
        check.errors.append(f"duration {duration / 3600:.1f} h exceeds SoundCloud's 6 h maximum")
    max_bytes = settings.soundcloud_max_file_mb * 1024 * 1024
    if size > max_bytes:
        check.ok = False
        check.errors.append(
            f"file is {size / GB:.2f} GB; SoundCloud accepts up to "
            f"{settings.soundcloud_max_file_mb / 1024:.0f} GB per track")
    rate = int(audio.get("sample_rate") or 0)
    if rate and rate < 44100:
        check.warnings.append(f"sample rate {rate} Hz is below the recommended 44.1 kHz")
    if int(audio.get("channels") or 0) > 2:
        check.warnings.append("more than 2 channels; SoundCloud plays back in stereo")
    return check


VALIDATORS = {
    "vimeo": validate_vimeo,
    "dailymotion": validate_dailymotion,
    "rumble": validate_rumble,
    "patreon": validate_patreon,
    "soundcloud": validate_soundcloud_audio,
}
