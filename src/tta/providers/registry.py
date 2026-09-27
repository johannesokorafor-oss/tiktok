"""Provider registry and selection logic.

Rules:
* ``IMAGE_PROVIDER=auto`` -> first available of: comfyui (if configured),
  pollinations (free cloud), local.  PAID providers are NEVER auto-selected.
* An explicitly selected PAID provider still refuses unless
  ``ALLOW_PAID_API=true``.
* On generation failure the registry falls back to the local renderer
  (configurable) so a provider outage produces a usable job, not a crash.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config
from .base import CostClass, ImageProvider, ImageRequest, ProviderError
from .comfyui import ComfyUIProvider
from .local_art import LocalArtProvider
from .openai_image import OpenAIImageProvider
from .pollinations import PollinationsProvider

log = logging.getLogger("tta.providers")


class ProviderRegistry:
    def __init__(self, providers: dict[str, ImageProvider],
                 preference: str = "auto", fallback_to_local: bool = True):
        self.providers = providers
        self.preference = preference
        self.fallback_to_local = fallback_to_local

    # ------------------------------------------------------------------
    def get(self, name: str) -> ImageProvider:
        if name not in self.providers:
            raise ProviderError(
                f"unknown image provider '{name}' (known: {sorted(self.providers)})"
            )
        return self.providers[name]

    def select(self) -> ImageProvider:
        """Resolve the configured preference to a concrete provider."""
        pref = (self.preference or "auto").lower()
        if pref != "auto":
            return self.get(pref)
        # auto: never PAID
        for name in ("comfyui", "pollinations", "local"):
            provider = self.providers.get(name)
            if provider is None or provider.cost is CostClass.PAID:
                continue
            available, reason = provider.availability()
            if available:
                log.info("auto-selected image provider '%s' (%s)", name, reason)
                return provider
            log.debug("provider '%s' unavailable: %s", name, reason)
        return self.get("local")

    def generate(self, request: ImageRequest, out_path: Path) -> tuple[Path, str]:
        """Generate with the selected provider; fall back to local on failure.

        Returns (path, provider_name_used).  Raises ProviderError only if
        every eligible provider failed.
        """
        provider = self.select()
        try:
            return provider.generate(request, out_path), provider.name
        except ProviderError as exc:
            log.warning("provider '%s' failed: %s", provider.name, exc)
            if self.fallback_to_local and provider.name != "local":
                local = self.get("local")
                log.info("falling back to local renderer")
                return local.generate(request, out_path), local.name
            raise

    def describe(self) -> list[dict]:
        return [p.describe() for p in self.providers.values()]


def build_registry(config: Config) -> ProviderRegistry:
    providers: dict[str, ImageProvider] = {
        "local": LocalArtProvider(),
        "pollinations": PollinationsProvider(),
        "comfyui": ComfyUIProvider(config.comfyui_url, config.comfyui_workflow),
        "openai": OpenAIImageProvider(config.openai_api_key, config.allow_paid_api),
    }
    return ProviderRegistry(
        providers,
        preference=config.image_provider,
        fallback_to_local=config.image_fallback_to_local,
    )
