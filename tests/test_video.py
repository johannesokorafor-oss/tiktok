from pathlib import Path

import pytest

from tta.providers.base import ImageRequest
from tta.providers.local_art import LocalArtProvider
from tta.video import (
    compute_cover_timestamp,
    insert_cover,
    normalize_video,
    plan_layout,
    probe,
    validate_final,
)


def test_plan_layout_decisions():
    assert plan_layout(1080, 1920) == "scale"
    assert plan_layout(540, 960) == "scale"
    assert plan_layout(1088, 1920) == "scale"     # <2% mismatch: just scale
    assert plan_layout(1080, 2000) == "crop"      # slightly too tall
    assert plan_layout(1080, 2160) == "crop"      # 9:18 -> mild top/bottom crop
    assert plan_layout(1920, 1080) == "pad"       # landscape
    assert plan_layout(1000, 1000) == "pad"       # square
    assert plan_layout(0, 0) == "pad"


def test_probe_fixture(fixture_video):
    info = probe(fixture_video)
    assert info.width == 640 and info.height == 360
    assert info.video_codec == "h264"
    assert info.has_audio
    assert 3.5 < info.duration < 4.6


def test_normalize_landscape_pads_to_vertical(tmp_path, fixture_video):
    out = tmp_path / "norm.mp4"
    info = normalize_video(fixture_video, out)
    assert (info.width, info.height) == (1080, 1920)
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert info.pix_fmt == "yuv420p"


def test_normalize_injects_silent_audio(tmp_path, fixture_video_noaudio):
    out = tmp_path / "norm.mp4"
    info = normalize_video(fixture_video_noaudio, out)
    assert info.has_audio and info.audio_codec == "aac"


def test_normalize_preserves_original(tmp_path, fixture_video):
    before = Path(fixture_video).read_bytes()
    normalize_video(fixture_video, tmp_path / "n.mp4")
    assert Path(fixture_video).read_bytes() == before


def test_insert_cover_extends_duration_and_timestamp(tmp_path, fixture_video):
    norm = tmp_path / "norm.mp4"
    base_info = normalize_video(fixture_video, norm)

    cover = tmp_path / "cover.png"
    LocalArtProvider().generate(ImageRequest(prompt="c", seed=3), cover)

    final = tmp_path / "final.mp4"
    final_info, ts = insert_cover(norm, cover, final, cover_duration_ms=500)
    assert final.exists()
    assert (final_info.width, final_info.height) == (1080, 1920)
    # duration grew by roughly the cover segment
    assert final_info.duration == pytest.approx(base_info.duration + 0.5, abs=0.25)
    assert 0 <= ts < final_info.duration * 1000
    assert ts == 250


def test_compute_cover_timestamp_clamped():
    assert compute_cover_timestamp(500, 10.0) == 250
    assert compute_cover_timestamp(500, 0.1) == 99   # clamped below duration
    assert compute_cover_timestamp(0, 10.0) == 0


def test_validate_final_ok(tmp_path, fixture_video):
    norm = tmp_path / "norm.mp4"
    normalize_video(fixture_video, norm)
    cover = tmp_path / "cover.png"
    LocalArtProvider().generate(ImageRequest(prompt="c", seed=4), cover)
    final = tmp_path / "final.mp4"
    _, ts = insert_cover(norm, cover, final, cover_duration_ms=400)
    report = validate_final(final, cover_timestamp_ms=ts)
    assert report.ok, report.issues


def test_validate_rejects_missing_file(tmp_path):
    report = validate_final(tmp_path / "nope.mp4")
    assert not report.ok


def test_validate_rejects_wrong_resolution(fixture_video):
    report = validate_final(fixture_video)
    assert not report.ok
    assert any("resolution" in issue for issue in report.issues)


def test_validate_rejects_bad_timestamp(tmp_path, fixture_video):
    norm = tmp_path / "norm.mp4"
    normalize_video(fixture_video, norm)
    report = validate_final(norm, cover_timestamp_ms=99_999_999)
    assert not report.ok
    assert any("timestamp" in issue for issue in report.issues)
