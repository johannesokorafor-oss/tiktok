"""YouTube (real private upload) + Spotify / Apple Music (prepare only) + SoundCloud.

All offline against the local fake server. Core invariants: YouTube never
uploads anything but `privacyStatus=private`, Spotify/Apple Music never call a
release API, and nothing generates artwork.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from app.config import PlatformMode, PlatformStatus, Settings
from app.jobs.pipeline import Pipeline
from app.jobs.states import JobState
from app.platforms.apple_music import AppleMusicProvider
from app.platforms.audio import MASTER_NAME, build_audio_master
from app.platforms.base import PlatformAuthError, PlatformError, PublishingNotAllowed
from app.platforms.registry import PROVIDER_CLASSES, all_providers
from app.platforms.soundcloud import UNVERIFIED_NOTE, VERIFIED_NOTE, SoundCloudProvider
from app.platforms.spotify import SpotifyProvider
from app.platforms.youtube import UPLOAD_SCOPE, YouTubeProvider, YouTubeTokens
from tests.fake_platforms import FakePlatformServer

REPO = Path(__file__).resolve().parent.parent


class _Plan:
    tiktok_title = "Die Zeichen, die deine Seele dir zeigen will"
    caption = "Eine mystische Betrachtung über Intuition."
    hashtags = ["seele", "intuition"]
    language = "de"
    topic = "spiritual"


@pytest.fixture()
def fake():
    with FakePlatformServer() as server:
        yield server


@pytest.fixture()
def yt_settings(settings, fake):
    fake.configure(settings)
    settings.youtube_enabled = True
    settings.youtube_client_id = "gid"
    settings.youtube_client_secret = "gsecret"
    settings.youtube_backoff_base_seconds = 0.0
    settings.youtube_chunk_size = 256 * 1024
    return settings


def _state():
    from tests import fake_platforms
    return fake_platforms.STATE


def _authed(provider: YouTubeProvider) -> None:
    provider.save_tokens(YouTubeTokens(access_token="yt-access", refresh_token="yt-refresh",
                                       scope=UPLOAD_SCOPE, expires_at=time.time() + 3600))


# ================================================================ defaults
@pytest.mark.parametrize("platform,mode", [
    ("youtube", "UPLOAD_PRIVATE"), ("spotify", "PREPARE_ONLY"), ("apple_music", "PREPARE_ONLY")])
def test_new_platforms_disabled_by_default(platform, mode):
    s = Settings(_env_file=None)
    assert getattr(s, f"{platform}_enabled") is False
    assert getattr(s, f"{platform}_mode").value == mode


def test_registry_contains_all_eight_optional_platforms(settings):
    assert set(PROVIDER_CLASSES) == {"youtube", "vimeo", "dailymotion", "soundcloud",
                                     "spotify", "apple_music", "patreon", "rumble"}
    assert all(not p.enabled for p in all_providers(settings))


# ================================================================ YouTube OAuth
def test_youtube_authorize_url_uses_documented_scope(yt_settings):
    url = YouTubeProvider(yt_settings).authorize_url("state-1")
    assert "/o/oauth2/v2/auth" in url
    assert "client_id=gid" in url and "response_type=code" in url
    assert "youtube.upload" in url
    assert "access_type=offline" in url and "state=state-1" in url


def test_youtube_code_exchange_and_refresh(yt_settings):
    provider = YouTubeProvider(yt_settings)
    tokens = provider.exchange_code("the-code")
    assert tokens.access_token == "yt-access" and tokens.refresh_token == "yt-refresh"
    body = _state().yt_token_calls[-1]
    assert body["grant_type"] == "authorization_code" and body["client_secret"] == "gsecret"

    tokens.expires_at = time.time() - 10           # force a refresh
    provider.save_tokens(tokens)
    refreshed = provider.refresh()
    assert refreshed.access_token == "yt-access-refreshed"
    assert _state().yt_token_calls[-1]["grant_type"] == "refresh_token"


def test_youtube_without_tokens_is_auth_error(yt_settings, sample_video, tmp_path):
    result = YouTubeProvider(yt_settings).upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.AUTH_REQUIRED
    assert "not connected" in result.error


def test_youtube_unauthorized_is_not_retried(yt_settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _state().yt_unauthorized = True
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    with pytest.raises(PlatformAuthError):
        provider.get_account()
    calls = [r for r in _state().requests if r["path"] == "/youtube/v3/channels"]
    assert len(calls) == 1


# ================================================================ YouTube upload
def test_youtube_resumable_upload_is_private(yt_settings, sample_video, tmp_path):
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    result = provider.upload(sample_video, _Plan(), tmp_path)

    assert result.status == PlatformStatus.UPLOADED_PRIVATE, result.error
    assert result.mode == PlatformMode.UPLOAD_PRIVATE
    assert result.external_id.startswith("YT")
    assert result.url.startswith("https://www.youtube.com/watch?v=")

    start = next(r for r in _state().requests
                 if r["path"] == "/upload/youtube/v3/videos" and r["method"] == "POST")
    assert start["body"]["status"]["privacyStatus"] == "private"
    assert start["body"]["status"]["selfDeclaredMadeForKids"] is False
    assert start["body"]["snippet"]["title"] == _Plan.tiktok_title[:100]
    assert start["body"]["snippet"]["description"] == _Plan.caption
    assert start["body"]["snippet"]["tags"] == ["seele", "intuition"]
    assert start["body"]["snippet"]["defaultLanguage"] == "de"
    headers = {k.lower(): v for k, v in start["headers"].items()}
    assert headers["x-upload-content-length"] == str(sample_video.stat().st_size)
    assert headers["x-upload-content-type"] == "video/mp4"
    puts = [r for r in _state().requests if r["method"] == "PUT"]
    assert puts and all("bytes" in (r["body"]["range"] or "") for r in puts)
    assert (tmp_path / "youtube.json").is_file()


def test_youtube_chunks_are_256kb_multiples(yt_settings, sample_video, tmp_path):
    yt_settings.youtube_chunk_size = 300_000          # not a multiple
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    provider.upload(sample_video, _Plan(), tmp_path)
    puts = [r for r in _state().requests if r["method"] == "PUT" and r["body"]["bytes"]]
    intermediate = puts[:-1]
    assert all(r["body"]["bytes"] % (256 * 1024) == 0 for r in intermediate), \
        [r["body"]["bytes"] for r in intermediate]


def test_youtube_resumes_after_interruption(yt_settings, sample_video, tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _state().yt_break_after = 300_000
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    result = provider.upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.UPLOADED_PRIVATE, result.error
    assert any("bytes */" in (r["body"].get("range") or "")
               for r in _state().requests if r["method"] == "PUT")


def test_youtube_retries_transient_5xx(yt_settings, sample_video, tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _state().yt_fail_times = 1
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    result = provider.upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.UPLOADED_PRIVATE, result.error


def test_youtube_refuses_public_privacy_setting(yt_settings, sample_video, tmp_path):
    yt_settings.youtube_privacy_status = "public"
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    with pytest.raises(PublishingNotAllowed):
        provider.build_body(_Plan())
    result = provider.upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.FAILED
    assert "only uploads private videos" in result.error
    assert not any(r["path"] == "/upload/youtube/v3/videos" for r in _state().requests)


def test_youtube_refuses_unlisted_too(yt_settings):
    yt_settings.youtube_privacy_status = "unlisted"
    with pytest.raises(PublishingNotAllowed):
        YouTubeProvider(yt_settings).build_body(_Plan())


def test_youtube_detects_non_private_result(yt_settings, sample_video, tmp_path):
    _state().yt_privacy_override = "public"
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    result = provider.upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.FAILED
    assert "not private" in result.error


def test_youtube_never_uploads_a_thumbnail(yt_settings, sample_video, tmp_path):
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    provider.upload(sample_video, _Plan(), tmp_path)
    assert not any("thumbnail" in r["path"].lower() for r in _state().requests)
    assert not list(tmp_path.glob("*.png")) and not list(tmp_path.glob("*.jpg"))
    source = (REPO / "app" / "platforms" / "youtube.py").read_text(encoding="utf-8")
    assert "thumbnails/set" not in source and "set_thumbnail" not in source


def test_youtube_disabled_makes_no_api_call(settings, db, sample_video, metadata_text,
                                            monkeypatch):
    assert settings.youtube_enabled is False
    calls = []
    monkeypatch.setattr(httpx.Client, "request",
                        lambda self, *a, **k: calls.append(a) or (_ for _ in ()).throw(
                            AssertionError("no HTTP expected")))
    video = settings.input_dir / "noyt.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "noyt.txt").write_text(metadata_text, encoding="utf-8")
    job = Pipeline(settings, db).run(db.create_job(
        base_name="noyt", video_path=str(video),
        metadata_path=str(settings.input_dir / "noyt.txt"), dry_run=True))
    assert job["state"] == JobState.COMPLETED.value
    assert "youtube" not in job["platforms"] and calls == []


def test_youtube_diagnosis_reports_audit_state(yt_settings):
    diag = YouTubeProvider(yt_settings).diagnose().as_dict()
    assert diag["mode"] == "UPLOAD_PRIVATE"
    assert "API project: unknown" in diag["detail"]
    assert "Automatic public publishing: DISABLED" in diag["detail"]
    assert "youtube.upload" in diag["required_access"]


def test_youtube_status_check(yt_settings, sample_video, tmp_path):
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    uploaded = provider.upload(sample_video, _Plan(), tmp_path)
    status = provider.get_status(uploaded.external_id)
    assert status.status == PlatformStatus.UPLOADED_PRIVATE
    assert status.metadata["status"]["privacyStatus"] == "private"


# ================================================================ audio master
def test_audio_master_is_built_once_and_reused(settings, sample_video, tmp_path):
    first = build_audio_master(sample_video, tmp_path, settings)
    assert first.path.name == f"{MASTER_NAME}.flac" and first.reused is False
    assert first.codec == "flac" and first.sample_rate > 0 and first.duration > 0
    second = build_audio_master(sample_video, tmp_path, settings)
    assert second.reused is True and second.path == first.path


def test_music_platforms_share_the_same_master(settings, sample_video, tmp_path):
    settings.spotify_enabled = True
    settings.apple_music_enabled = True
    SpotifyProvider(settings).prepare(sample_video, _Plan(), tmp_path)
    mtime = (tmp_path / f"{MASTER_NAME}.flac").stat().st_mtime
    AppleMusicProvider(settings).prepare(sample_video, _Plan(), tmp_path)
    assert (tmp_path / f"{MASTER_NAME}.flac").stat().st_mtime == mtime      # not re-extracted


# ================================================================ Spotify
def test_spotify_is_prepare_only_with_no_api_calls(settings, fake, sample_video, tmp_path,
                                                   monkeypatch):
    fake.configure(settings)
    settings.spotify_enabled = True
    settings.spotify_artist = "Test Artist"

    def boom(*a, **k):
        raise AssertionError("Spotify must not make any HTTP request")
    monkeypatch.setattr(httpx.Client, "request", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)

    provider = SpotifyProvider(settings)
    assert provider.effective_mode == PlatformMode.PREPARE_ONLY
    result = provider.prepare(sample_video, _Plan(), tmp_path)

    assert result.status == PlatformStatus.READY_FOR_DISTRIBUTION, result.error
    package = tmp_path / "spotify"
    assert (package / "spotify_ready.flac").is_file()
    assert (package / "caption_spotify.txt").is_file()
    meta = json.loads((package / "spotify_metadata.json").read_text(encoding="utf-8"))
    assert meta["api_upload_supported"] is False and meta["api_calls_made"] == 0
    assert meta["artist"] == "Test Artist"
    assert meta["track_title"] == _Plan.tiktok_title
    assert meta["language"] == "de"
    assert "distributor" in meta["distribution"].lower()
    assert "artwork" in meta and "never generates cover images" in meta["artwork"]
    assert not list(package.glob("*.png")) and not list(package.glob("*.jpg"))
    assert _state().requests == []


def test_spotify_reports_missing_artist(settings, sample_video, tmp_path):
    settings.spotify_enabled = True
    settings.spotify_artist = None
    result = SpotifyProvider(settings).prepare(sample_video, _Plan(), tmp_path)
    assert "artist name" in " ".join(result.metadata["missing_fields"])
    diag = SpotifyProvider(settings).diagnose().as_dict()
    assert "SPOTIFY_ARTIST" in diag["detail"]


def test_spotify_has_no_upload_method(settings, sample_video, tmp_path):
    with pytest.raises(PlatformError):
        SpotifyProvider(settings).upload(sample_video, _Plan(), tmp_path)


# ================================================================ Apple Music
def test_apple_music_is_prepare_only_with_no_api_calls(settings, fake, sample_video, tmp_path,
                                                       monkeypatch):
    fake.configure(settings)
    settings.apple_music_enabled = True
    settings.apple_music_artist = "Test Artist"

    def boom(*a, **k):
        raise AssertionError("Apple Music must not make any HTTP request")
    monkeypatch.setattr(httpx.Client, "request", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)

    result = AppleMusicProvider(settings).prepare(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.READY_FOR_DISTRIBUTION, result.error
    package = tmp_path / "apple_music"
    assert (package / "apple_music_ready.flac").is_file()
    assert (package / "caption_apple_music.txt").is_file()
    meta = json.loads((package / "apple_music_metadata.json").read_text(encoding="utf-8"))
    assert meta["api_upload_supported"] is False and meta["api_calls_made"] == 0
    assert "distributor" in meta["distribution"].lower()
    assert not list(package.glob("*.png"))
    assert _state().requests == []


def test_apple_music_has_no_upload_method(settings, sample_video, tmp_path):
    with pytest.raises(PlatformError):
        AppleMusicProvider(settings).upload(sample_video, _Plan(), tmp_path)


# ================================================================ SoundCloud re-verification
def test_soundcloud_private_upload_is_now_verified():
    s = Settings(_env_file=None)
    assert s.soundcloud_private_field_verified is True
    assert s.soundcloud_private_field == "track[sharing]"
    assert s.soundcloud_private_value == "private"
    provider = SoundCloudProvider(s)
    assert provider.effective_mode == PlatformMode.UPLOAD_PRIVATE
    assert "TrackDataRequest" in VERIFIED_NOTE


def test_soundcloud_uploads_privately_with_the_verified_field(settings, fake, sample_video,
                                                              tmp_path):
    fake.configure(settings)
    settings.soundcloud_enabled = True
    settings.soundcloud_client_id = "id"
    settings.soundcloud_client_secret = "s"
    settings.soundcloud_backoff_base_seconds = 0.0
    provider = SoundCloudProvider(settings)
    from app.platforms.soundcloud import SoundCloudTokens
    provider.save_tokens(SoundCloudTokens(access_token="a", refresh_token="r",
                                          expires_at=time.time() + 3600))
    result = provider.upload(sample_video, _Plan(), tmp_path)
    assert result.status == PlatformStatus.UPLOADED_PRIVATE, result.error
    upload = next(r for r in _state().requests if r["path"] == "/tracks")
    assert upload["body"]["track[sharing]"] == "private"


def test_soundcloud_can_be_downgraded_again(settings, sample_video, tmp_path):
    settings.soundcloud_enabled = True
    settings.soundcloud_private_field_verified = False
    provider = SoundCloudProvider(settings)
    assert provider.effective_mode == PlatformMode.PREPARE_ONLY
    result = provider.upload(sample_video, _Plan(), tmp_path)
    assert result.message == UNVERIFIED_NOTE


def test_soundcloud_reuses_the_audio_master(settings, sample_video, tmp_path):
    settings.soundcloud_enabled = True
    SoundCloudProvider(settings).prepare(sample_video, _Plan(), tmp_path)
    assert (tmp_path / f"{MASTER_NAME}.flac").is_file()
    assert (tmp_path / "soundcloud_ready.flac").is_file()


# ================================================================ pipeline
def test_pipeline_runs_youtube_and_music_platforms_isolated(yt_settings, db, sample_video,
                                                            metadata_text):
    yt_settings.dry_run = False
    yt_settings.tiktok_mock = True
    yt_settings.spotify_enabled = True
    yt_settings.spotify_artist = "Test Artist"
    yt_settings.apple_music_enabled = True
    yt_settings.vimeo_enabled = True                 # misconfigured on purpose
    yt_settings.vimeo_access_token = None
    provider = YouTubeProvider(yt_settings)
    _authed(provider)

    video = yt_settings.input_dir / "all.mp4"
    video.write_bytes(sample_video.read_bytes())
    (yt_settings.input_dir / "all.txt").write_text(metadata_text, encoding="utf-8")

    job = Pipeline(yt_settings, db).run(db.create_job(
        base_name="all", video_path=str(video),
        metadata_path=str(yt_settings.input_dir / "all.txt"), dry_run=False))

    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    platforms = job["platforms"]
    assert platforms["tiktok"]["status"] == PlatformStatus.READY_TO_PUBLISH.value
    assert platforms["youtube"]["status"] == PlatformStatus.UPLOADED_PRIVATE.value
    assert platforms["youtube"]["url"].startswith("https://www.youtube.com/watch?v=")
    assert platforms["spotify"]["status"] == PlatformStatus.READY_FOR_DISTRIBUTION.value
    assert platforms["apple_music"]["status"] == PlatformStatus.READY_FOR_DISTRIBUTION.value
    assert platforms["vimeo"]["status"] == PlatformStatus.AUTH_REQUIRED.value
    assert platforms["youtube"]["error"] is None and platforms["tiktok"]["error"] is None

    out = Path(job["output_dir"])
    assert (out / "youtube.json").is_file()
    assert (out / "spotify" / "spotify_ready.flac").is_file()
    assert (out / "apple_music" / "apple_music_metadata.json").is_file()
    assert (out / f"{MASTER_NAME}.flac").is_file()
    assert not list(out.rglob("*.png"))


def test_dry_run_skips_youtube_but_prepares_music(yt_settings, db, sample_video, metadata_text):
    yt_settings.spotify_enabled = True
    provider = YouTubeProvider(yt_settings)
    _authed(provider)
    video = yt_settings.input_dir / "dry2.mp4"
    video.write_bytes(sample_video.read_bytes())
    (yt_settings.input_dir / "dry2.txt").write_text(metadata_text, encoding="utf-8")
    job = Pipeline(yt_settings, db).run(db.create_job(
        base_name="dry2", video_path=str(video),
        metadata_path=str(yt_settings.input_dir / "dry2.txt"), dry_run=True))
    assert job["platforms"]["youtube"]["status"] == PlatformStatus.SKIPPED.value
    assert job["platforms"]["spotify"]["status"] == PlatformStatus.READY_FOR_DISTRIBUTION.value
    assert not any(r["path"] == "/upload/youtube/v3/videos" for r in _state().requests)


# ================================================================ no-fabrication
def test_spotify_and_apple_are_never_treated_as_upload_apis():
    for module, banned in (("spotify.py", ("api.spotify.com/v1/upload", "releases/upload")),
                           ("apple_music.py", ("api.music.apple.com/v1/upload",
                                               "release/ingest"))):
        text = (REPO / "app" / "platforms" / module).read_text(encoding="utf-8")
        assert "httpx" not in text                  # no HTTP client at all
        for fragment in banned:
            assert fragment not in text
    from app.platforms.apple_music import API_UPLOAD_SUPPORTED as APPLE
    from app.platforms.spotify import API_UPLOAD_SUPPORTED as SPOT
    assert SPOT is False and APPLE is False


def test_youtube_never_sends_public_in_any_code_path():
    text = (REPO / "app" / "platforms" / "youtube.py").read_text(encoding="utf-8")
    assert '"privacyStatus": "public"' not in text
    assert "'privacyStatus': 'public'" not in text
    s = Settings(_env_file=None)
    assert s.youtube_privacy_status == "private"
    for value in ("public", "unlisted"):
        s.youtube_privacy_status = value
        with pytest.raises(PublishingNotAllowed):
            YouTubeProvider(s).build_body(_Plan())


def test_no_browser_automation_in_new_modules():
    banned = ("selenium", "playwright", "puppeteer", "BeautifulSoup")
    for module in ("youtube.py", "spotify.py", "apple_music.py", "audio.py",
                   "release_package.py"):
        text = (REPO / "app" / "platforms" / module).read_text(encoding="utf-8").lower()
        assert not [b for b in banned if b.lower() in text]


def test_existing_platforms_are_unchanged(settings):
    s = Settings(_env_file=None)
    assert s.tiktok_post_mode == "UPLOAD" and s.confirm_direct_post is False
    assert s.instagram_enabled is False and s.instagram_mode.value == "PREPARE_ONLY"
    assert s.vimeo_privacy_view == "nobody" and s.dailymotion_visibility == "private"
    assert s.patreon_mode.value == "PREPARE_ONLY" and s.rumble_mode.value == "PREPARE_ONLY"
    from app.platforms.patreon import POST_CREATION_SUPPORTED
    from app.platforms.rumble import OFFICIAL_VOD_UPLOAD_API
    assert POST_CREATION_SUPPORTED is False and OFFICIAL_VOD_UPLOAD_API is False


def test_no_thumbnail_generation_anywhere():
    for module in ("app.images", "app.cover"):
        with pytest.raises(ModuleNotFoundError):
            __import__(module)
    for path in (REPO / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from PIL" not in text and "import PIL" not in text
