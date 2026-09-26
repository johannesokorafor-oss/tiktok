"""Provider registry, ordering, fallback chain and candidate scoring."""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from PIL import Image, ImageFilter, ImageStat

from app.config import ProviderCost, QualityMode, Settings
from app.images.base import (ImageProvider, ImageProviderError, ImageRequest, ImageResult,
                             PaidProviderBlocked)
from app.images.providers.automatic1111 import Automatic1111Provider
from app.images.providers.comfyui import ComfyUIProvider
from app.images.providers.huggingface import HuggingFaceProvider
from app.images.providers.offline import OfflineProvider
from app.images.providers.pollinations import PollinationsProvider

log = logging.getLogger(__name__)

PROVIDER_CLASSES = {
    PollinationsProvider.name: PollinationsProvider,
    ComfyUIProvider.name: ComfyUIProvider,
    Automatic1111Provider.name: Automatic1111Provider,
    HuggingFaceProvider.name: HuggingFaceProvider,
    OfflineProvider.name: OfflineProvider,
}

#: default preference order — quality first, then genuinely free availability,
#: reliability, resolution control and speed.  Verified cost data lives on each
#: provider class (see docs/IMAGE_PROVIDER.md).
DEFAULT_ORDER = ["pollinations", "comfyui", "automatic1111", "huggingface", "offline"]

#: substrings that mark a provider error as transient and worth retrying
RETRYABLE_HINTS = ("429", "rate limit", "timeout", "timed out", "temporarily", "502", "503",
                   "504", "connection", "network", "unreachable", "reset by peer", "500")


def build_provider(name: str, settings: Settings) -> ImageProvider:
    cls = PROVIDER_CLASSES.get(name.strip().lower())
    if cls is None:
        raise KeyError(f"unknown image provider '{name}'")
    return cls(settings)


def all_providers(settings: Settings) -> List[ImageProvider]:
    return [build_provider(n, settings) for n in DEFAULT_ORDER]


def resolve_chain(settings: Settings) -> List[ImageProvider]:
    """Ordered provider chain honouring IMAGE_PROVIDER / PRIMARY / FALLBACK."""
    names: List[str] = []
    explicit = (settings.image_provider or "auto").strip().lower()
    if explicit and explicit != "auto":
        names = [explicit]
    else:
        if settings.image_provider_primary:
            names.append(settings.image_provider_primary.strip().lower())
        names.extend(settings.fallback_providers)
        for n in DEFAULT_ORDER:
            if n not in names:
                names.append(n)
    seen, chain = set(), []
    for n in names:
        if n in seen or n not in PROVIDER_CLASSES:
            continue
        seen.add(n)
        p = build_provider(n, settings)
        if p.cost == ProviderCost.PAID and not settings.allow_paid_api:
            log.warning("skipping paid provider %s because ALLOW_PAID_API=false", n)
            continue
        chain.append(p)
    return chain


# --------------------------------------------------------------------------
# deterministic candidate scoring
# --------------------------------------------------------------------------
@dataclass
class CandidateScore:
    total: float
    contrast: float
    sharpness: float
    text_area: float
    focal: float
    clipping: float

    def as_dict(self) -> dict:
        return {"total": round(self.total, 3), "contrast": round(self.contrast, 3),
                "sharpness": round(self.sharpness, 3), "text_area": round(self.text_area, 3),
                "focal": round(self.focal, 3), "clipping": round(self.clipping, 3)}


