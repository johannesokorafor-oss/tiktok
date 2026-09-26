"""Real end-to-end runs against the fixtures in tests/fixtures/.

These tests execute the *production* pipeline (no pipeline-level mocking):
real parsing, real image rendering through the provider registry, real Pillow
typography, real FFmpeg encoding and real ffprobe validation. Only the TikTok
network layer is replaced (DRY_RUN / TIKTOK_MOCK), because uploading from a
test suite is forbidden.

Artifacts are written to tests/output/ so they can be inspected by hand.
"""
from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.config import QualityMode, StylePreset
from app.images.base import ImageRequest
from app.images.providers.comfyui import ComfyUIProvider
from app.images.providers.offline import OfflineProvider
from app.jobs.pipeline import Pipeline
from app.jobs.states import JobState
from app.storage.db import Database
from app.video.ffmpeg import probe
from app.video.validate import validate_video
from app.watcher.watcher import WatcherService
from tests.fixtures.make_fixtures import ensure_fixtures

TEST_OUTPUT = Path(__file__).resolve().parent / "output"


@pytest.fixture(scope="session")
def fixtures() -> tuple[Path, Path]:
    return ensure_fixtures()


@pytest.fixture()
def e2e_settings(settings):
    """Route the application's output at tests/output/ for inspection."""
    TEST_OUTPUT.mkdir(parents=True, exist_ok=True)
    settings.output_dir = TEST_OUTPUT
    settings.ensure_dirs()
    return settings


def test_full_dry_run_from_watched_folder(e2e_settings, db, fixtures):
    """input/example.mp4 + example.txt -> complete artifacts, nothing uploaded."""
    video_src, text_src = fixtures
    if (TEST_OUTPUT / "example").exists():
        shutil.rmtree(TEST_OUTPUT / "example")
    video = e2e_settings.input_dir / "example.mp4"
    text = e2e_settings.input_dir / "example.txt"
    shutil.copy2(video_src, video)
    shutil.copy2(text_src, text)
    video_before = video.read_bytes()
    text_before = text.read_bytes()

    watcher = WatcherService(e2e_settings, db, Pipeline(e2e_settings, db))
    processed = watcher.scan_once()
    assert len(processed) == 1

    job = db.get_job(processed[0])
    assert job["state"] == JobState.COMPLETED.value, job.get("error")

    out = TEST_OUTPUT / "example"
    expected = {"source.mp4", "metadata.txt", "cover.png", "final_tiktok.mp4",
                "job.json", "upload_result.json", "caption.txt", "source_reference.txt"}
    assert expected <= {p.name for p in out.iterdir()}

    # --- cover is a real 1080x1920 PNG
    from PIL import Image
    with Image.open(out / "cover.png") as img:
        assert img.size == (1080, 1920) and img.format == "PNG"

    # --- final video is a valid, decodable TikTok-compatible file
    check = validate_video(out / "final_tiktok.mp4", e2e_settings, source_had_audio=True)
    assert check.ok, check.errors
    info = check.info
    assert (info.width, info.height) == (1080, 1920)
    assert info.video_codec == "h264" and info.audio_codec == "aac"

    # --- job record
    record = json.loads((out / "job.json").read_text(encoding="utf-8"))
    assert record["video_sha256"] and record["dry_run"] is True
    assert 2 <= len(record["plan"]["hook_words"]) <= 4
    assert record["plan"]["language"] == "de"
    assert record["cover"]["validation"]["ok"] is True
    assert 0 < record["video_build"]["cover_timestamp_ms"] < info.duration * 1000
    assert record["video_validation"]["ok"] is True

    # --- nothing was uploaded
    upload = json.loads((out / "upload_result.json").read_text(encoding="utf-8"))
    assert upload["skipped"] is True and "DRY_RUN" in upload["reason"]

    # --- originals untouched, database persisted
    assert video.read_bytes() == video_before
    assert text.read_bytes() == text_before
    assert e2e_settings.db_path.is_file()


def test_pipeline_state_survives_restart(e2e_settings, db, fixtures):
    """A job interrupted mid-flight is recovered from SQLite, not lost."""
    video_src, text_src = fixtures
    video = e2e_settings.input_dir / "restart.mp4"
    shutil.copy2(video_src, video)
    shutil.copy2(text_src, e2e_settings.input_dir / "restart.txt")

    job_id = db.create_job(base_name="restart", video_path=str(video),
                           metadata_path=str(e2e_settings.input_dir / "restart.txt"),
                           dry_run=True)
    db.set_state(job_id, JobState.VALIDATING)
    db.set_state(job_id, JobState.GENERATING_METADATA)
    db.set_state(job_id, JobState.GENERATING_IMAGE)
    db.try_lock(job_id, "dead-process")
    db.close()

    # "restart": a brand new Database object on the same file
    db2 = Database(e2e_settings.db_path)
    try:
        recovered = db2.recover_interrupted()
        assert job_id in recovered
        restored = db2.get_job(job_id)
        assert restored["state"] == JobState.RETRY_PENDING.value
        assert restored["locked_by"] is None
        assert any(e["event"] == "RECOVERED_AFTER_RESTART" for e in db2.events(job_id))

        # the watcher picks the recovered job up on its next pass and finishes it
        watcher = WatcherService(e2e_settings, db2, Pipeline(e2e_settings, db2))
        assert job_id in watcher.scan_once()
        assert db2.get_job(job_id)["state"] == JobState.COMPLETED.value
    finally:
        db2.close()


