"""Image provider abstraction."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from app.config import ProviderCost, Settings


class ImageProviderError(RuntimeError):
    """Raised when a provider cannot produce an image."""


class PaidProviderBlocked(ImageProviderError):
    """Raised when ALLOW_PAID_API=false blocks a paid provider."""


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


class ImageProvider(abc.ABC):
    name: str = "base"
    cost: ProviderCost = ProviderCost.PAID
    #: short, human readable statement of what was verified about the cost
    cost_note: str = ""
    requires_network: bool = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True when the provider has everything it needs to run."""

    @abc.abstractmethod
    def generate(self, request: ImageRequest) -> ImageResult:
        """Generate one image. Must raise ImageProviderError on failure."""

    def health(self) -> tuple[bool, str]:
        """Cheap reachability/configuration probe for the dashboard/diagnostics."""
        return (self.is_configured(), "configured" if self.is_configured() else "not configured")
