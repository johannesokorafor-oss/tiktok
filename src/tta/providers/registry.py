"""Provider registry and selection logic.

Rules:
* ``IMAGE_PROVIDER=auto`` -> first available of: comfyui (if configured),
  pollinations (keyless AI cloud), local.  PAID/UNKNOWN-cost providers are
  NEVER auto-selected - a billable request can never happen silently.
* An explicitly selected PAID provider still refuses unless
  ``ALLOW_PAID_API=true``.
* On generation failure the registry falls back to the local renderer
  (configurable) so a provider outage produces a usable job, not a crash.
* Every generation reports an honest mode: AI_GENERATED, PROCEDURAL or
  PROCEDURAL_FALLBACK - a fallback is never presented as AI output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from .base import BILLABLE_CLASSES, ImageProvider, ImageRequest, ProviderError
from .comfyui import ComfyUIProvider
from .local_art import LocalArtProvider
from .openai_image import OpenAIImageProvider
from .pollinations import PollinationsProvider

log = logging.getLogger("tta.providers")


@dataclass
class GenerationResult:
    path: Path
    provider: str          # actual provider that produced the image
    mode: str              # AI_GENERATED | PROCEDURAL | PROCEDURAL_FALLBACK
    requested_provider: str = ""
    fallback_reason: str = ""


def _mode_for(provider: ImageProvider, is_fallback: bool) -> str:
    if provider.generation_kind == "procedural":
        return "PROCEDURAL_FALLBACK" if is_fallback else "PROCEDURAL"
    return "AI_GENERATED"


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
        # auto: never anything billable (PAID or UNKNOWN cost)
        for name in ("comfyui", "pollinations", "local"):
            provider = self.providers.get(name)
            if provider is None or provider.cost in BILLABLE_CLASSES:
                continue
            available, reason = provider.availability()
            if available:
                log.info("auto-selected image provider '%s' (%s)", name, reason)
                return provider
            log.debug("provider '%s' unavailable: %s", name, reason)
        return self.get("local")

    def generate(self, request: ImageRequest, out_path: Path) -> GenerationResult:
        """Generate with the selected provider; fall back to local on failure.

        Returns a :class:`GenerationResult` that honestly records which
        provider produced the image and whether it was AI-generated,
        procedural, or a procedural fallback.  Raises ProviderError only
        if every eligible provider failed.
        """
        provider = self.select()
        try:
            path = provider.generate(request, out_path)
            return GenerationResult(
                path=path, provider=provider.name,
                mode=_mode_for(provider, is_fallback=False),
                requested_provider=provider.name,
            )
        except ProviderError as exc:
            log.warning("provider '%s' failed: %s", provider.name, exc)
            if self.fallback_to_local and provider.name != "local":
                local = self.get("local")
                log.info("falling back to local procedural renderer")
                path = local.generate(request, out_path)
                return GenerationResult(
                    path=path, provider=local.name,
                    mode=_mode_for(local, is_fallback=True),
                    requested_provider=provider.name,
                    fallback_reason=str(exc)[:300],
                )
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
