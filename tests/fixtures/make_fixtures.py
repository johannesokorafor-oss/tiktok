"""Create the local test fixtures (no external assets required).

Generates a tiny but *real* H.264 + AAC video and its metadata sidecar:

    tests/fixtures/example.mp4
    tests/fixtures/example.txt

Run directly (``python tests/fixtures/make_fixtures.py``) or let the test
suite call :func:`ensure_fixtures`, which regenerates the video on demand so
no binary blob has to live in Git.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent
VIDEO = FIXTURES / "example.mp4"
LANDSCAPE = FIXTURES / "example_landscape.mp4"
TEXT = FIXTURES / "example.txt"

METADATA = """TITLE:
Die Zeichen, die deine Seele dir zeigen will

DESCRIPTION:
Eine ruhige, mystische Betrachtung über Intuition, wiederkehrende Muster und
Momente, die sich spirituell bedeutsam anfühlen. Warum tauchen dieselben Zahlen
und Träume immer wieder auf? Nichts davon ist Zufall, wenn du genauer hinsiehst.

IMAGE_PROMPT:
A cinematic mystical night scene with a solitary human silhouette standing
beneath a vast celestial sky, subtle glowing symbols in the atmosphere, deep
blue and violet tones, dramatic volumetric light, realistic cinematic
photography, strong depth, highly detailed, premium visual style.
"""


def _ffmpeg() -> str:
    sys.path.insert(0, str(FIXTURES.parent.parent))
    from app.video.ffmpeg import find_ffmpeg
    return find_ffmpeg(None)


def build_vertical(path: Path = VIDEO, duration: float = 4.0) -> Path:
    """A 1080x1920 test clip with audio - the shape TikTok expects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        _ffmpeg(), "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1080x1920:rate=30:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-crf", "30",
        "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart", str(path),
    ], check=True, capture_output=True)
    return path


def build_landscape(path: Path = LANDSCAPE, duration: float = 2.0) -> Path:
    """A 1920x1080 clip without audio, to exercise the 9:16 conversion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        _ffmpeg(), "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=25:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-crf", "30",
        "-an", str(path),
    ], check=True, capture_output=True)
    return path


def ensure_fixtures() -> tuple[Path, Path]:
    if not TEXT.is_file():
        TEXT.write_text(METADATA, encoding="utf-8")
    if not VIDEO.is_file() or VIDEO.stat().st_size < 1000:
        build_vertical()
    return VIDEO, TEXT


if __name__ == "__main__":
    video, text = ensure_fixtures()
    landscape = build_landscape()
    for p in (video, text, landscape):
        print(f"{p}  ({p.stat().st_size} bytes)")
