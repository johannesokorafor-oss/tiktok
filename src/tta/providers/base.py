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
    LOCAL_FREE = "LOCAL_FREE"                  # runs on this machine, no billing
    FREE_WITH_LIMITS = "FREE_WITH_LIMITS"      # keyless cloud; rate-limited, availability not guaranteed
    REGISTERED_FREE_QUOTA = "REGISTERED_FREE_QUOTA"  # needs an account; free tier with quota
    PAID = "PAID"                              # costs money per call
    UNKNOWN = "UNKNOWN"                        # cost not verifiable - treated like PAID

# cost classes that must never be selected/called automatically
BILLABLE_CLASSES = (CostClass.PAID, CostClass.UNKNOWN)


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


def write_normalized_png(raw: bytes, out_path: Path, width: int, height: int) -> Path:
    """Validate raw image bytes and write them as PNG at the exact size.

    Any invalid/corrupt payload raises :class:`ProviderError` (so registries
    can fall back) and never leaves partial files behind.
    """
    import io

    from PIL import Image, UnidentifiedImageError

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    try:
        try:
            with Image.open(io.BytesIO(raw)) as img:
                img.load()  # force full decode - catches truncated data
                img = img.convert("RGB")
                if img.size != (width, height):
                    img = img.resize((width, height), Image.LANCZOS)
                img.save(tmp, "PNG")
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ProviderError(f"provider returned invalid image data: {exc}") from exc
        tmp.replace(out_path)
        return out_path
    finally:
        tmp.unlink(missing_ok=True)


class ImageProvider(ABC):
    """A real, callable image source."""

    name: str = "base"
    cost: CostClass = CostClass.UNKNOWN
    #: "ai" for genuine generative models, "procedural" for deterministic art
    generation_kind: str = "ai"

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
            "kind": self.generation_kind,
            "available": available,
            "detail": reason,
        }
