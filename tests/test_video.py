import subprocess

import pytest
from PIL import Image

from app.config import QualityMode
from app.cover.compose import CoverComposer
from app.cover.presets import get_preset
from app.config import StylePreset
from app.images.base import ImageRequest
from app.images.providers.offline import OfflineProvider
from app.video.builder import build_tiktok_video, choose_scaling, crf_for, extract_frame
from app.video.ffmpeg import FFmpegError, probe
from app.video.validate import validate_video


@pytest.fixture()
def cover(settings, tmp_path):
    bg = OfflineProvider(settings).generate(
        ImageRequest(prompt="mystical night", width=1080, height=1920, seed=7)).data
    composed = CoverComposer(get_preset(StylePreset.CINEMATIC_MYSTICAL)).compose(
        bg, ["DEINE", "SEELE", "SPRICHT"])
    return composed.save(tmp_path / "cover.png")


def test_probe_reads_streams(settings, sample_video):
    info = probe(sample_video, settings)
    assert info.width == 1080 and info.height == 1920
    assert info.has_audio and info.duration > 2.5


def test_build_preserves_audio_and_duration(settings, sample_video, cover, tmp_path):
    src = probe(sample_video, settings)
    result = build_tiktok_video(sample_video, cover, tmp_path / "out.mp4", settings)
    assert result.output_path.is_file()
    out = result.info
    assert out.has_audio
    assert out.video_codec == "h264" and out.audio_codec == "aac"
    assert abs(out.duration - src.duration) < 0.35          # no artificial intro
    assert (out.width, out.height) == (1080, 1920)
    assert 0 < result.cover_timestamp_ms < out.duration * 1000
    # original untouched
    assert sample_video.is_file() and probe(sample_video, settings).duration == src.duration


def test_cover_frame_is_baked_into_the_video(settings, sample_video, cover, tmp_path):
    result = build_tiktok_video(sample_video, cover, tmp_path / "out.mp4", settings)
    frame = extract_frame(result.output_path, result.cover_timestamp_ms,
                          tmp_path / "frame.jpg", settings)
    with Image.open(frame) as f:
        assert f.size == (1080, 1920)
        frame_px = f.convert("RGB").resize((32, 57))
    with Image.open(cover) as c:
        cover_px = c.convert("RGB").resize((32, 57))
    diff = sum(abs(a - b) for pa, pb in zip(frame_px.getdata(), cover_px.getdata())
               for a, b in zip(pa, pb)) / (32 * 57 * 3)
    assert diff < 28, f"extracted frame does not match the cover (mean diff {diff:.1f})"


def test_landscape_source_is_converted_to_9_16(settings, landscape_video, cover, tmp_path):
    result = build_tiktok_video(landscape_video, cover, tmp_path / "ls.mp4", settings)
    assert (result.info.width, result.info.height) == (1080, 1920)
    assert result.scaling_mode == "pad_blur"
    check = validate_video(result.output_path, settings, source_had_audio=False)
    assert check.ok, check.errors


def test_validation_detects_missing_audio(settings, landscape_video):
    check = validate_video(landscape_video, settings, source_had_audio=True, deep=False)
    assert not check.ok
    assert any("audio" in e for e in check.errors)


def test_validation_rejects_garbage_file(settings, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 5000)
    assert not validate_video(bad, settings).ok


def test_missing_source_raises(settings, cover, tmp_path):
    with pytest.raises(FFmpegError):
        build_tiktok_video(tmp_path / "nope.mp4", cover, tmp_path / "x.mp4", settings)


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


def test_quality_mode_affects_crf():
    assert crf_for(QualityMode.HIGH_QUALITY, 20) < crf_for(QualityMode.BALANCED, 20) \
        < crf_for(QualityMode.FAST, 20)