def score_image(data: bytes, text_region: tuple = (0.06, 0.06, 0.94, 0.45)) -> CandidateScore:
    """Deterministic quality score: contrast, detail, calm text region, focal strength."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    gray = img.convert("L")

    stat = ImageStat.Stat(gray)
    contrast = min(1.0, (stat.stddev[0] or 0) / 70.0)

    edges = gray.filter(ImageFilter.FIND_EDGES)
    sharpness = min(1.0, (ImageStat.Stat(edges).mean[0] or 0) / 26.0)

    x0, y0, x1, y1 = (int(text_region[0] * w), int(text_region[1] * h),
                      int(text_region[2] * w), int(text_region[3] * h))
    top = gray.crop((x0, y0, x1, y1))
    top_edges = ImageStat.Stat(top.filter(ImageFilter.FIND_EDGES)).mean[0] or 0
    # calmer = better for typography
    text_area = max(0.0, 1.0 - min(1.0, top_edges / 34.0))

    bottom = gray.crop((0, int(h * 0.45), w, h))
    focal = min(1.0, ((ImageStat.Stat(bottom).stddev[0] or 0) / 62.0))

    hist = gray.histogram()
    total_px = max(1, sum(hist))
    clipped = (sum(hist[:3]) + sum(hist[-3:])) / total_px
    clipping = max(0.0, 1.0 - min(1.0, clipped / 0.22))

    total = (0.26 * contrast + 0.20 * sharpness + 0.26 * text_area
             + 0.16 * focal + 0.12 * clipping)
    return CandidateScore(total, contrast, sharpness, text_area, focal, clipping)


class ImageService:
    """Generates a background image through the configured provider chain."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.chain = resolve_chain(settings)

    def describe(self) -> List[Dict[str, str]]:
        out = []
        for p in self.chain:
            ok, note = (p.is_configured(), p.cost_note)
            out.append({"name": p.name, "cost": p.cost.value, "configured": str(ok),
                        "note": note})
        return out

    def candidate_count(self) -> int:
        if self.settings.image_candidates > 0:
            base = self.settings.image_candidates
        else:
            base = 1
        if self.settings.quality_mode == QualityMode.HIGH_QUALITY:
            return max(base, 3)
        if self.settings.quality_mode == QualityMode.FAST:
            return 1
        return base

    def _generate_with_retry(self, provider, req, index, attempts, errors, on_event):
        """One candidate from one provider, with bounded retry + backoff.

        Transient failures (rate limits, timeouts, network blips) are retried
        `IMAGE_MAX_RETRIES` times with exponential backoff; a provider that
        keeps failing simply hands over to the next one in the chain. No
        provider error is ever allowed to propagate out of the service.
        """
        max_retries = max(0, self.settings.image_max_retries)
        for attempt in range(max_retries + 1):
            started = time.monotonic()
            try:
                res = provider.generate(req)
                sc = score_image(res.data)
                attempts.append({"provider": provider.name, "status": "ok", "candidate": index,
                                 "attempt": attempt, "score": sc.as_dict(),
                                 "duration_ms": int((time.monotonic() - started) * 1000)})
                if on_event:
                    on_event(provider.name, "candidate_ok", sc.as_dict())
                return res, sc
            except PaidProviderBlocked as exc:
                errors.append(f"{provider.name}: {exc}")
                attempts.append({"provider": provider.name, "status": "blocked",
                                 "candidate": index, "detail": str(exc)})
                if on_event:
                    on_event(provider.name, "blocked", str(exc))
                return None
            except ImageProviderError as exc:
                detail = str(exc)
            except Exception as exc:  # defensive: a provider must never kill the watcher
                detail = f"unexpected {exc.__class__.__name__}: {exc}"
                log.exception("image provider %s raised", provider.name)

            retryable = any(t in detail.lower() for t in RETRYABLE_HINTS)
            attempts.append({"provider": provider.name, "status": "error", "candidate": index,
                             "attempt": attempt, "detail": detail, "retryable": retryable})
            errors.append(f"{provider.name}: {detail}")
            if on_event:
                on_event(provider.name, "error", detail)
            if not retryable or attempt >= max_retries:
                return None
            backoff = min(30.0, self.settings.image_backoff_base_seconds * (2 ** attempt))
            log.warning("image provider %s failed (%s); retrying in %.1fs",
                        provider.name, detail, backoff)
            time.sleep(backoff)
        return None

    def generate_background(self, prompt: str, negative_prompt: str, *,
                            seed: Optional[int] = None,
                            width: Optional[int] = None,
                            height: Optional[int] = None,
                            on_event=None) -> tuple[ImageResult, CandidateScore, List[dict]]:
        width = width or self.settings.cover_width
        height = height or self.settings.cover_height
        n = self.candidate_count()
        attempts: List[dict] = []
        errors: List[str] = []

        for provider in self.chain:
            if not provider.is_configured():
                attempts.append({"provider": provider.name, "status": "skipped",
                                 "detail": "not configured"})
                continue
            results: List[tuple[ImageResult, CandidateScore]] = []
            for i in range(n):
                req = ImageRequest(prompt=prompt, negative_prompt=negative_prompt,
                                   width=width, height=height,
                                   seed=(seed + i) if seed is not None else None,
                                   quality=self.settings.quality_mode.value)
                outcome = self._generate_with_retry(provider, req, i, attempts, errors, on_event)
                if outcome is None:
                    break
                results.append(outcome)
            if results:
                results.sort(key=lambda rs: -rs[1].total)
                best = results[0]
                return best[0], best[1], attempts
        raise ImageProviderError("all image providers failed: " + " | ".join(errors or ["none configured"]))
