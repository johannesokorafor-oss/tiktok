"""Tests for the optional platforms: Vimeo, Dailymotion, SoundCloud, Patreon, Rumble.

All offline, against local fake servers. Invariants asserted everywhere:
nothing is ever made public, Patreon v1 is never called, and Rumble never
performs an HTTP upload.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest

from app.config import PlatformMode, PlatformStatus, Settings
from app.jobs.pipeline import Pipeline
from app.jobs.states import JobState
from app.platforms.base import (PlatformAuthError, PlatformError, PlatformProvider,
                                PublishingNotAllowed)
from app.platforms.dailymotion import DailymotionProvider
from app.platforms.patreon import POST_CREATION_SUPPORTED, V1_FORBIDDEN, PatreonProvider
from app.platforms.registry import PROVIDER_CLASSES, all_providers, enabled_providers
from app.platforms.rumble import OFFICIAL_VOD_UPLOAD_API, RumbleProvider
from app.platforms.soundcloud import UNVERIFIED_NOTE, SoundCloudProvider, extract_audio
from app.platforms.vimeo import VimeoProvider
from tests.fake_platforms import FakePlatformServer

REPO = Path(__file__).resolve().parent.parent


class _Plan:
    tiktok_title = "Die Zeichen, die deine Seele dir zeigen will"
    caption = "Eine mystische Betrachtung über Intuition."
    hashtags = ["seele", "intuition"]


@pytest.fixture()
def fake():
    with FakePlatformServer() as server:
        yield server


@pytest.fixture()
def plat_settings(settings, fake):
    fake.configure(settings)
    settings.vimeo_backoff_base_seconds = 0.0
    settings.dailymotion_backoff_base_seconds = 0.0
    settings.soundcloud_backoff_base_seconds = 0.0
    return settings


# ================================================================ defaults
@pytest.mark.parametrize("platform,mode", [
    ("vimeo", "UPLOAD_PRIVATE"), ("dailymotion", "UPLOAD_PRIVATE"),
    ("soundcloud", "UPLOAD_PRIVATE"), ("patreon", "PREPARE_ONLY"), ("rumble", "PREPARE_ONLY")])
def test_every_new_platform_is_disabled_by_default(platform, mode):
    s = Settings(_env_file=None)
    assert getattr(s, f"{platform}_enabled") is False
    assert getattr(s, f"{platform}_mode").value == mode


def test_disabled_platforms_are_not_run(settings):
    assert enabled_providers(settings) == []
    assert {p.name for p in all_providers(settings)} == set(PROVIDER_CLASSES)


def test_credentials_alone_never_enable_a_platform(settings):
    settings.vimeo_access_token = "token"
    settings.dailymotion_client_id = "id"
    settings.dailymotion_client_secret = "secret"
    settings.soundcloud_client_id = "id"
    assert enabled_providers(settings) == []


# ================================================================ Vimeo
def test_vimeo_tus_upload_is_private(plat_settings, sample_video, tmp_path):
    plat_settings.vimeo_enabled = True
    plat_settings.vimeo_access_token = "vimeo-token"
    provider = VimeoProvider(plat_settings)
    result = provider.upload(sample_video, _Plan(), tmp_path)

    assert result.status == PlatformStatus.UPLOADED_PRIVATE, result.error
    assert result.mode == PlatformMode.UPLOAD_PRIVATE
    assert result.external_id.startswith("/videos/")
    create = next(r for r in plat_settings and _requests(plat_settings) if False) if False else None
    body = next(r["body"] for r in _state(plat_settings).requests
                if r["method"] == "POST" and r["path"] == "/me/videos")
    assert body["upload"]["approach"] == "tus"
    assert int(body["upload"]["size"]) == sample_video.stat().st_size
    assert body["privacy"]["view"] == "nobody"
    # the whole file was transferred and verified via HEAD
    assert any(r["method"] == "PATCH" for r in _state(plat_settings).requests)
    assert any(r["method"] == "HEAD" for r in _state(plat_settings).requests)
    assert (tmp_path / "vimeo.json").is_file()


def test_vimeo_resumes_after_an_interrupted_patch(plat_settings, sample_video, tmp_path,
                                                  monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    state = _state(plat_settings)
    state.vimeo_break_after = 50_000            # fail once mid-transfer
    plat_settings.vimeo_enabled = True
    plat_settings.vimeo_access_token = "vimeo-token"
    plat_settings.vimeo_chunk_size = 200_000
    result = VimeoProvider(plat_settings).upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.UPLOADED_PRIVATE, result.error
    patches = [r for r in state.requests if r["method"] == "PATCH"]
    assert len(patches) >= 2                     # resumed at least once


def test_vimeo_refuses_public_privacy(plat_settings, sample_video, tmp_path):
    plat_settings.vimeo_enabled = True
    plat_settings.vimeo_access_token = "t"
    plat_settings.vimeo_privacy_view = "anybody"
    with pytest.raises(PublishingNotAllowed):
        VimeoProvider(plat_settings)._privacy_view()
    result = VimeoProvider(plat_settings).upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.FAILED
    assert "publicly viewable" in result.error


def test_vimeo_detects_a_non_private_result(plat_settings, sample_video, tmp_path):
    _state(plat_settings).vimeo_privacy_override = "anybody"
    plat_settings.vimeo_enabled = True
    plat_settings.vimeo_access_token = "t"
    result = VimeoProvider(plat_settings).upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.FAILED
    assert "not private" in result.error


def test_vimeo_missing_token_is_auth_error(settings, sample_video, tmp_path):
    settings.vimeo_enabled = True
    settings.vimeo_access_token = None
    result = VimeoProvider(settings).upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.AUTH_REQUIRED
    assert "VIMEO_ACCESS_TOKEN" in result.error


def test_vimeo_unauthorized_is_not_retried_forever(plat_settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _state(plat_settings).vimeo_unauthorized = True
    plat_settings.vimeo_enabled = True
    plat_settings.vimeo_access_token = "bad"
    provider = VimeoProvider(plat_settings)
    with pytest.raises(PlatformAuthError):
        provider.get_account()
    assert len([r for r in _state(plat_settings).requests if r["path"] == "/me"]) == 1


def test_vimeo_diagnosis_mentions_upload_access(settings):
    settings.vimeo_enabled = True
    diag = VimeoProvider(settings).diagnose().as_dict()
    assert diag["mode"] == "UPLOAD_PRIVATE"
    assert "upload access" in diag["detail"].lower()
    assert "upload, edit" in diag["required_access"]


# ================================================================ Dailymotion
def test_dailymotion_upload_is_private(plat_settings, sample_video, tmp_path):
    plat_settings.dailymotion_enabled = True
    plat_settings.dailymotion_client_id = "id"
    plat_settings.dailymotion_client_secret = "secret"
    plat_settings.dailymotion_profile_id = "x123"
    result = DailymotionProvider(plat_settings).upload(sample_video, _Plan(), tmp_path)

    assert result.status == PlatformStatus.UPLOADED_PRIVATE, result.error
    state = _state(plat_settings)
    paths = state.paths()
    assert "/oauth/token" in paths
    assert "/v2/files/upload_sessions" in paths        # v2 only
    assert "/upload" in paths
    create = next(r for r in state.requests if "/v2/profiles/" in r["path"])
    assert create["body"]["visibility"] == "private"
    assert create["body"]["source"]["file_url"].endswith(".mp4")
    assert create["body"]["is_for_kids"] is False
    token_body = next(r["body"] for r in state.requests if r["path"] == "/oauth/token")
    assert token_body["scope"] == "video.manage"
    assert (tmp_path / "dailymotion.json").is_file()


def test_dailymotion_refuses_public_visibility(plat_settings, sample_video, tmp_path):
    plat_settings.dailymotion_enabled = True
    plat_settings.dailymotion_client_id = "id"
    plat_settings.dailymotion_client_secret = "s"
    plat_settings.dailymotion_profile_id = "x1"
    plat_settings.dailymotion_visibility = "public"
    result = DailymotionProvider(plat_settings).upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.FAILED
    assert "never publishes publicly" in result.error
    assert not any("/v2/profiles/" in p for p in _state(plat_settings).paths())


def test_dailymotion_detects_public_result(plat_settings, sample_video, tmp_path):
    _state(plat_settings).dm_visibility_override = "public"
    plat_settings.dailymotion_enabled = True
    plat_settings.dailymotion_client_id = "id"
    plat_settings.dailymotion_client_secret = "s"
    plat_settings.dailymotion_profile_id = "x1"
    result = DailymotionProvider(plat_settings).upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.FAILED
    assert "public" in result.error


def test_dailymotion_token_failure_is_auth_error(plat_settings, sample_video, tmp_path):
    _state(plat_settings).dm_token_fail = True
    plat_settings.dailymotion_enabled = True
    plat_settings.dailymotion_client_id = "id"
    plat_settings.dailymotion_client_secret = "s"
    plat_settings.dailymotion_profile_id = "x1"
    result = DailymotionProvider(plat_settings).upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.AUTH_REQUIRED


def test_dailymotion_uses_api_v2_only(plat_settings, sample_video, tmp_path):
    plat_settings.dailymotion_enabled = True
    plat_settings.dailymotion_client_id = "id"
    plat_settings.dailymotion_client_secret = "s"
    plat_settings.dailymotion_profile_id = "x1"
    DailymotionProvider(plat_settings).upload(sample_video, _Plan(), tmp_path)
    for path in _state(plat_settings).paths():
        assert "/file/upload" not in path          # legacy API
        assert "/me/videos" not in path            # legacy API


# ================================================================ SoundCloud
def test_soundcloud_downgrades_when_the_private_field_is_unverified(settings):
    """The safety gate still works if the flag is switched off again."""
    settings.soundcloud_enabled = True
    settings.soundcloud_private_field_verified = False
    provider = SoundCloudProvider(settings)
    assert provider.mode == PlatformMode.UPLOAD_PRIVATE
    assert provider.effective_mode == PlatformMode.PREPARE_ONLY
    assert "not currently verified" in provider.diagnose().detail


def test_soundcloud_private_upload_is_verified_by_default(settings):
    """Re-verified 2026-09-27 against the current OpenAPI spec (TrackDataRequest)."""
    from app.config import Settings as _S
    assert _S(_env_file=None).soundcloud_private_field_verified is True
    settings.soundcloud_enabled = True
    assert SoundCloudProvider(settings).effective_mode == PlatformMode.UPLOAD_PRIVATE


def test_soundcloud_prepare_extracts_audio(plat_settings, sample_video, tmp_path):
    plat_settings.soundcloud_enabled = True
    plat_settings.soundcloud_private_field_verified = False      # force PREPARE_ONLY
    result = SoundCloudProvider(plat_settings).prepare(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.READY_FOR_MANUAL_UPLOAD, result.error
    audio = tmp_path / "soundcloud_ready.flac"
    assert audio.is_file() and audio.stat().st_size > 1000
    assert (tmp_path / "caption_soundcloud.txt").is_file()
    payload = json.loads((tmp_path / "soundcloud.json").read_text(encoding="utf-8"))
    assert payload["api_calls_made"] == 0
    assert UNVERIFIED_NOTE in payload["reason"]
    assert result.validation["info"]["audio_codec"] == "flac"
    assert _state(plat_settings).requests == []        # no API call at all
    assert not list(tmp_path.glob("*.png"))            # no artwork generated


def test_soundcloud_upload_refuses_when_unverified(plat_settings, sample_video, tmp_path):
    plat_settings.soundcloud_enabled = True
    plat_settings.soundcloud_private_field_verified = False
    plat_settings.soundcloud_client_id = "id"
    plat_settings.soundcloud_client_secret = "s"
    result = SoundCloudProvider(plat_settings).upload(sample_video, _Plan(), tmp_path)
    assert result.mode == PlatformMode.PREPARE_ONLY
    assert result.message == UNVERIFIED_NOTE
    assert not any(p == "/tracks" for p in _state(plat_settings).paths())


def test_soundcloud_upload_private_when_verified(plat_settings, sample_video, tmp_path):
    plat_settings.soundcloud_enabled = True
    plat_settings.soundcloud_client_id = "id"
    plat_settings.soundcloud_client_secret = "s"
    plat_settings.soundcloud_private_field_verified = True
    provider = SoundCloudProvider(plat_settings)
    provider.save_tokens(type(provider.load_tokens())(
        access_token="sc-access", refresh_token="sc-refresh",
        expires_at=time.time() + 3600))
    result = provider.upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.UPLOADED_PRIVATE, result.error
    upload = next(r for r in _state(plat_settings).requests if r["path"] == "/tracks")
    assert upload["body"]["track[sharing]"] == "private"
    assert "track[asset_data]" in upload["body"]
    assert upload["headers"].get("Authorization", "").startswith("OAuth ")


def test_soundcloud_pkce_and_refresh(plat_settings):
    plat_settings.soundcloud_client_id = "id"
    plat_settings.soundcloud_client_secret = "s"
    provider = SoundCloudProvider(plat_settings)
    verifier, challenge = provider.pkce_pair()
    assert len(verifier) > 40 and challenge and verifier != challenge
    url = provider.authorize_url("state123", challenge)
    assert url.startswith(plat_settings.soundcloud_auth_base + "/authorize")
    for part in ("code_challenge=", "code_challenge_method=S256", "state=state123",
                 "response_type=code"):
        assert part in url

    tokens = provider.exchange_code("the-code", verifier)
    assert tokens.access_token == "sc-access"
    body = _state(plat_settings).sc_token_calls[-1]
    assert body["grant_type"] == "authorization_code" and body["code_verifier"] == verifier

    refreshed = provider.refresh(tokens)
    assert refreshed.refresh_token == "sc-refresh-new"     # single-use rotation stored
    assert _state(plat_settings).sc_token_calls[-1]["grant_type"] == "refresh_token"


def test_soundcloud_detects_public_result(plat_settings, sample_video, tmp_path):
    _state(plat_settings).sc_sharing_override = "public"
    plat_settings.soundcloud_enabled = True
    plat_settings.soundcloud_client_id = "id"
    plat_settings.soundcloud_client_secret = "s"
    plat_settings.soundcloud_private_field_verified = True
    provider = SoundCloudProvider(plat_settings)
    provider.save_tokens(type(provider.load_tokens())(access_token="a", refresh_token="r",
                                                      expires_at=time.time() + 3600))
    result = provider.upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.FAILED
    assert "public" in result.error


def test_soundcloud_audio_validation_rejects_video(settings, sample_video):
    check = SoundCloudProvider(settings).validate(sample_video)
    assert any("video stream" in w for w in check["warnings"])


def test_audio_extraction_is_lossless_flac(settings, sample_video, tmp_path):
    out = extract_audio(sample_video, tmp_path / "a.flac", settings)
    assert out.is_file()
    check = SoundCloudProvider(settings).validate(out)
    assert check["ok"], check["errors"]
    assert check["info"]["audio_codec"] == "flac"


# ================================================================ Patreon
def test_patreon_is_prepare_only_and_never_uses_v1(plat_settings, sample_video, tmp_path):
    plat_settings.patreon_enabled = True
    provider = PatreonProvider(plat_settings)
    assert POST_CREATION_SUPPORTED is False
    assert provider.effective_mode == PlatformMode.PREPARE_ONLY

    result = provider.prepare(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.READY_FOR_MANUAL_UPLOAD, result.error
    assert (tmp_path / "patreon_post.txt").is_file()
    assert (tmp_path / "patreon_video.mp4").is_file()
    meta = json.loads((tmp_path / "patreon_metadata.json").read_text(encoding="utf-8"))
    assert meta["api_version"] == "v2" and meta["api_v1_used"] is False
    assert meta["post_creation_api"] == "NOT DOCUMENTED IN CURRENT V2"
    assert meta["api_calls_made"] == 0
    assert _state(plat_settings).patreon_v1_calls == []
    assert _state(plat_settings).requests == []


def test_patreon_refuses_v1_urls(settings):
    provider = PatreonProvider(settings)
    for fragment in V1_FORBIDDEN:
        with pytest.raises(PlatformError):
            provider._assert_v2(f"https://www.patreon.com{fragment}current_user")
    provider._assert_v2("https://www.patreon.com/api/oauth2/v2/identity")   # fine


def test_patreon_identity_uses_v2(plat_settings, monkeypatch):
    monkeypatch.setenv("PATREON_ACCESS_TOKEN", "creator-token")
    plat_settings.patreon_enabled = True
    data = PatreonProvider(plat_settings).identity()
    assert data["data"]["attributes"]["full_name"] == "Test Creator"
    assert _state(plat_settings).paths() == ["/identity"]
    assert _state(plat_settings).patreon_v1_calls == []


def test_patreon_upload_is_not_available(settings, sample_video, tmp_path):
    with pytest.raises(PlatformError):
        PatreonProvider(settings).upload(sample_video, _Plan(), tmp_path)


def test_patreon_diagnosis_reports_capability(settings):
    settings.patreon_enabled = True
    diag = PatreonProvider(settings).diagnose().as_dict()
    assert diag["mode"] == "PREPARE_ONLY"
    assert "NOT DOCUMENTED" in diag["capability"]
    assert "v1 retires" in diag["required_access"]


# ================================================================ Rumble
def test_rumble_is_prepare_only_with_no_http(plat_settings, sample_video, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Rumble must not make any HTTP request")
    monkeypatch.setattr(httpx.Client, "request", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)

    plat_settings.rumble_enabled = True
    provider = RumbleProvider(plat_settings)
    assert OFFICIAL_VOD_UPLOAD_API is False
    assert provider.effective_mode == PlatformMode.PREPARE_ONLY

    result = provider.prepare(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.READY_FOR_MANUAL_UPLOAD, result.error
    assert (tmp_path / "rumble_ready.mp4").is_file()
    assert (tmp_path / "rumble_metadata.txt").is_file()
    meta = json.loads((tmp_path / "rumble_metadata.json").read_text(encoding="utf-8"))
    assert meta["official_vod_upload_api"] == "NOT VERIFIED"
    assert meta["api_calls_made"] == 0
    assert "not currently verified as an official public API" in meta["note"]


def test_rumble_upload_is_not_available(settings, sample_video, tmp_path):
    with pytest.raises(PlatformError):
        RumbleProvider(settings).upload(sample_video, _Plan(), tmp_path)


def test_rumble_mode_cannot_be_forced_to_upload(settings):
    settings.rumble_enabled = True
    settings.rumble_mode = PlatformMode.UPLOAD_PRIVATE      # user tries to force it
    assert RumbleProvider(settings).effective_mode == PlatformMode.PREPARE_ONLY


# ================================================================ pipeline
def test_pipeline_runs_enabled_platforms_and_isolates_failures(plat_settings, db, sample_video,
                                                               metadata_text):
    plat_settings.dry_run = False
    plat_settings.tiktok_mock = True
    plat_settings.vimeo_enabled = True
    plat_settings.vimeo_access_token = "t"
    plat_settings.rumble_enabled = True
    plat_settings.dailymotion_enabled = True          # misconfigured on purpose
    plat_settings.dailymotion_client_id = None

    video = plat_settings.input_dir / "multi.mp4"
    video.write_bytes(sample_video.read_bytes())
    (plat_settings.input_dir / "multi.txt").write_text(metadata_text, encoding="utf-8")

    job = Pipeline(plat_settings, db).run(db.create_job(
        base_name="multi", video_path=str(video),
        metadata_path=str(plat_settings.input_dir / "multi.txt"), dry_run=False))

    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    platforms = job["platforms"]
    assert platforms["tiktok"]["status"] == PlatformStatus.READY_TO_PUBLISH.value
    assert platforms["vimeo"]["status"] == PlatformStatus.UPLOADED_PRIVATE.value
    assert platforms["vimeo"]["mode"] == "UPLOAD_PRIVATE"
    assert platforms["vimeo"]["url"]
    assert platforms["rumble"]["status"] == PlatformStatus.READY_FOR_MANUAL_UPLOAD.value
    assert platforms["dailymotion"]["status"] in {PlatformStatus.FAILED.value,
                                                  PlatformStatus.NOT_CONFIGURED.value}
    # one platform failing changed nothing for the others
    assert platforms["tiktok"]["error"] is None
    assert platforms["vimeo"]["error"] is None
    out = Path(job["output_dir"])
    assert (out / "vimeo.json").is_file() and (out / "rumble_ready.mp4").is_file()
    assert not list(out.glob("*.png"))


def test_dry_run_skips_api_uploads_but_prepares_local(plat_settings, db, sample_video,
                                                      metadata_text):
    plat_settings.vimeo_enabled = True
    plat_settings.vimeo_access_token = "t"
    plat_settings.patreon_enabled = True
    video = plat_settings.input_dir / "dryplat.mp4"
    video.write_bytes(sample_video.read_bytes())
    (plat_settings.input_dir / "dryplat.txt").write_text(metadata_text, encoding="utf-8")

    job = Pipeline(plat_settings, db).run(db.create_job(
        base_name="dryplat", video_path=str(video),
        metadata_path=str(plat_settings.input_dir / "dryplat.txt"), dry_run=True))
    platforms = job["platforms"]
    assert platforms["vimeo"]["status"] == PlatformStatus.SKIPPED.value
    assert platforms["patreon"]["status"] == PlatformStatus.READY_FOR_MANUAL_UPLOAD.value
    assert not any(r["path"] == "/me/videos" for r in _state(plat_settings).requests)


def test_disabled_platforms_touch_nothing(settings, db, sample_video, metadata_text):
    video = settings.input_dir / "none.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "none.txt").write_text(metadata_text, encoding="utf-8")
    job = Pipeline(settings, db).run(db.create_job(
        base_name="none", video_path=str(video),
        metadata_path=str(settings.input_dir / "none.txt"), dry_run=True))
    assert set(job["platforms"]) == {"tiktok"}
    out = Path(job["output_dir"])
    for name in ("vimeo.json", "dailymotion.json", "soundcloud.json", "patreon_post.txt",
                 "rumble_ready.mp4"):
        assert not (out / name).exists()


# ================================================================ no-fabrication
def test_no_publish_function_exists_anywhere():
    offenders = []
    for path in (REPO / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"def\s+publish(_all)?\s*\(", stripped):
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


def test_no_browser_automation_or_scraping_dependency():
    banned = ("selenium", "playwright", "puppeteer", "undetected_chromedriver",
              "BeautifulSoup", "mechanize")
    hits = []
    for path in (REPO / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        hits += [f"{path.name}: {b}" for b in banned if b.lower() in text.lower()]
    requirements = (REPO / "requirements.txt").read_text(encoding="utf-8").lower()
    hits += [f"requirements.txt: {b}" for b in banned if b.lower() in requirements]
    assert hits == []


def test_no_patreon_v1_endpoint_in_source():
    hits = []
    for path in (REPO / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "oauth2/api" in line and "V1_FORBIDDEN" not in line and not line.strip().startswith(("#", "*", '"')):
                if "forbidden" not in line.lower() and "retired" not in line.lower():
                    hits.append(f"{path.name}: {line.strip()}")
    assert hits == []


def test_no_unknown_rumble_endpoints():
    text = (REPO / "app" / "platforms" / "rumble.py").read_text(encoding="utf-8")
    assert "httpx" not in text                    # the provider makes no HTTP calls at all
    for banned in ("rumble.com/api", "upload.php\"", "api/v0"):
        assert banned not in text


def test_defaults_are_never_public():
    s = Settings(_env_file=None)
    assert s.vimeo_privacy_view == "nobody"
    assert s.dailymotion_visibility == "private"
    assert s.soundcloud_private_value == "private"
    assert s.instagram_auto_publish is False
    assert s.tiktok_post_mode == "UPLOAD"
    assert s.confirm_direct_post is False


def test_soundcloud_private_field_is_not_used_unless_verified(settings):
    settings.soundcloud_enabled = True
    settings.soundcloud_private_field_verified = False
    provider = SoundCloudProvider(settings)
    assert provider.effective_mode == PlatformMode.PREPARE_ONLY
    source = (REPO / "app" / "platforms" / "soundcloud.py").read_text(encoding="utf-8")
    # the field is only ever read from settings, never hard-coded into a request
    assert 'data = {' in source and 'self.settings.soundcloud_private_field' in source
    assert '"track[sharing]": "private"' not in source


def test_tiktok_and_instagram_are_untouched_by_the_new_platforms(settings):
    from app.instagram.service import InstagramService
    assert settings.tiktok_post_mode == "UPLOAD"
    assert settings.effective_mode.value == "DRY_RUN"
    assert InstagramService(settings).mode.value == "PREPARE_ONLY"
    for provider in all_providers(settings):
        assert provider.name not in {"tiktok", "instagram"}


def test_no_thumbnail_generation_returned():
    for module in ("app.images", "app.cover"):
        with pytest.raises(ModuleNotFoundError):
            __import__(module)
    for path in (REPO / "app" / "platforms").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from PIL" not in text and "import PIL" not in text


# ================================================================ helpers
def _state(settings):
    from tests import fake_platforms
    return fake_platforms.STATE


def _requests(settings):
    return _state(settings).requests
