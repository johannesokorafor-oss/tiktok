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

from app.config import QualityMode
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
    expected = {"source.mp4", "metadata.txt", "final_tiktok.mp4",
                "job.json", "upload_result.json", "caption.txt", "source_reference.txt"}
    produced = {p.name for p in out.iterdir()}
    assert expected == produced, produced
    assert not list(out.glob("*.png")) and not list(out.glob("*cover*"))

    # --- final video is a valid, decodable TikTok-compatible file
    check = validate_video(out / "final_tiktok.mp4", e2e_settings, source_had_audio=True)
    assert check.ok, check.errors
    info = check.info
    assert (info.width, info.height) == (1080, 1920)
    assert info.video_codec == "h264" and info.audio_codec == "aac"

    # --- job record
    record = json.loads((out / "job.json").read_text(encoding="utf-8"))
    assert record["video_sha256"] and record["dry_run"] is True
    assert record["plan"]["language"] == "de" and record["plan"]["caption"]
    assert record["cover"]["generated"] is False
    assert record["video_build"]["cover_embedded"] is False
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
    db.set_state(job_id, JobState.BUILDING_VIDEO)
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




def test_pipeline_runs_without_any_image_provider(e2e_settings, db, fixtures, monkeypatch):
    """No image module exists any more - the pipeline must not need one."""
    import importlib
    for gone in ("app.images", "app.images.registry", "app.cover", "app.cover.compose"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(gone)

    video_src, text_src = fixtures
    shutil.copy2(video_src, e2e_settings.input_dir / "noprov.mp4")
    shutil.copy2(text_src, e2e_settings.input_dir / "noprov.txt")

    # any outbound HTTP attempt would be a bug: make it explode
    import httpx

    def boom(*args, **kwargs):
        raise AssertionError("the pipeline must not perform any HTTP request")

    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.Client, "request", boom)

    watcher = WatcherService(e2e_settings, db, Pipeline(e2e_settings, db))
    job = db.get_job(watcher.scan_once()[0])
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    assert not list(Path(job["output_dir"]).glob("*.png"))


def test_video_quality_mode_still_works_end_to_end(e2e_settings, db, fixtures):
    video_src, text_src = fixtures
    shutil.copy2(video_src, e2e_settings.input_dir / "hq.mp4")
    shutil.copy2(text_src, e2e_settings.input_dir / "hq.txt")
    e2e_settings.quality_mode = QualityMode.HIGH_QUALITY
    e2e_settings.video_normalization = "always"          # force a re-encode
    watcher = WatcherService(e2e_settings, db, Pipeline(e2e_settings, db))
    job = db.get_job(watcher.scan_once()[0])
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    record = json.loads((Path(job["output_dir"]) / "job.json").read_text(encoding="utf-8"))
    assert record["video_build"]["processing"] == "transcode"
    assert record["video_build"]["cover_embedded"] is False
