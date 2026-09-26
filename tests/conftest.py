from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# the test suite must never touch the network or TikTok
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("TIKTOK_MOCK", "true")
os.environ.setdefault("ALLOW_PAID_API", "false")

from app.config import Settings, set_settings  # noqa: E402
from app.storage.db import Database  # noqa: E402
from app.video.ffmpeg import find_ffmpeg  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        base_dir=tmp_path,
        input_dir=tmp_path / "input",
        processing_dir=tmp_path / "processing",
        output_dir=tmp_path / "output",
        failed_dir=tmp_path / "failed",
        archive_dir=tmp_path / "archive",
        covers_dir=tmp_path / "covers",
        logs_dir=tmp_path / "logs",
        state_dir=tmp_path / "state",
        dry_run=True,
        tiktok_mock=True,
        allow_paid_api=False,
        allow_offline_fallback=True,
        image_provider="offline",
        stability_seconds=0.0,
        tiktok_client_key="test-key",
        tiktok_client_secret="test-secret",
        tiktok_backoff_base_seconds=0.0,
        tiktok_status_poll_seconds=0.0,
        tiktok_max_retries=2,
    )
    s.ensure_dirs()
    set_settings(s)
    yield s
    set_settings(None)  # type: ignore[arg-type]


@pytest.fixture()
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    yield database
    database.close()


@pytest.fixture(scope="session")
def ffmpeg_exe() -> str:
    return find_ffmpeg(None)


@pytest.fixture()
def sample_video(tmp_path: Path, ffmpeg_exe: str) -> Path:
    """A real, tiny 3-second 1080x1920 H.264+AAC video generated with FFmpeg."""
    out = tmp_path / "sample_source.mp4"
    subprocess.run([
        ffmpeg_exe, "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
        "-c:a", "aac", "-shortest", str(out)], check=True, capture_output=True)
    return out


@pytest.fixture()
def landscape_video(tmp_path: Path, ffmpeg_exe: str) -> Path:
    out = tmp_path / "landscape.mp4"
    subprocess.run([
        ffmpeg_exe, "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
        "-an", str(out)], check=True, capture_output=True)
    return out


@pytest.fixture()
def metadata_text() -> str:
    return (
        "TITLE:\nDie Zeichen, die deine Seele dir zeigen will\n\n"
        "DESCRIPTION:\nEine mystische Reflexion über Intuition, wiederkehrende Muster und "
        "Momente, die sich spirituell bedeutsam anfühlen. Nichts davon ist Zufall.\n\n"
        "IMAGE_PROMPT:\nCinematic mystical night scene, solitary silhouette under a vast "
        "celestial sky, deep blue and violet tones, volumetric light.\n"
    )
