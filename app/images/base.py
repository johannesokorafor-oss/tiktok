"""Image provider abstraction.

Two things matter here beyond "generate an image":

* **Truthful cost reporting.** A provider exposes a *declared* tier and a
  :meth:`ImageProvider.tier` method that tries to verify the tier at runtime.
  If the current pricing/quota cannot be verified, the tier is reported as
  ``UNKNOWN`` - never as ``FREE``.
* **Truthful quality reporting.** ``is_generative`` distinguishes real
  image models (cloud or local diffusion) from the built-in procedural
  fallback renderer, so the pipeline and the dashboard can say exactly which
  one produced a cover.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Optional

from app.config import ProviderCost, Settings


class ImageProviderError(RuntimeError):
    """Raised when a provider cannot produce an image."""


class PaidProviderBlocked(ImageProviderError):
    """Raised when ALLOW_PAID_API=false blocks a paid (or unverified) provider."""


@dataclass
class TierInfo:
    """Result of a tier/pricing resolution."""

    tier: ProviderCost
    #: "runtime" (verified now), "documented" (verified by us, pinned in code),
    #: "configured" (asserted by the user in .env) or "unverified"
    source: str = "unverified"
    detail: str = ""
    model: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def verified(self) -> bool:
        return self.tier != ProviderCost.UNKNOWN and self.source != "unverified"

    def as_dict(self) -> dict:
        return {"tier": self.tier.value, "source": self.source, "detail": self.detail,
                "model": self.model, "verified": self.verified}


@dataclass
class PreflightResult:
    """Pre-flight report for one provider (see ImageService.preflight)."""

    provider: str
    available: bool
    authenticated: bool
    model: str
    tier: TierInfo
    generative: bool
    resolution_ok: bool
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.available and self.authenticated and self.resolution_ok

    def as_dict(self) -> dict:
        return {"provider": self.provider, "available": self.available,
                "authenticated": self.authenticated, "model": self.model,
                "generative": self.generative, "resolution_ok": self.resolution_ok,
                "usable": self.usable, "detail": self.detail, **self.tier.as_dict()}


@dataclass
class ImageRequest:
    prompt: str
    negative_prompt: str = ""
    width: int = 1080
    height: int = 1920
    seed: Optional[int] = None
    steps: Optional[int] = None
    quality: str = "BALANCED"


@dataclass
class ImageResult:
    data: bytes
    mime: str
    provider: str
    seed: Optional[int] = None
    meta: Optional[dict] = None
    #: False only for the built-in procedural fallback renderer
    generative: bool = True


class ImageProvider(abc.ABC):
    name: str = "base"
    #: tier assumed before any runtime verification
    declared_cost: ProviderCost = ProviderCost.UNKNOWN
    #: how the declared tier was established, see TierInfo.source
    declared_source: str = "unverified"
    #: short, human readable statement about the cost situation
    cost_note: str = ""
    requires_network: bool = True
    #: True for real image models, False for the procedural fallback renderer
    is_generative: bool = True
    #: maximum resolution the provider can be asked for
    max_width: int = 4096
    max_height: int = 4096

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tier_cache: Optional[TierInfo] = None

    # ------------------------------------------------------------------
    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True when the provider has everything it needs to run."""

    @abc.abstractmethod
    def generate(self, request: ImageRequest) -> ImageResult:
        """Generate one image. Must raise ImageProviderError on failure."""

    # ------------------------------------------------------------------
    @property
    def model_name(self) -> str:
        return ""

    def requires_auth(self) -> bool:
        return False

    def is_authenticated(self) -> bool:
        return True

    def supports_resolution(self, width: int, height: int) -> bool:
        return width <= self.max_width and height <= self.max_height

    def _probe_tier(self) -> Optional[TierInfo]:
        """Ask the provider about its current pricing/quota. None = unknown."""
        return None

    def tier(self, refresh: bool = False) -> TierInfo:
        """Resolve the commercial tier, preferring runtime verification."""
        ttl = getattr(self.settings, "provider_tier_cache_seconds", 900.0)
        cached = self._tier_cache
        if cached and not refresh and (time.time() - cached.checked_at) < ttl:
            return cached
        info: Optional[TierInfo] = None
        try:
            info = self._probe_tier()
        except Exception as exc:  # noqa: BLE001 - a pricing probe must never break a job
            info = TierInfo(ProviderCost.UNKNOWN, "unverified",
                            f"tier probe failed: {exc.__class__.__name__}",
                            model=self.model_name)
        if info is None:
            info = TierInfo(self.declared_cost, self.declared_source, self.cost_note,
                            model=self.model_name)
        self._tier_cache = info
        return info

    def health(self) -> tuple[bool, str]:
        """Cheap reachability/configuration probe for the dashboard/diagnostics."""
        ok = self.is_configured()
        return (ok, "configured" if ok else "not configured")

    def preflight(self, width: int, height: int) -> PreflightResult:
        available, detail = self.health()
        return PreflightResult(
            provider=self.name,
            available=available,
            authenticated=(not self.requires_auth()) or self.is_authenticated(),
            model=self.model_name,
            tier=self.tier(),
            generative=self.is_generative,
            resolution_ok=self.supports_resolution(width, height),
            detail=detail,
        )
