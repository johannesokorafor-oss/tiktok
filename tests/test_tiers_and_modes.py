"""Hardening tests: truthful provider tiers, explicit modes, honest fallbacks."""
from __future__ import annotations

import httpx
import pytest

from app.config import AppMode, CoverSource, ProviderCost, QualityMode, Settings
from app.images.base import ImageRequest, PaidProviderBlocked, TierInfo
from app.images.providers.comfyui import ComfyUIProvider
from app.images.providers.huggingface import HuggingFaceProvider
from app.images.providers.offline import OfflineProvider
from app.images.providers.pollinations import PollinationsProvider
from app.images.registry import ImageService, resolve_chain
from app.jobs.pipeline import Pipeline
from app.jobs.states import JobState
from app.tiktok.client import DEFAULT_RATE_LIMIT, RATE_LIMITS, TikTokClient


# ---------------------------------------------------------------- tiers
def test_pollinations_is_not_free_just_because_it_answers(settings, monkeypatch):
    """A keyless endpoint must not be labelled FREE without verification."""
    monkeypatch.setattr(PollinationsProvider, "_catalogue",
                        lambda self: (_ for _ in ()).throw(httpx.ConnectError("no network")))
    tier = PollinationsProvider(settings).tier(refresh=True)
    assert tier.tier == ProviderCost.UNKNOWN
    assert tier.verified is False
    assert "could not be verified" in tier.detail


def test_pollinations_tier_from_live_catalogue(settings, monkeypatch):
    monkeypatch.setattr(PollinationsProvider, "_catalogue",
                        lambda self: [{"name": "flux", "tier": "anonymous"}])
    tier = PollinationsProvider(settings).tier(refresh=True)
    assert tier.tier == ProviderCost.FREE and tier.source == "runtime"
    assert tier.verified


def test_pollinations_detects_metered_model(settings, monkeypatch):
    settings.pollinations_model = "gptimage"
    monkeypatch.setattr(PollinationsProvider, "_catalogue",
                        lambda self: [{"name": "gptimage", "premium": True}])
    provider = PollinationsProvider(settings)
    assert provider.tier(refresh=True).tier == ProviderCost.PAID
    with pytest.raises(PaidProviderBlocked):
        provider.generate(ImageRequest(prompt="x"))


def test_pollinations_catalogue_without_tier_field_is_unknown(settings, monkeypatch):
    monkeypatch.setattr(PollinationsProvider, "_catalogue", lambda self: ["flux", "turbo"])
    assert PollinationsProvider(settings).tier(refresh=True).tier == ProviderCost.UNKNOWN


def test_user_can_assert_tier_in_env(settings, monkeypatch):
    settings.pollinations_tier = "FREE"
    monkeypatch.setattr(PollinationsProvider, "_catalogue",
                        lambda self: (_ for _ in ()).throw(RuntimeError("offline")))
    tier = PollinationsProvider(settings).tier(refresh=True)
    assert tier.tier == ProviderCost.FREE and tier.source == "configured"


def test_local_and_quota_providers_are_labelled_correctly(settings):
    assert ComfyUIProvider(settings).tier().tier == ProviderCost.LOCAL
    assert OfflineProvider(settings).tier().tier == ProviderCost.LOCAL
    hf = HuggingFaceProvider(settings)
    assert hf.tier().tier == ProviderCost.FREE_WITH_QUOTA
    assert hf.requires_auth() and not hf.is_authenticated()


def test_unknown_tier_can_be_refused(settings, monkeypatch):
    monkeypatch.setattr(PollinationsProvider, "_probe_tier",
                        lambda self: TierInfo(ProviderCost.UNKNOWN, "unverified", "no data"))
    settings.image_provider = "auto"
    settings.image_provider_primary = "pollinations"
    settings.image_provider_fallback = "offline"
    settings.allow_unknown_tier_providers = False
    assert "pollinations" not in [p.name for p in resolve_chain(settings)]
    settings.allow_unknown_tier_providers = True
    assert "pollinations" in [p.name for p in resolve_chain(settings)]


def test_provider_tier_appears_in_description(settings):
    described = {d["name"]: d for d in ImageService(settings).describe()}
    assert described["offline"]["tier"] == "LOCAL"
    assert described["offline"]["generative"] is False
    assert all("tier_source" in d for d in described.values())


# ---------------------------------------------------------------- preflight
def test_preflight_reports_everything_needed(settings):
    settings.image_provider = "auto"          # the test fixture pins "offline"
    reports = {r.provider: r for r in ImageService(settings).preflight()}
    offline = reports["offline"]
    assert offline.usable and offline.generative is False
    assert offline.resolution_ok and offline.model == "pillow-procedural"
    hf = reports["huggingface"]
    assert hf.authenticated is False          # no token configured
    assert "tier" in hf.as_dict()


def test_preflight_detects_unsupported_resolution(settings):
    provider = OfflineProvider(settings)
    assert provider.preflight(20000, 20000).resolution_ok is False


def test_offline_fallback_can_be_disabled_entirely(settings):
    settings.image_provider = "auto"
    settings.allow_offline_image_fallback = False
    assert "offline" not in [p.name for p in resolve_chain(settings)]


