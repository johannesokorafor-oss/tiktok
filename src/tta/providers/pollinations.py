"""Pollinations.ai provider (CostClass.FREE_WITH_LIMITS, real AI).

Pollinations offers keyless, genuinely free image generation via a simple
GET endpoint (https://image.pollinations.ai).  No account, no API key.
Availability is probed before use; failures raise ProviderError so the
watcher stays alive and the registry can fall back to the local renderer.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import requests

from .base import (
    CostClass,
    ImageProvider,
    ImageRequest,
    ProviderError,
    write_normalized_png,
)

BASE_URL = "https://image.pollinations.ai"


class PollinationsProvider(ImageProvider):
    name = "pollinations"
    cost = CostClass.FREE_WITH_LIMITS
    generation_kind = "ai"

    def __init__(self, timeout: float = 120.0, session: requests.Session | None = None):
        self.timeout = timeout
        self.session = session or requests.Session()

    def availability(self) -> tuple[bool, str]:
        try:
            resp = self.session.get(f"{BASE_URL}/models", timeout=6)
            if resp.ok:
                return True, "image.pollinations.ai reachable (keyless; rate-limited, availability not guaranteed)"
            return False, f"image.pollinations.ai returned HTTP {resp.status_code}"
        except requests.RequestException as exc:
            return False, f"image.pollinations.ai unreachable: {exc.__class__.__name__}"

    def generate(self, request: ImageRequest, out_path: Path) -> Path:
        prompt = request.prompt
        if request.negative_prompt:
            # pollinations has no dedicated negative field; append as guidance
            prompt += f" | avoid: {request.negative_prompt}"
        url = f"{BASE_URL}/prompt/{urllib.parse.quote(prompt[:1500], safe='')}"
        params = {
            "width": request.width,
            "height": request.height,
            "nologo": "true",
            "enhance": "true",
        }
        if request.seed is not None:
            params["seed"] = request.seed
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderError(f"pollinations request failed: {exc}") from exc
        if not resp.ok:
            raise ProviderError(
                f"pollinations returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type:
            raise ProviderError(f"pollinations returned non-image ({content_type})")
        return write_normalized_png(resp.content, out_path,
                                    request.width, request.height)
