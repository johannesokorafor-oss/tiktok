"""Image provider abstraction.

Every provider declares an honest :class:`CostClass`.  Paid providers are
hard-gated behind ``ALLOW_PAID_API`` in the registry - a paid call can never
happen silently.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class CostClass(enum.Enum):
    LOCAL = "LOCAL"                    # runs on this machine, no network cost
    FREE = "FREE"                      # verified free cloud service
    FREE_WITH_QUOTA = "FREE_WITH_QUOTA"  # free tier with limits
    PAID = "PAID"                      # costs money per call


class ProviderError(RuntimeError):
    """Recoverable provider failure (job fails, watcher keeps running)."""


class PaidProviderBlocked(ProviderError):
    """Raised when a paid provider is invoked while ALLOW_PAID_API=false."""


@dataclass
class ImageRequest:
    prompt: str
    negative_prompt: str = ""
    width: int = 1080
    height: int = 1920
    seed: int | None = None
    style: str = "CLEAN_MODERN"


class ImageProvider(ABC):
    """A real, callable image source."""

    name: str = "base"
    cost: CostClass = CostClass.LOCAL

    @abstractmethod
    def availability(self) -> tuple[bool, str]:
        """Return (available, human-readable reason)."""

    @abstractmethod
    def generate(self, request: ImageRequest, out_path: Path) -> Path:
        """Generate an image and write it to ``out_path`` (PNG).

        Must raise :class:`ProviderError` on failure - never crash the
        process.
        """

    def describe(self) -> dict:
        available, reason = self.availability()
        return {
            "name": self.name,
            "cost": self.cost.value,
            "available": available,
            "detail": reason,
        }
