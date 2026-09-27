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


# ---------------------------------------------------------------- cover embedding quality
def test_cover_embedding_does_not_disturb_audio_or_duration(settings, sample_video, cover, tmp_path):
    """The cover overlay must not add duration, shift audio or corrupt frames."""
    src = probe(sample_video, settings)
    result = build_tiktok_video(sample_video, cover, tmp_path / "quality.mp4", settings)
    out = result.info

    # duration is preserved (the overlay replaces pixels, it does not insert frames)
    assert abs(out.duration - src.duration) < 0.15
    # audio stream is intact and the same length as the video
    assert out.has_audio and out.audio_codec == "aac"
    ffprobe = __import__("app.video.ffmpeg", fromlist=["find_ffprobe"]).find_ffprobe(settings)
    audio_dur = float(subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=duration", "-of", "csv=p=0", str(result.output_path)],
        capture_output=True, text=True, check=True).stdout.strip() or 0)
    assert abs(audio_dur - out.duration) < 0.25


def test_first_frames_are_clean_and_hold_is_short(settings, sample_video, cover, tmp_path):
    result = build_tiktok_video(sample_video, cover, tmp_path / "frames.mp4", settings)
    hold_ms = settings.cover_frame_hold_ms

    # frame at the cover timestamp == the cover
    first = extract_frame(result.output_path, result.cover_timestamp_ms,
                          tmp_path / "f0.png", settings)
    with Image.open(first) as f:
        first_px = f.convert("RGB").resize((24, 42))
    with Image.open(cover) as c:
        cover_px = c.convert("RGB").resize((24, 42))
    diff_cover = sum(abs(a - b) for pa, pb in zip(first_px.getdata(), cover_px.getdata())
                     for a, b in zip(pa, pb)) / (24 * 42 * 3)
    assert diff_cover < 28

    # shortly after the hold the original content is back (no long frozen intro)
    later = extract_frame(result.output_path, hold_ms + 400, tmp_path / "f1.png", settings)
    with Image.open(later) as f:
        later_px = f.convert("RGB").resize((24, 42))
    diff_later = sum(abs(a - b) for pa, pb in zip(later_px.getdata(), cover_px.getdata())
                     for a, b in zip(pa, pb)) / (24 * 42 * 3)
    assert diff_later > diff_cover
    assert hold_ms <= 400                      # keep it professional/unobtrusive


def test_cover_timestamp_points_inside_the_hold(settings, sample_video, cover, tmp_path):
    result = build_tiktok_video(sample_video, cover, tmp_path / "ts.mp4", settings)
    assert 0 < result.cover_timestamp_ms <= settings.cover_frame_hold_ms
    assert result.cover_timestamp_ms < result.info.duration * 1000
