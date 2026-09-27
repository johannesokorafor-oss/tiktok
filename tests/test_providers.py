import pytest
from PIL import Image

from tta.config import Config
from tta.providers import CostClass, ImageRequest, ProviderError, build_registry
from tta.providers.base import ImageProvider, PaidProviderBlocked
from tta.providers.local_art import LocalArtProvider
from tta.providers.openai_image import OpenAIImageProvider
from tta.providers.registry import ProviderRegistry
from tta.tiktok_api import plan_chunks  # noqa: F401  (import sanity)


def test_cost_classes_are_honest(tmp_path):
    cfg = Config(home=tmp_path)
    registry = build_registry(cfg)
    costs = {name: p.cost for name, p in registry.providers.items()}
    assert costs["local"] is CostClass.LOCAL
    assert costs["comfyui"] is CostClass.LOCAL
    assert costs["pollinations"] is CostClass.FREE
    assert costs["openai"] is CostClass.PAID


@pytest.mark.parametrize("style", ["CINEMATIC_MYSTICAL", "DARK_LUXURY", "CLEAN_MODERN"])
def test_local_provider_generates_1080x1920_png(tmp_path, style):
    out = tmp_path / f"{style}.png"
    LocalArtProvider().generate(ImageRequest(prompt="x", style=style, seed=1), out)
    with Image.open(out) as img:
        assert img.size == (1080, 1920)
        assert img.format == "PNG"


def test_local_provider_deterministic_for_same_prompt(tmp_path):
    p = LocalArtProvider()
    a = p.generate(ImageRequest(prompt="same prompt", style="DARK_LUXURY"), tmp_path / "a.png")
    b = p.generate(ImageRequest(prompt="same prompt", style="DARK_LUXURY"), tmp_path / "b.png")
    assert a.read_bytes() == b.read_bytes()


def test_paid_provider_blocked_without_allow_flag(tmp_path):
    provider = OpenAIImageProvider(api_key="sk-test", allow_paid=False)
    available, reason = provider.availability()
    assert not available and "PAID" in reason
    with pytest.raises(PaidProviderBlocked):
        provider.generate(ImageRequest(prompt="x"), tmp_path / "x.png")


def test_auto_never_selects_paid(tmp_path, monkeypatch):
    cfg = Config(home=tmp_path)
    cfg.allow_paid_api = True
    cfg.openai_api_key = "sk-test"
    cfg.image_provider = "auto"
    registry = build_registry(cfg)
    # make cloud/comfy unavailable so selection walks the whole chain
    monkeypatch.setattr(registry.providers["pollinations"], "availability",
                        lambda: (False, "down"))
    monkeypatch.setattr(registry.providers["comfyui"], "availability",
                        lambda: (False, "down"))
    assert registry.select().name == "local"


class _FailingProvider(ImageProvider):
    name = "failing"
    cost = CostClass.FREE

    def availability(self):
        return True, "pretend"

    def generate(self, request, out_path):
        raise ProviderError("boom")


def test_fallback_to_local_on_failure(tmp_path):
    registry = ProviderRegistry(
        {"failing": _FailingProvider(), "local": LocalArtProvider()},
        preference="failing", fallback_to_local=True,
    )
    out = tmp_path / "img.png"
    path, used = registry.generate(ImageRequest(prompt="x", seed=2), out)
    assert used == "local" and path.exists()


def test_no_fallback_raises_recoverable_error(tmp_path):
    registry = ProviderRegistry(
        {"failing": _FailingProvider(), "local": LocalArtProvider()},
        preference="failing", fallback_to_local=False,
    )
    with pytest.raises(ProviderError):
        registry.generate(ImageRequest(prompt="x"), tmp_path / "img.png")


def test_unknown_provider_raises(tmp_path):
    registry = ProviderRegistry({"local": LocalArtProvider()}, preference="nope")
    with pytest.raises(ProviderError):
        registry.select()
