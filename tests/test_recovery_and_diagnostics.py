"""Crash recovery, provider retry/fallback behaviour and diagnostics."""
from __future__ import annotations

import time

import pytest

from app.diagnostics import FAIL, PASS, WARN, format_report, run_diagnostics
from app.jobs.states import INTERRUPTED, JobState, can_transition
from app.storage.db import Database


# ------------------------------------------------------------------ state machine
def test_metadata_goes_straight_to_video_build():
    """The image stage is gone from the production flow (kept only for old DBs)."""
    assert can_transition(JobState.GENERATING_METADATA, JobState.BUILDING_VIDEO)
    # legacy jobs stuck in the removed state are still recoverable
    assert JobState.GENERATING_IMAGE in INTERRUPTED


def test_validating_video_is_part_of_the_flow():
    assert can_transition(JobState.BUILDING_VIDEO, JobState.VALIDATING_VIDEO)
    assert can_transition(JobState.VALIDATING_VIDEO, JobState.UPLOADING)
    assert can_transition(JobState.VALIDATING_VIDEO, JobState.COMPLETED)   # dry run
    assert not can_transition(JobState.BUILDING_VIDEO, JobState.UPLOADING)
    assert JobState.VALIDATING_VIDEO in INTERRUPTED


# ------------------------------------------------------------------ persistence
@pytest.mark.parametrize("state", sorted(INTERRUPTED, key=lambda s: s.value))
def test_every_interrupted_state_is_recovered(settings, state):
    db = Database(settings.db_path)
    try:
        jid = db.create_job(base_name="j", video_path="/v.mp4", metadata_path="/v.txt")
        db.update_job(jid, state=state.value, locked_by="dead-worker")
        assert db.recover_interrupted() == [jid]
        job = db.get_job(jid)
        assert job["state"] == JobState.RETRY_PENDING.value
        assert job["locked_by"] is None
        assert jid in [j["id"] for j in db.due_retries()]
    finally:
        db.close()


def test_terminal_jobs_are_not_recovered(settings):
    db = Database(settings.db_path)
    try:
        done = db.create_job(base_name="done", video_path="/a.mp4", metadata_path="/a.txt")
        db.update_job(done, state=JobState.COMPLETED.value)
        waiting = db.create_job(base_name="wait", video_path="/b.mp4", metadata_path=None)
        db.update_job(waiting, state=JobState.WAITING_FOR_PAIR.value)
        assert db.recover_interrupted() == []
        assert db.get_job(done)["state"] == JobState.COMPLETED.value
        assert db.get_job(waiting)["state"] == JobState.WAITING_FOR_PAIR.value
    finally:
        db.close()


def test_jobs_and_events_survive_a_new_connection(settings):
    db = Database(settings.db_path)
    jid = db.create_job(base_name="persist", video_path="/p.mp4", metadata_path="/p.txt")
    db.set_state(jid, JobState.VALIDATING)
    db.update_job(jid, video_sha256="deadbeef", plan_json={"cover_text": "DREI WORTE HIER"})
    db.close()

    db2 = Database(settings.db_path)
    try:
        job = db2.get_job(jid)
        assert job["video_sha256"] == "deadbeef"
        assert job["plan"]["cover_text"] == "DREI WORTE HIER"
        assert any(e["event"] == "VALIDATING" for e in db2.events(jid))
        assert db2.find_by_hash("deadbeef")[0]["id"] == jid
    finally:
        db2.close()


# ------------------------------------------------------------------ diagnostics
def test_diagnostics_report_is_complete_and_honest(settings):
    checks = run_diagnostics(settings)
    names = {c.name for c in checks}
    for expected in ("Python", "FFmpeg", "ffprobe", "Filesystem permissions", "Disk space",
                     "Watched folder", "SQLite database", "TikTok app credentials",
                     "TikTok OAuth", "TikTok scopes",
                     "Local pipeline (video processing, DRY_RUN)", "Thumbnail / cover",
                     "TikTok upload path", "Mode"):
        assert expected in names
    # no image-generation diagnostics remain, and none can fail the report
    assert not [n for n in names if "provider" in n.lower() and "TikTok" not in n]
    assert not [n for n in names if "font" in n.lower() or "image" in n.lower()]
    assert all(c.status in (PASS, WARN, FAIL) for c in checks)

    local = next(c for c in checks if c.name.startswith("Local pipeline"))
    assert local.status == PASS, local.detail
    cover = next(c for c in checks if c.name == "Thumbnail / cover")
    assert cover.status == PASS and "manually in TikTok" in cover.detail

    upload = next(c for c in checks if c.name == "TikTok upload path")
    assert upload.status == WARN and "DRY_RUN=false" in upload.detail

    report = format_report(checks)
    assert "DIAGNOSTIC REPORT" in report and "PASS=" in report
    assert "test-secret" not in report                  # never print credentials


def test_diagnostics_flags_missing_scope(settings):
    settings.tiktok_post_mode = "DIRECT_POST"
    settings.tiktok_scopes = "user.info.basic,video.upload"
    scope_check = next(c for c in run_diagnostics(settings) if c.name == "TikTok scopes")
    assert scope_check.status == FAIL and "video.publish" in scope_check.detail
