"""Instagram integration tests - fully offline against a local fake Meta server.

Nothing here talks to the real Instagram API. The central invariant of every
test: ``media_publish`` is never called.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from app.config import (InstagramLoginMode, InstagramUploadMethod, PlatformStatus, Settings)
from app.instagram.client import (InstagramAPIError, InstagramClient, PublishingDisabledError)
from app.instagram.oauth import (MIN_REFRESH_AGE_SECONDS, InstagramOAuth, InstagramOAuthError,
                                 InstagramTokens, InstagramTokenStore)
from app.instagram.service import (AUTO_PUBLISH_REFUSED, READY_MESSAGE, InstagramService,
                                   build_instagram_caption)
from app.instagram.validate import validate_reel
from app.jobs.pipeline import Pipeline
from app.jobs.states import JobState
from app.watcher.watcher import WatcherService
from tests.fake_instagram import FakeInstagramServer


# ---------------------------------------------------------------- fixtures
@pytest.fixture()
def ig_settings(settings):
    settings.instagram_enabled = True
    settings.instagram_client_id = "ig-client"
    settings.instagram_client_secret = "ig-secret"
    settings.instagram_login_mode = InstagramLoginMode.FACEBOOK_LOGIN   # resumable
    settings.instagram_status_poll_seconds = 0.0
    settings.instagram_backoff_base_seconds = 0.0
    settings.instagram_max_retries = 2
    return settings


@pytest.fixture()
def fake():
    with FakeInstagramServer() as server:
        yield server


def _authenticate(settings, oauth: InstagramOAuth) -> None:
    oauth.store.save(InstagramTokens(
        access_token="IGQlonglived", user_id="17841400000000000",
        permissions="instagram_business_basic,instagram_business_content_publish",
        issued_at=time.time() - 2 * MIN_REFRESH_AGE_SECONDS,
        expires_at=time.time() + 50 * 86400, long_lived=True))


# ---------------------------------------------------------------- OAuth
def test_authorize_url_uses_documented_endpoint_and_scopes(ig_settings):
    auth = InstagramOAuth(ig_settings).authorize_url()
    assert auth.url.startswith("https://www.instagram.com/oauth/authorize")
    assert "client_id=ig-client" in auth.url and "response_type=code" in auth.url
    assert "instagram_business_basic" in auth.url
    assert "instagram_business_content_publish" in auth.url
    assert "state=" in auth.url


def test_scope_validation_reports_missing_permissions(ig_settings):
    oauth = InstagramOAuth(ig_settings)
    assert oauth.missing_scopes("instagram_business_basic") == [
        "instagram_business_content_publish"]
    assert oauth.missing_scopes(
        "instagram_business_basic,instagram_business_content_publish") == []


def test_code_exchange_gets_short_then_long_lived_token(ig_settings, fake):
    fake.configure(ig_settings)
    oauth = InstagramOAuth(ig_settings)
    state = oauth.states.issue()
    tokens = oauth.exchange_code("AQB-code", state)
    assert tokens.access_token == "IGQlonglived" and tokens.long_lived
    assert tokens.user_id == "17841400000000000"
    assert tokens.expires_at - time.time() > 50 * 86400
    paths = [p for _m, p, _f in fake.state.requests]
    assert any(p.endswith("/oauth/access_token") for p in paths)
    assert any(p.endswith("/access_token") for p in paths)          # ig_exchange_token


def test_oauth_state_is_validated(ig_settings):
    with pytest.raises(InstagramOAuthError):
        InstagramOAuth(ig_settings).exchange_code("code", "forged")


def test_refresh_requires_24h_old_long_lived_token(ig_settings, fake):
    fake.configure(ig_settings)
    oauth = InstagramOAuth(ig_settings)
    oauth.store.save(InstagramTokens(access_token="t", issued_at=time.time(),
                                     expires_at=time.time() + 86400, long_lived=True))
    with pytest.raises(InstagramOAuthError) as exc:
        oauth.refresh()
    assert "24 h old" in str(exc.value)

    oauth.store.save(InstagramTokens(access_token="t",
                                     issued_at=time.time() - MIN_REFRESH_AGE_SECONDS - 60,
                                     expires_at=time.time() + 86400, long_lived=True))
    refreshed = oauth.refresh()
    assert refreshed.access_token == "IGQrefreshed"


def test_expired_token_cannot_be_refreshed(ig_settings, fake):
    fake.configure(ig_settings)
    oauth = InstagramOAuth(ig_settings)
    oauth.store.save(InstagramTokens(access_token="t", issued_at=time.time() - 90 * 86400,
                                     expires_at=time.time() - 10, long_lived=True))
    with pytest.raises(InstagramOAuthError) as exc:
        oauth.refresh()
    assert "expired" in str(exc.value)


def test_tokens_are_never_exposed(ig_settings):
    tokens = InstagramTokens(access_token="IGQsecret123", user_id="1784")
    assert "IGQsecret123" not in str(tokens.public_dict())


def test_instagram_tokens_are_separate_from_tiktok(ig_settings):
    ig_store = InstagramTokenStore(ig_settings.state_dir / "instagram_tokens.json")
    ig_store.save(InstagramTokens(access_token="IGQ1"))
    assert ig_store.path != ig_settings.token_path
    assert ig_store.path.is_file()
    from app.tiktok.tokens import TokenStore
    assert not TokenStore(ig_settings.token_path).load().access_token


# ---------------------------------------------------------------- account + limits
def test_account_lookup_and_publishing_limit(ig_settings, fake):
    fake.configure(ig_settings)
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    client = InstagramClient(ig_settings, oauth=oauth)
    account = client.get_account()
    assert account["account_type"] == "BUSINESS" and account["id"]
    limit = client.publishing_limit()
    assert limit["quota_usage"] == 3 and limit["config"]["quota_total"] == 100
    client.close()


def test_service_rejects_non_professional_account(ig_settings, fake):
    fake.configure(ig_settings)
    fake.state.account_type = "PERSONAL"
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    result = InstagramService(ig_settings, oauth).test_connection()
    assert result["ok"] is False and "Professional" in result["detail"]


# ---------------------------------------------------------------- container flow
def test_container_creation_and_resumable_upload(ig_settings, fake, sample_video):
    fake.configure(ig_settings)
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    client = InstagramClient(ig_settings, oauth=oauth)

    container = client.create_container(ig_user_id="17841400000000000",
                                        caption="Hallo Welt #test", resumable=True)
    assert container.container_id.startswith("1789")
    form = fake.state.containers[container.container_id]["form"]
    assert form["media_type"] == "REELS" and form["upload_type"] == "resumable"
    assert form["caption"] == "Hallo Welt #test"
    assert "cover_url" not in form and "thumb_offset" not in form   # no cover, ever

    client.upload_video(container, sample_video)
    stored = fake.state.containers[container.container_id]
    assert stored["uploaded"] == sample_video.stat().st_size
    assert stored["auth_header"].startswith("OAuth ")
    assert stored["offset"] == "0"

    client.wait_for_container(container, sleeper=lambda s: None)
    assert container.status_code == "FINISHED"
    assert fake.state.publish_calls == []
    client.close()


def test_video_url_flow_when_resumable_is_unavailable(ig_settings, fake, sample_video):
    fake.configure(ig_settings)
    ig_settings.instagram_login_mode = InstagramLoginMode.INSTAGRAM_LOGIN
    ig_settings.instagram_public_video_url_template = "https://cdn.example.com/{filename}"
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    service = InstagramService(ig_settings, oauth)

    method, reason = service.resolve_upload_method(sample_video)
    assert method == "video_url" and "cdn.example.com" in reason

    client = InstagramClient(ig_settings, oauth=oauth)
    with pytest.raises(InstagramAPIError) as exc:
        client.create_container(ig_user_id="1784", resumable=True)
    assert "Facebook Login" in str(exc.value)
    container = client.create_container(ig_user_id="1784",
                                        video_url="https://cdn.example.com/final_tiktok.mp4")
    assert fake.state.containers[container.container_id]["form"]["video_url"].endswith(".mp4")
    client.close()


def test_instagram_login_without_public_url_is_not_configured(ig_settings, sample_video):
    ig_settings.instagram_login_mode = InstagramLoginMode.INSTAGRAM_LOGIN
    ig_settings.instagram_public_video_url_template = ""
    method, reason = InstagramService(ig_settings).resolve_upload_method(sample_video)
    assert method == "" and "Facebook Login only" in reason


def test_upload_retries_transient_failures(ig_settings, fake, sample_video, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake.configure(ig_settings)
    fake.state.upload_failures = 1
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    client = InstagramClient(ig_settings, oauth=oauth)
    container = client.create_container(ig_user_id="1784", resumable=True)
    client.upload_video(container, sample_video)
    assert fake.state.containers[container.container_id]["uploaded"] > 0
    client.close()


def test_rate_limit_is_retried(ig_settings, fake, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake.configure(ig_settings)
    fake.state.rate_limit_once = True
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    client = InstagramClient(ig_settings, oauth=oauth)
    container = client.create_container(ig_user_id="1784", resumable=True)
    client.get_container_status(container)
    assert container.status_code in {"IN_PROGRESS", "FINISHED"}
    client.close()


def test_container_error_status_is_reported(ig_settings, fake, sample_video):
    fake.configure(ig_settings)
    fake.state.status_sequence = ["ERROR"]
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    result = InstagramService(ig_settings, oauth).stage_video(sample_video, "caption")
    assert result.status == PlatformStatus.FAILED
    assert "ERROR" in (result.error or "")
    assert fake.state.publish_calls == []


def test_container_expiry_is_detected_by_status(ig_settings, fake, sample_video):
    fake.configure(ig_settings)
    fake.state.status_sequence = ["EXPIRED"]
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    result = InstagramService(ig_settings, oauth).stage_video(sample_video, "caption")
    assert result.status == PlatformStatus.EXPIRED
    assert "expired" in (result.error or "").lower()


def test_container_expiry_is_detected_by_age(ig_settings, fake):
    """Even if Meta still says IN_PROGRESS, a >24 h old container is expired."""
    fake.configure(ig_settings)
    fake.state.status_sequence = ["IN_PROGRESS"]
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    service = InstagramService(ig_settings, oauth)
    old = time.time() - 25 * 3600
    result = service.refresh_container_status("1789001", old)
    assert result.status == PlatformStatus.EXPIRED
    assert result.container.remaining_seconds(24) == 0


def test_expired_container_is_not_retried_forever(ig_settings, fake, sample_video):
    fake.configure(ig_settings)
    fake.state.status_sequence = ["EXPIRED"]
    ig_settings.instagram_status_poll_max = 5
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    InstagramService(ig_settings, oauth).stage_video(sample_video, "caption")
    status_calls = [p for m, p, _ in fake.state.requests
                    if m == "GET" and p.rstrip("/").split("/")[-1].startswith("1789")]
    assert len(status_calls) == 1          # terminal state -> stop immediately


# ---------------------------------------------------------------- safety
def test_media_publish_method_always_refuses(ig_settings):
    with pytest.raises(PublishingDisabledError):
        InstagramClient(ig_settings).media_publish("1784", "container")


def test_no_publish_call_in_the_whole_staging_flow(ig_settings, fake, sample_video):
    fake.configure(ig_settings)
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    result = InstagramService(ig_settings, oauth).stage_video(sample_video, "caption")
    assert result.status == PlatformStatus.READY_TO_PUBLISH
    assert result.message == READY_MESSAGE
    assert fake.state.publish_calls == []
    assert not any("media_publish" in p for _m, p, _f in fake.state.requests)


def test_auto_publish_flag_cannot_cause_publishing(ig_settings, fake, sample_video):
    """CRITICAL: even INSTAGRAM_AUTO_PUBLISH=true must never publish."""
    fake.configure(ig_settings)
    ig_settings.instagram_auto_publish = True
    oauth = InstagramOAuth(ig_settings)
    _authenticate(ig_settings, oauth)
    events = []
    result = InstagramService(ig_settings, oauth).stage_video(
        sample_video, "caption", on_event=lambda e, m, d: events.append(e))
    assert fake.state.publish_calls == []
    assert "INSTAGRAM_AUTO_PUBLISH_REFUSED" in events
    assert result.as_dict()["auto_publish"] is False
    assert result.as_dict()["auto_publish_note"] == AUTO_PUBLISH_REFUSED


def test_source_tree_never_calls_media_publish():
    """No code path in app/ may POST to media_publish (only the refusing stub)."""
    repo = Path(__file__).resolve().parent.parent
    offenders = []
    for path in (repo / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "media_publish" not in line:
                continue
            allowed = ("def media_publish" in line or line.strip().startswith("#")
                       or line.strip().startswith("*") or "NEVER" in line
                       or "media_publish``" in line or "/media_publish" in line)
            if not allowed:
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == [], offenders


# ---------------------------------------------------------------- disabled mode
def test_disabled_instagram_makes_zero_api_calls(settings, db, sample_video, metadata_text,
                                                 monkeypatch):
    settings.instagram_enabled = False
    calls = []
    monkeypatch.setattr(httpx.Client, "request",
                        lambda self, *a, **k: calls.append(a) or (_ for _ in ()).throw(
                            AssertionError("no HTTP request expected")))
    video = settings.input_dir / "noig.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "noig.txt").write_text(metadata_text, encoding="utf-8")
    job = Pipeline(settings, db).run(db.create_job(
        base_name="noig", video_path=str(video),
        metadata_path=str(settings.input_dir / "noig.txt"), dry_run=True))
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    assert calls == []
    assert "instagram" not in job["platforms"]
    out = Path(job["output_dir"])
    assert not (out / "instagram.json").exists()
    assert not (out / "caption_instagram.txt").exists()


def test_disabled_status_reports_disabled(settings):
    settings.instagram_enabled = False
    status = InstagramService(settings).status()
    assert status["status"] == PlatformStatus.DISABLED.value
    assert status["auto_publish"] is False


# ---------------------------------------------------------------- caption
def test_instagram_caption_uses_title_description_hashtags(metadata_text):
    from app.content.analyzer import analyze
    from app.parsing.text_parser import parse_text
    plan = analyze(parse_text(metadata_text))
    caption = build_instagram_caption(plan)
    assert plan.tiktok_title.split(",")[0][:20] in caption
    assert caption.rstrip().endswith("#" + plan.hashtags[-1])
    assert len(caption) <= 2200


def test_instagram_caption_respects_limit():
    class Plan:
        tiktok_title = "T" * 100
        caption = "x" * 5000
        hashtags = ["a", "b"]
    caption = build_instagram_caption(Plan(), max_chars=300)
    assert len(caption) <= 300 and "#a #b" in caption


# ---------------------------------------------------------------- reels validation
def test_reels_validation_accepts_the_pipeline_output(settings, sample_video):
    check = validate_reel(sample_video, settings)
    assert check.ok, check.errors


def test_reels_validation_rejects_bad_media(settings, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"nonsense" * 1000)
    assert not validate_reel(bad, settings).ok


def test_reels_validation_flags_short_clip(settings, landscape_video):
    check = validate_reel(landscape_video, settings)   # 2 s, 1920x1080, no audio
    assert not check.ok
    assert any("3 s minimum" in e for e in check.errors)


# ---------------------------------------------------------------- pipeline integration
def test_pipeline_stages_instagram_after_tiktok(ig_settings, db, fake, sample_video,
                                                metadata_text):
    fake.configure(ig_settings)
    ig_settings.dry_run = False
    ig_settings.tiktok_mock = True
    _authenticate(ig_settings, InstagramOAuth(ig_settings))

    video = ig_settings.input_dir / "both.mp4"
    video.write_bytes(sample_video.read_bytes())
    (ig_settings.input_dir / "both.txt").write_text(metadata_text, encoding="utf-8")

    watcher = WatcherService(ig_settings, db, Pipeline(ig_settings, db))
    job = db.get_job(watcher.scan_once()[0])
    assert job["state"] == JobState.COMPLETED.value, job.get("error")

    platforms = job["platforms"]
    assert platforms["tiktok"]["status"] == PlatformStatus.READY_TO_PUBLISH.value
    assert platforms["tiktok"]["external_id"].startswith("mock.")
    assert platforms["instagram"]["status"] == PlatformStatus.READY_TO_PUBLISH.value
    assert platforms["instagram"]["external_id"].startswith("1789")
    assert platforms["instagram"]["error"] is None

    events = [e["event"] for e in db.events(job["id"])]
    for expected in ("TIKTOK_DRAFT_READY", "INSTAGRAM_UPLOAD_STARTED",
                     "INSTAGRAM_READY_TO_PUBLISH"):
        assert expected in events

    out = Path(job["output_dir"])
    assert (out / "caption_instagram.txt").is_file()
    ig_json = json.loads((out / "instagram.json").read_text(encoding="utf-8"))
    assert ig_json["status"] == "READY_TO_PUBLISH" and ig_json["auto_publish"] is False
    status_json = json.loads((out / "instagram_status.json").read_text(encoding="utf-8"))
    assert status_json["container_id"].startswith("1789")
    assert "does not provide the same visible draft/inbox workflow" in status_json["note"]
    assert not list(out.glob("*.png"))
    assert fake.state.publish_calls == []


def test_instagram_failure_does_not_damage_tiktok(ig_settings, db, fake, sample_video,
                                                  metadata_text):
    fake.configure(ig_settings)
    fake.state.fail_container_creation = True
    ig_settings.dry_run = False
    ig_settings.tiktok_mock = True
    _authenticate(ig_settings, InstagramOAuth(ig_settings))

    video = ig_settings.input_dir / "igfail.mp4"
    video.write_bytes(sample_video.read_bytes())
    (ig_settings.input_dir / "igfail.txt").write_text(metadata_text, encoding="utf-8")

    job = Pipeline(ig_settings, db).run(db.create_job(
        base_name="igfail", video_path=str(video),
        metadata_path=str(ig_settings.input_dir / "igfail.txt"), dry_run=False))

    assert job["state"] == JobState.COMPLETED.value            # TikTok result preserved
    assert job["publish_id"].startswith("mock.")
    assert job["platforms"]["tiktok"]["status"] == PlatformStatus.READY_TO_PUBLISH.value
    assert job["platforms"]["instagram"]["status"] == PlatformStatus.FAILED.value
    assert job["platforms"]["instagram"]["error"]
    assert job["upload_result"]["publish_id"].startswith("mock.")


def test_tiktok_failure_keeps_platform_records_separate(ig_settings, db, sample_video,
                                                        metadata_text, monkeypatch):
    ig_settings.dry_run = False
    ig_settings.tiktok_mock = True
    from app.tiktok.client import MockTikTokClient, TikTokAPIError

    def boom(self, video_path, *, progress=None):
        raise TikTokAPIError("simulated TikTok outage", retryable=False)

    monkeypatch.setattr(MockTikTokClient, "upload_draft", boom)
    video = ig_settings.input_dir / "ttfail.mp4"
    video.write_bytes(sample_video.read_bytes())
    (ig_settings.input_dir / "ttfail.txt").write_text(metadata_text, encoding="utf-8")

    job = Pipeline(ig_settings, db).run(db.create_job(
        base_name="ttfail", video_path=str(video),
        metadata_path=str(ig_settings.input_dir / "ttfail.txt"), dry_run=False))
    assert job["state"] == JobState.FAILED.value
    assert job["platforms"]["tiktok"]["status"] == PlatformStatus.FAILED.value
    assert "instagram" not in job["platforms"]      # not touched, not corrupted


def test_dry_run_skips_instagram_upload(ig_settings, db, fake, sample_video, metadata_text):
    fake.configure(ig_settings)
    video = ig_settings.input_dir / "igdry.mp4"
    video.write_bytes(sample_video.read_bytes())
    (ig_settings.input_dir / "igdry.txt").write_text(metadata_text, encoding="utf-8")
    job = Pipeline(ig_settings, db).run(db.create_job(
        base_name="igdry", video_path=str(video),
        metadata_path=str(ig_settings.input_dir / "igdry.txt"), dry_run=True))
    assert job["state"] == JobState.COMPLETED.value
    assert job["platforms"]["instagram"]["status"] == PlatformStatus.SKIPPED.value
    assert fake.state.requests == []                # no Instagram call at all
    assert (Path(job["output_dir"]) / "caption_instagram.txt").is_file()
