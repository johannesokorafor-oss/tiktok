import shutil
import subprocess
from pathlib import Path

import pytest

from tta.config import Config
from tta.db import JobStore
from tta.video import find_ffmpeg


@pytest.fixture()
def config(tmp_path) -> Config:
    cfg = Config(home=tmp_path)
    cfg.dry_run = True
    cfg.stabilize_seconds = 0.2
    cfg.scan_interval = 0.05
    cfg.pair_timeout = 5.0
    cfg.max_retries = 1
    cfg.image_provider = "local"
    cfg.cover_duration_ms = 400
    cfg.ensure_dirs()
    return cfg


@pytest.fixture()
def store(config) -> JobStore:
    s = JobStore(config.db_path)
    yield s
    s.close()


def _make_video(path: Path, size="640x360", duration=4, audio=True) -> Path:
    """Create a small real H.264 test clip with ffmpeg."""
    exe = find_ffmpeg()
    cmd = [exe, "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    return path


@pytest.fixture(scope="session")
def fixture_video(tmp_path_factory) -> Path:
    """Session-cached landscape test video (real MP4, H.264+AAC)."""
    base = tmp_path_factory.mktemp("fixtures")
    return _make_video(base / "fixture.mp4")


@pytest.fixture(scope="session")
def fixture_video_vertical(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("fixtures_v")
    return _make_video(base / "fixture_v.mp4", size="540x960")


@pytest.fixture(scope="session")
def fixture_video_noaudio(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("fixtures_na")
    return _make_video(base / "fixture_na.mp4", audio=False)


SAMPLE_TEXT = """TITLE:
The universe sends you signs

DESCRIPTION:
When you feel lost, the universe often sends subtle signs to guide your
spiritual awakening. Learn to trust your intuition and inner energy.

IMAGE_PROMPT:
a lone silhouette on a mountain under a glowing night sky, mystical light
"""


@pytest.fixture()
def input_pair(config, fixture_video):
    """A ready video+text pair inside the watched input folder."""
    video = config.input_dir / "myclip.mp4"
    text = config.input_dir / "myclip.txt"
    shutil.copy2(fixture_video, video)
    text.write_text(SAMPLE_TEXT, encoding="utf-8")
    return video, text
