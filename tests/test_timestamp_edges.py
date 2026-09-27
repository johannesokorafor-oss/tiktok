"""Cover-timestamp and video edge cases: very short clips, fractional
frame rates, missing audio, ms conversion."""

from pathlib import Path

import pytest

from tta.providers.base import ImageRequest
from tta.providers.local_art import LocalArtProvider
from tta.video import (
    compute_cover_timestamp,
    insert_cover,
    normalize_video,
    probe,
    validate_final,
)
from tests.conftest import _make_video


@pytest.fixture(scope="module")
def cover(tmp_path_factory):
    path = tmp_path_factory.mktemp("cov") / "cover.png"
    LocalArtProvider().generate(ImageRequest(prompt="edge", seed=42), path)
    return path


def _full_build(src: Path, tmp_path: Path, cover: Path, cover_ms=500):
    norm = tmp_path / "norm.mp4"
    normalize_video(src, norm)
    final = tmp_path / "final.mp4"
    info, ts = insert_cover(norm, cover, final, cover_duration_ms=cover_ms)
    return final, info, ts


def test_very_short_video(tmp_path, cover):
    src = _make_video(tmp_path / "short.mp4", duration=1)
    final, info, ts = _full_build(src, tmp_path, cover)
    assert info.duration >= 1.0
    assert 0 <= ts < info.duration * 1000
    report = validate_final(final, cover_timestamp_ms=ts)
    assert not any("timestamp" in i for i in report.issues)


def test_longer_video(tmp_path, cover):
    src = _make_video(tmp_path / "long.mp4", duration=12)
    final, info, ts = _full_build(src, tmp_path, cover)
    assert info.duration == pytest.approx(12.5, abs=0.4)
    assert ts == 250
    assert validate_final(final, cover_timestamp_ms=ts).ok


def test_fractional_frame_rate_source(tmp_path, cover):
    """NTSC 29.97 fps source must normalize to clean 30 fps and validate."""
    import subprocess

    from tta.video import find_ffmpeg

    src = tmp_path / "ntsc.mp4"
    subprocess.run(
        [find_ffmpeg(), "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30000/1001:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert probe(src).fps == pytest.approx(29.97, abs=0.01)
    final, info, ts = _full_build(src, tmp_path, cover)
    assert info.fps == 30.0
    report = validate_final(final, cover_timestamp_ms=ts)
    assert report.ok, report.issues


def test_video_without_audio_full_path(tmp_path, cover):
    src = _make_video(tmp_path / "mute.mp4", duration=3, audio=False)
    final, info, ts = _full_build(src, tmp_path, cover)
    assert info.has_audio and info.audio_codec == "aac"  # silence injected
    assert validate_final(final, cover_timestamp_ms=ts).ok


def test_cover_frame_visually_present_at_timestamp(tmp_path, cover):
    """Extract the frame at the computed timestamp and compare it to the
    cover image - it must actually BE the cover (not just metadata)."""
    import subprocess

    from PIL import Image

    from tta.video import find_ffmpeg

    src = _make_video(tmp_path / "clip.mp4", duration=3)
    final, info, ts = _full_build(src, tmp_path, cover)

    frame_png = tmp_path / "frame.png"
    subprocess.run(
        [find_ffmpeg(), "-y", "-ss", f"{ts / 1000:.3f}", "-i", str(final),
         "-frames:v", "1", str(frame_png)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with Image.open(frame_png) as frame, Image.open(cover) as ref:
        small_f = frame.convert("RGB").resize((54, 96))
        small_r = ref.convert("RGB").resize((54, 96))
        diff = [
            abs(a - b)
            for pf, pr in zip(small_f.getdata(), small_r.getdata())
            for a, b in zip(pf, pr)
        ]
        mean_diff = sum(diff) / len(diff)
    # h264-compressed frame vs source PNG: near-identical, far from random
    assert mean_diff < 12, f"frame at {ts}ms does not match the cover (mean diff {mean_diff:.1f})"

    # ...and a frame in the middle of the actual content must NOT be the cover
    subprocess.run(
        [find_ffmpeg(), "-y", "-ss", "2.0", "-i", str(final),
         "-frames:v", "1", str(frame_png)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with Image.open(frame_png) as frame, Image.open(cover) as ref:
        small_f = frame.convert("RGB").resize((54, 96))
        small_r = ref.convert("RGB").resize((54, 96))
        diff = [
            abs(a - b)
            for pf, pr in zip(small_f.getdata(), small_r.getdata())
            for a, b in zip(pf, pr)
        ]
        assert sum(diff) / len(diff) > 20


def test_ms_conversion_and_clamping():
    assert compute_cover_timestamp(500, 10.0) == 250          # middle of segment
    assert compute_cover_timestamp(1000, 0.4) == 399          # clamped: 400ms video
    assert compute_cover_timestamp(500, 0.0) == 250           # unknown duration: raw
    assert compute_cover_timestamp(0, 5.0) == 0
    assert compute_cover_timestamp(500, 0.001) == 0           # 1ms video -> 0
    # never negative
    for ms, dur in ((0, 0.0), (100, 0.05), (10_000, 1.0)):
        assert compute_cover_timestamp(ms, dur) >= 0
