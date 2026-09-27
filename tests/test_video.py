"""Video preparation tests.

The application must never add anything to the creator's video: no generated
cover, no synthetic intro frame, no overlay. It may only re-encode when that
is technically required for TikTok compatibility.
"""
import subprocess

import pytest
from PIL import Image

from app.config import QualityMode
from app.video.builder import (build_tiktok_video, choose_scaling, crf_for, extract_frame,
                               plan_processing, preset_for)
from app.video.ffmpeg import FFmpegError, find_ffprobe, probe
from app.video.validate import validate_video


def _frame(path, ms, dest, settings):
    extract_frame(path, ms, dest, settings)
    with Image.open(dest) as img:
        return img.convert("RGB").resize((32, 57))


def _mean_diff(a, b):
    return sum(abs(x - y) for pa, pb in zip(a.getdata(), b.getdata())
               for x, y in zip(pa, pb)) / (32 * 57 * 3)


# ---------------------------------------------------------------- probing
def test_probe_reads_streams(settings, sample_video):
    info = probe(sample_video, settings)
    assert info.width == 1080 and info.height == 1920
    assert info.has_audio and info.duration > 2.5


# ---------------------------------------------------------------- processing policy
def test_compatible_vertical_source_is_passed_through(settings, sample_video, tmp_path):
    """An already-compatible file is uploaded unchanged - no re-encode at all."""
    result = build_tiktok_video(sample_video, tmp_path / "out.mp4", settings)
    assert result.processing == "passthrough"
    assert result.output_path.read_bytes() == sample_video.read_bytes()
    assert result.command == []


def test_landscape_source_is_transcoded_to_9_16(settings, landscape_video, tmp_path):
    result = build_tiktok_video(landscape_video, tmp_path / "ls.mp4", settings)
    assert result.processing == "transcode"
    assert result.scaling_mode == "pad_blur"
    assert (result.info.width, result.info.height) == (1080, 1920)
    assert validate_video(result.output_path, settings, source_had_audio=False).ok
    # original untouched
    assert probe(landscape_video, settings).width == 1920


def test_enforce_vertical_can_be_disabled(settings, landscape_video, tmp_path):
    settings.enforce_vertical = False
    info = probe(landscape_video, settings)
    processing, reasons = plan_processing(info, settings)
    assert processing == "passthrough"
    assert not any("1080x1920" in r for r in reasons)


def test_normalization_policies(settings, sample_video):
    info = probe(sample_video, settings)
    settings.video_normalization = "always"
    assert plan_processing(info, settings)[0] == "transcode"
    settings.video_normalization = "auto"
    assert plan_processing(info, settings)[0] == "passthrough"


def test_incompatible_codec_triggers_transcode(settings, sample_video, tmp_path, ffmpeg_exe):
    """A WebM/VP9 source must be converted to H.264 before upload."""
    webm = tmp_path / "src.webm"
    subprocess.run([ffmpeg_exe, "-y", "-v", "error", "-i", str(sample_video),
                    "-c:v", "libvpx-vp9", "-b:v", "300k", "-deadline", "realtime",
                    "-cpu-used", "8", "-c:a", "libopus", "-t", "1", str(webm)],
                   check=True, capture_output=True)
    info = probe(webm, settings)
    processing, reasons = plan_processing(info, settings)
    assert processing == "transcode"
    assert any("codec" in r for r in reasons)
    result = build_tiktok_video(webm, tmp_path / "out.mp4", settings)
    assert result.info.video_codec == "h264" and result.info.audio_codec == "aac"


# ---------------------------------------------------------------- video integrity
def test_no_cover_frame_is_inserted(settings, sample_video, tmp_path):
    """Regression guard: the first frames are the creator's own content."""
    result = build_tiktok_video(sample_video, tmp_path / "out.mp4", settings)

    src_first = _frame(sample_video, 0, tmp_path / "s0.png", settings)
    out_first = _frame(result.output_path, 0, tmp_path / "o0.png", settings)
    assert _mean_diff(src_first, out_first) < 6          # identical opening frame

    # nothing frozen at the start: the output changes over time exactly like the
    # source does (a synthetic cover hold would freeze the first frames)
    src_later = _frame(sample_video, 1500, tmp_path / "s1500.png", settings)
    out_later = _frame(result.output_path, 1500, tmp_path / "o1500.png", settings)
    assert _mean_diff(out_first, out_later) == pytest.approx(
        _mean_diff(src_first, src_later), abs=1.0)
    assert _mean_diff(src_later, out_later) < 6

    # and the 120 ms mark (the old synthetic cover hold) is normal content too
    src_120 = _frame(sample_video, 120, tmp_path / "s120.png", settings)
    out_120 = _frame(result.output_path, 120, tmp_path / "o120.png", settings)
    assert _mean_diff(src_120, out_120) < 6


def test_duration_and_audio_are_unchanged_without_transcoding(settings, sample_video, tmp_path):
    src = probe(sample_video, settings)
    result = build_tiktok_video(sample_video, tmp_path / "out.mp4", settings)
    out = result.info
    assert out.duration == pytest.approx(src.duration, abs=0.02)   # no 120 ms intro
    assert out.has_audio and out.audio_codec == src.audio_codec

    ffprobe = find_ffprobe(settings)
    def audio_start(path):
        return float(subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=start_time", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True).stdout.strip() or 0)
    assert audio_start(result.output_path) == pytest.approx(audio_start(sample_video), abs=0.02)


def test_duration_only_changes_within_transcoding_tolerance(settings, landscape_video, tmp_path):
    src = probe(landscape_video, settings)
    result = build_tiktok_video(landscape_video, tmp_path / "ls.mp4", settings)
    assert result.processing == "transcode"
    assert result.info.duration == pytest.approx(src.duration, abs=0.2)


def test_build_result_reports_no_cover(settings, sample_video, tmp_path):
    result = build_tiktok_video(sample_video, tmp_path / "out.mp4", settings)
    data = result.as_dict()
    assert data["cover_embedded"] is False
    assert "cover_timestamp_ms" not in data
    assert not hasattr(result, "cover_timestamp_ms")


# ---------------------------------------------------------------- validation
def test_validation_detects_missing_audio(settings, landscape_video):
    check = validate_video(landscape_video, settings, source_had_audio=True, deep=False)
    assert not check.ok
    assert any("audio" in e for e in check.errors)


def test_validation_rejects_garbage_file(settings, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 5000)
    assert not validate_video(bad, settings).ok


def test_missing_source_raises(settings, tmp_path):
    with pytest.raises(FFmpegError):
        build_tiktok_video(tmp_path / "nope.mp4", tmp_path / "x.mp4", settings)


def test_scaling_decision():
    class I:
        def __init__(self, w, h):
            self.width, self.height = w, h

        @property
        def aspect(self):
            return self.width / self.height
    assert choose_scaling(I(1080, 1920), 1080, 1920) == "native"
    assert choose_scaling(I(1000, 1900), 1080, 1920) == "crop"
    assert choose_scaling(I(1920, 1080), 1080, 1920) == "pad_blur"


def test_quality_mode_affects_encoding():
    assert crf_for(QualityMode.HIGH_QUALITY, 20) < crf_for(QualityMode.BALANCED, 20) \
        < crf_for(QualityMode.FAST, 20)
    assert preset_for(QualityMode.FAST, "medium") == "veryfast"