# ---------------------------------------------------------------- honest labelling
def test_cover_is_labelled_as_offline_fallback(settings, db, sample_video, metadata_text):
    video = settings.input_dir / "label.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "label.txt").write_text(metadata_text, encoding="utf-8")
    job = Pipeline(settings, db).run(db.create_job(
        base_name="label", video_path=str(video),
        metadata_path=str(settings.input_dir / "label.txt"), dry_run=True))
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    assert job["cover_source"] == CoverSource.OFFLINE_FALLBACK.value
    assert job["cover_provider"] == "offline"
    events = [e["event"] for e in db.events(job["id"])]
    assert "OFFLINE_FALLBACK_USED" in events
    assert "IMAGE_PREFLIGHT" in events


def test_high_quality_job_fails_instead_of_downgrading(settings, db, sample_video, metadata_text):
    settings.quality_mode = QualityMode.HIGH_QUALITY
    settings.dry_run_allow_offline = False
    video = settings.input_dir / "hq.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "hq.txt").write_text(metadata_text, encoding="utf-8")
    job = Pipeline(settings, db).run(db.create_job(
        base_name="hq", video_path=str(video),
        metadata_path=str(settings.input_dir / "hq.txt"), dry_run=True))
    assert job["state"] == JobState.FAILED.value
    assert "No high-quality image provider is configured" in job["error"]
    assert job["cover_source"] is None


def test_dry_run_may_explicitly_accept_offline(settings, db, sample_video, metadata_text):
    settings.quality_mode = QualityMode.HIGH_QUALITY
    settings.dry_run_allow_offline = True
    video = settings.input_dir / "hqok.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "hqok.txt").write_text(metadata_text, encoding="utf-8")
    job = Pipeline(settings, db).run(db.create_job(
        base_name="hqok", video_path=str(video),
        metadata_path=str(settings.input_dir / "hqok.txt"), dry_run=True))
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    assert job["cover_source"] == CoverSource.OFFLINE_FALLBACK.value


# ---------------------------------------------------------------- modes
def test_default_mode_is_dry_run_then_draft_upload():
    assert Settings().effective_mode == AppMode.DRY_RUN
    assert Settings(dry_run=False).effective_mode == AppMode.DRAFT_UPLOAD


def test_direct_post_requires_explicit_confirmation_and_scope():
    asked = Settings(dry_run=False, tiktok_post_mode="DIRECT_POST")
    assert asked.effective_mode == AppMode.DRAFT_UPLOAD
    assert any("CONFIRM_DIRECT_POST" in w for w in asked.mode_warnings)

    no_scope = Settings(dry_run=False, tiktok_post_mode="DIRECT_POST",
                        confirm_direct_post=True, tiktok_scopes="user.info.basic,video.upload")
    assert no_scope.effective_mode == AppMode.DRAFT_UPLOAD
    assert any("video.publish" in w for w in no_scope.mode_warnings)

    ok = Settings(dry_run=False, tiktok_post_mode="DIRECT_POST", confirm_direct_post=True,
                  tiktok_scopes="user.info.basic,video.publish")
    assert ok.effective_mode == AppMode.DIRECT_POST


def test_dry_run_always_wins():
    s = Settings(dry_run=True, app_mode=AppMode.DIRECT_POST, confirm_direct_post=True,
                 tiktok_scopes="video.publish")
    assert s.effective_mode == AppMode.DRY_RUN


def test_pipeline_uses_draft_upload_when_direct_post_is_unconfirmed(
        settings, db, sample_video, metadata_text):
    settings.dry_run = False
    settings.tiktok_mock = True
    settings.tiktok_post_mode = "DIRECT_POST"          # requested but not confirmed
    settings.confirm_direct_post = False
    video = settings.input_dir / "mode.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "mode.txt").write_text(metadata_text, encoding="utf-8")
    job = Pipeline(settings, db).run(db.create_job(
        base_name="mode", video_path=str(video),
        metadata_path=str(settings.input_dir / "mode.txt"), dry_run=False))
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    assert job["upload_result"]["mode"] == "UPLOAD"     # never silently direct-posts


# ---------------------------------------------------------------- rate limits
def test_rate_limits_are_per_endpoint(settings):
    client = TikTokClient(settings)
    assert RATE_LIMITS["/v2/post/publish/inbox/video/init/"] == 6
    assert RATE_LIMITS["/v2/post/publish/status/fetch/"] == 30
    assert RATE_LIMITS["/v2/post/publish/creator_info/query/"] == 20
    assert client.status_limiter is not client.inbox_init_limiter
    assert client.status_limiter.max_calls == 30
    assert client.inbox_init_limiter.max_calls == 6
    assert client.limiter_for("/v2/unknown/endpoint/").max_calls == DEFAULT_RATE_LIMIT
    client.close()


def test_each_limiter_tracks_its_own_budget(settings):
    client = TikTokClient(settings)
    for _ in range(6):
        client.inbox_init_limiter.acquire(sleeper=lambda s: None)
    slept = []
    client.inbox_init_limiter.acquire(sleeper=slept.append)
    assert slept                                   # init budget exhausted
    slept2 = []
    client.status_limiter.acquire(sleeper=slept2.append)
    assert not slept2                              # status budget untouched
    client.close()
