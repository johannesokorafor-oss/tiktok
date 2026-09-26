"""Crash recovery, provider retry/fallback behaviour and diagnostics."""
from __future__ import annotations

import time

import pytest

from app.diagnostics import FAIL, PASS, WARN, format_report, run_diagnostics
from app.images.base import ImageProviderError, PaidProviderBlocked
from app.images.providers.offline import OfflineProvider
from app.images.providers.pollinations import PollinationsProvider
from app.images.registry import ImageService
from app.jobs.states import INTERRUPTED, JobState, can_transition
from app.storage.db import Database


# ------------------------------------------------------------------ state machine
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


# ------------------------------------------------------------------ provider retry
def test_transient_provider_error_is_retried_then_succeeds(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    settings.image_provider = "pollinations"
    settings.image_max_retries = 2
    calls = {"n": 0}
    real = OfflineProvider(settings)

    def flaky(self, request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ImageProviderError("pollinations rate limit (HTTP 429)")
        return real.generate(request)

    monkeypatch.setattr(PollinationsProvider, "generate", flaky)
    result, score, attempts = ImageService(settings).generate_background("night sky", "text")
    assert calls["n"] == 2
    assert result.provider == "offline"          # payload produced by the stand-in renderer
    assert [a["status"] for a in attempts] == ["error", "ok"]
    assert attempts[0]["retryable"] is True


def test_non_transient_error_is_not_retried(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    settings.image_provider = "auto"
    settings.image_provider_primary = "pollinations"
    settings.image_provider_fallback = "offline"
    settings.image_max_retries = 3
    calls = {"n": 0}

    def broken(self, request):
        calls["n"] += 1
        raise ImageProviderError("model 'x' is not in the verified free model set")

    monkeypatch.setattr(PollinationsProvider, "generate", broken)
    result, _score, attempts = ImageService(settings).generate_background("night", "")
    assert calls["n"] == 1                       # no pointless retries
    assert result.provider == "offline"          # chain fell through
    assert attempts[0]["retryable"] is False


def test_retries_are_bounded_and_chain_falls_through(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    settings.image_provider = "auto"
    settings.image_provider_primary = "pollinations"
    settings.image_provider_fallback = "offline"
    settings.image_max_retries = 2
    calls = {"n": 0}

    def always_timeout(self, request):
        calls["n"] += 1
        raise ImageProviderError("pollinations request failed: timeout")

    monkeypatch.setattr(PollinationsProvider, "generate", always_timeout)
    result, _s, attempts = ImageService(settings).generate_background("night", "")
    assert calls["n"] == 3                       # initial + 2 retries, then stop
    assert result.provider == "offline"
    assert sum(1 for a in attempts if a["status"] == "error") == 3


def test_unexpected_provider_exception_does_not_escape(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    settings.image_provider = "auto"
    settings.image_provider_primary = "pollinations"
    settings.image_provider_fallback = "offline"

    def explode(self, request):
        raise RuntimeError("driver segfault simulation")

    monkeypatch.setattr(PollinationsProvider, "generate", explode)
    result, _s, attempts = ImageService(settings).generate_background("night", "")
    assert result.provider == "offline"
    assert any("driver segfault" in str(a.get("detail", "")) for a in attempts)


def test_blocked_paid_provider_is_reported_not_retried(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    settings.image_provider = "auto"
    settings.image_provider_primary = "huggingface"
    settings.image_provider_fallback = "offline"
    settings.huggingface_api_token = "hf_dummy"
    monkeypatch.delenv("HUGGINGFACE_ACCEPT_QUOTA", raising=False)
    result, _s, attempts = ImageService(settings).generate_background("night", "")
    assert result.provider == "offline"
    assert any(a["status"] == "blocked" for a in attempts)


def test_all_providers_failing_raises_a_clear_error(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    settings.image_provider = "offline"
    settings.image_max_retries = 0

    def broken(self, request):
        raise ImageProviderError("renderer unavailable")

    monkeypatch.setattr(OfflineProvider, "generate", broken)
    with pytest.raises(ImageProviderError) as exc:
        ImageService(settings).generate_background("night", "")
    assert "all image providers failed" in str(exc.value)


# ------------------------------------------------------------------ diagnostics
def test_diagnostics_report_is_complete_and_honest(settings):
    checks = run_diagnostics(settings)
    names = {c.name for c in checks}
    for expected in ("Python", "FFmpeg", "ffprobe", "Filesystem permissions", "Disk space",
                     "SQLite database", "Headline font", "TikTok app credentials",
                     "TikTok OAuth", "TikTok scopes", "Local pipeline (cover + video, DRY_RUN)",
                     "TikTok upload path", "Mode"):
        assert expected in names
    assert all(c.status in (PASS, WARN, FAIL) for c in checks)

    local = next(c for c in checks if c.name.startswith("Local pipeline"))
    assert local.status == PASS, local.detail          # offline path must work here

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
