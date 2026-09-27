import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import ProviderCost
from app.dashboard.server import AppContext, create_app
from app.images.base import ImageProviderError, ImageRequest, PaidProviderBlocked
from app.images.providers.huggingface import HuggingFaceProvider
from app.images.providers.pollinations import PollinationsProvider
from app.images.registry import ImageService, resolve_chain


@pytest.fixture()
def client(settings):
    ctx = AppContext(settings, autostart=False)
    app = create_app(ctx, autostart=False)
    with TestClient(app) as c:
        yield c, ctx
    ctx.close()


def test_status_endpoint_never_leaks_tokens(client):
    c, _ = client
    body = c.get("/api/status").json()
    text = str(body)
    for secret in ("act.", "rft.", "test-secret", "Bearer "):
        assert secret not in text
    assert body["settings"]["dry_run"] is True
    assert body["providers"][0]["tier"] in {v.value for v in ProviderCost}
    assert "generative" in body["providers"][0]
    assert body["mode"]["effective"] == "DRY_RUN"
    assert body["mode"]["never_auto_publishes"] is True
    assert "ai_model_available" in body["image_quality"]


def test_jobs_endpoints(client, sample_video, metadata_text):
    c, ctx = client
    video = ctx.settings.input_dir / "dash.mp4"
    video.write_bytes(sample_video.read_bytes())
    (ctx.settings.input_dir / "dash.txt").write_text(metadata_text, encoding="utf-8")
    ctx.watcher.scan_once()

    jobs = c.get("/api/jobs").json()["jobs"]
    assert jobs and jobs[0]["state"] == "COMPLETED"
    jid = jobs[0]["id"]

    detail = c.get(f"/api/jobs/{jid}").json()
    assert detail["plan"]["cover_text"]
    assert detail["events"]

    cover = c.get(f"/api/jobs/{jid}/cover")
    assert cover.status_code == 200 and cover.headers["content-type"] == "image/png"

    r = c.post(f"/api/jobs/{jid}/overrides", json={"hook": "DEINE SEELE SPRICHT"})
    assert r.json()["overrides"]["hook"] == "DEINE SEELE SPRICHT"

    assert c.get("/api/jobs/does-not-exist").status_code == 404


def test_watcher_control_endpoints(client):
    c, _ = client
    assert c.post("/api/watcher/scan").status_code == 200
    assert c.post("/api/watcher/start").json()["running"] is True
    assert c.post("/api/watcher/stop").json()["running"] is False
    assert c.post("/api/watcher/nonsense").status_code == 400


def test_oauth_callback_validates_state(client):
    c, _ = client
    r = c.get("/tiktok/callback", params={"code": "abc", "state": "forged"})
    assert r.status_code == 400 and "state" in r.text.lower()


def test_index_renders(client):
    c, _ = client
    assert "TikTok Cover" in c.get("/").text


# ------------------------------------------------------------------ providers
def test_paid_providers_blocked_without_opt_in(settings, monkeypatch):
    monkeypatch.delenv("HUGGINGFACE_ACCEPT_QUOTA", raising=False)
    settings.huggingface_api_token = "hf_dummy"
    settings.allow_paid_api = False
    with pytest.raises(PaidProviderBlocked):
        HuggingFaceProvider(settings).generate(ImageRequest(prompt="x"))


def test_pollinations_rejects_non_free_model(settings):
    settings.pollinations_model = "gptimage"
    settings.allow_paid_api = False
    with pytest.raises(ImageProviderError):
        PollinationsProvider(settings).generate(ImageRequest(prompt="x"))


def test_pollinations_builds_documented_url(settings):
    p = PollinationsProvider(settings)
    url = p._url(ImageRequest(prompt="a night sky", width=1080, height=1920, seed=3))
    assert url.startswith("https://image.pollinations.ai/prompt/")
    for part in ("width=1080", "height=1920", "model=flux", "seed=3", "nologo=true"):
        assert part in url


def test_provider_failure_falls_back_to_next(settings, monkeypatch):
    settings.image_provider = "auto"
    settings.image_provider_primary = "pollinations"
    settings.image_provider_fallback = "offline"
    service = ImageService(settings)

    def boom(self, request):
        raise ImageProviderError("simulated outage")

    monkeypatch.setattr(PollinationsProvider, "generate", boom)
    result, score, attempts = service.generate_background("mystic night", "text, watermark")
    assert result.provider == "offline"
    assert any(a["status"] == "error" for a in attempts)
    assert 0 <= score.total <= 1


def test_chain_order_and_paid_filtering(settings):
    settings.image_provider = "auto"
    settings.image_provider_primary = "comfyui"
    settings.image_provider_fallback = "offline"
    names = [p.name for p in resolve_chain(settings)]
    assert names[0] == "comfyui" and "offline" in names


def test_explicit_provider_selection(settings):
    settings.image_provider = "offline"
    assert [p.name for p in resolve_chain(settings)] == ["offline"]


def test_high_quality_refuses_to_silently_use_the_fallback(settings):
    """HIGH_QUALITY must fail loudly instead of shipping a fallback image."""
    from app.config import QualityMode
    from app.images.registry import NoGenerativeProviderError

    settings.quality_mode = QualityMode.HIGH_QUALITY
    settings.image_provider = "offline"          # only the fallback renderer available
    service = ImageService(settings)
    assert service.candidate_count() == 3
    with pytest.raises(NoGenerativeProviderError) as exc:
        service.generate_background("mystic", "", seed=1)
    assert "No high-quality image provider is configured" in str(exc.value)


def test_high_quality_generates_and_scores_candidates_when_allowed(settings):
    """With the fallback explicitly accepted, 3 candidates are scored."""
    from app.config import QualityMode

    settings.quality_mode = QualityMode.HIGH_QUALITY
    settings.image_provider = "offline"
    service = ImageService(settings)
    result, score, attempts = service.generate_background(
        "mystic", "", seed=1, require_generative=False)
    ok = [a for a in attempts if a["status"] == "ok"]
    assert len(ok) == 3
    assert result.generative is False            # honestly labelled as the fallback
    assert score.total == pytest.approx(max(a["score"]["total"] for a in ok), abs=1e-3)