def test_duplicate_is_not_uploaded_twice(e2e_settings, db, fixtures):
    video_src, text_src = fixtures
    video = e2e_settings.input_dir / "dupe.mp4"
    text = e2e_settings.input_dir / "dupe.txt"
    shutil.copy2(video_src, video)
    shutil.copy2(text_src, text)

    e2e_settings.dry_run = False
    e2e_settings.tiktok_mock = True
    pipeline = Pipeline(e2e_settings, db)

    first = pipeline.run(db.create_job(base_name="dupe", video_path=str(video),
                                       metadata_path=str(text), dry_run=False))
    assert first["state"] == JobState.COMPLETED.value, first.get("error")
    assert first["publish_id"] and first["upload_status"]

    # same bytes under a different name -> duplicate, no second upload
    copy_video = e2e_settings.input_dir / "dupe_copy.mp4"
    shutil.copy2(video, copy_video)
    shutil.copy2(text, e2e_settings.input_dir / "dupe_copy.txt")
    second_id = db.create_job(base_name="dupe_copy", video_path=str(copy_video),
                              metadata_path=str(e2e_settings.input_dir / "dupe_copy.txt"),
                              dry_run=False)
    second = pipeline.run(second_id)
    assert second["state"] == JobState.DUPLICATE.value
    assert second["publish_id"] is None

    forced = pipeline.run(second_id, force=True)
    assert forced["state"] == JobState.COMPLETED.value, forced.get("error")
    assert forced["publish_id"]


def test_landscape_source_becomes_vertical(e2e_settings, db):
    from tests.fixtures.make_fixtures import build_landscape
    src = build_landscape(e2e_settings.input_dir / "wide.mp4")
    (e2e_settings.input_dir / "wide.txt").write_text(
        "TITLE:\nLuxury mindset shift\n\nDESCRIPTION:\nA short note about discipline, "
        "money and success.\n", encoding="utf-8")
    watcher = WatcherService(e2e_settings, db, Pipeline(e2e_settings, db))
    processed = watcher.scan_once()
    job = db.get_job(processed[0])
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    info = probe(Path(job["final_video_path"]), e2e_settings)
    assert (info.width, info.height) == (1080, 1920)
    assert probe(src, e2e_settings).width == 1920          # source untouched
    assert job["plan"]["style_preset"] == StylePreset.DARK_LUXURY.value


def test_style_presets_produce_different_covers(e2e_settings, db, fixtures):
    """Each preset must actually change the composition, not just a label."""
    from app.cover.compose import CoverComposer
    from app.cover.presets import get_preset

    bg = OfflineProvider(e2e_settings).generate(
        ImageRequest(prompt="mystical night", width=1080, height=1920, seed=11)).data
    digests = {}
    layouts = {}
    for style in (StylePreset.CINEMATIC_MYSTICAL, StylePreset.DARK_LUXURY,
                  StylePreset.CLEAN_MODERN):
        composed = CoverComposer(get_preset(style)).compose(bg, ["DEINE", "SEELE", "SPRICHT"])
        path = composed.save(TEST_OUTPUT / f"preset_{style.value}.png")
        digests[style] = path.read_bytes()
        layouts[style] = composed.layout
    assert len({d for d in digests.values()}) == 3
    assert len({l.font_size for l in layouts.values()}) > 1


# ---------------------------------------------------------------- local HTTP provider
class _FakeComfyUI(BaseHTTPRequestHandler):
    """Minimal stand-in that speaks the documented ComfyUI HTTP API.

    Used to prove the *local HTTP provider adapter* really performs the
    documented request/response cycle and returns real image bytes.
    """

    png: bytes = b""

    def log_message(self, *args):  # silence the test server
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self._json({"prompt_id": "test-prompt"})

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/history/"):
            self._json({"test-prompt": {"outputs": {"9": {"images": [
                {"filename": "cover.png", "subfolder": "", "type": "output"}]}}}})
        elif self.path.startswith("/view"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(self.png)))
            self.end_headers()
            self.wfile.write(self.png)
        elif self.path.startswith("/system_stats"):
            self._json({"system": {"os": "test"}})
        else:
            self.send_error(404)

    def _json(self, payload):
        import json as _json
        body = _json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_local_http_provider_roundtrip(e2e_settings):
    """The ComfyUI-compatible adapter performs a real HTTP roundtrip."""
    _FakeComfyUI.png = OfflineProvider(e2e_settings).generate(
        ImageRequest(prompt="local http", width=1080, height=1920, seed=5)).data
    server = HTTPServer(("127.0.0.1", 0), _FakeComfyUI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        e2e_settings.comfyui_base_url = f"http://127.0.0.1:{server.server_port}"
        provider = ComfyUIProvider(e2e_settings)
        healthy, detail = provider.health()
        assert healthy, detail
        result = provider.generate(ImageRequest(prompt="a mystical sky", negative_prompt="text",
                                                width=1080, height=1920, seed=5))
        assert result.provider == "comfyui" and result.mime == "image/png"
        from PIL import Image
        import io
        with Image.open(io.BytesIO(result.data)) as img:
            assert img.size == (1080, 1920)
    finally:
        server.shutdown()
        server.server_close()


def test_high_quality_mode_end_to_end(e2e_settings, db, fixtures):
    video_src, text_src = fixtures
    shutil.copy2(video_src, e2e_settings.input_dir / "hq.mp4")
    shutil.copy2(text_src, e2e_settings.input_dir / "hq.txt")
    e2e_settings.quality_mode = QualityMode.HIGH_QUALITY
    watcher = WatcherService(e2e_settings, db, Pipeline(e2e_settings, db))
    job = db.get_job(watcher.scan_once()[0])
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    record = json.loads((Path(job["output_dir"]) / "job.json").read_text(encoding="utf-8"))
    ok_attempts = [a for a in record["cover"]["attempts"] if a["status"] == "ok"]
    assert len(ok_attempts) == 3                      # candidates were scored
    assert record["cover"]["score"]["total"] == max(a["score"]["total"] for a in ok_attempts)
