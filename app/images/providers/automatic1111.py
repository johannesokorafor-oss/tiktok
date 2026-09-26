"""AUTOMATIC1111 / Stable Diffusion WebUI local API provider (LOCAL)."""
from __future__ import annotations

import base64
import random

import httpx

from app.config import ProviderCost
from app.images.base import ImageProvider, ImageProviderError, ImageRequest, ImageResult


class Automatic1111Provider(ImageProvider):
    name = "automatic1111"
    cost = ProviderCost.LOCAL
    cost_note = "Runs locally via the WebUI --api flag. No API fees."

    def is_configured(self) -> bool:
        return bool(self.settings.automatic1111_base_url)

    def _base(self) -> str:
        return self.settings.automatic1111_base_url.rstrip("/")

    def generate(self, request: ImageRequest) -> ImageResult:
        seed = request.seed if request.seed is not None else random.randint(1, 2 ** 31 - 1)
        steps = request.steps or {"FAST": 20, "BALANCED": 30, "HIGH_QUALITY": 45}.get(request.quality, 30)
        payload = {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "width": request.width,
            "height": request.height,
            "steps": steps,
            "cfg_scale": 6.5,
            "sampler_name": self.settings.automatic1111_sampler,
            "seed": seed,
            "batch_size": 1,
        }
        try:
            with httpx.Client(timeout=self.settings.image_timeout_seconds) as c:
                r = c.post(f"{self._base()}/sdapi/v1/txt2img", json=payload)
        except httpx.HTTPError as exc:
            raise ImageProviderError(f"automatic1111 unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise ImageProviderError(f"automatic1111 HTTP {r.status_code}: {r.text[:200]}")
        images = r.json().get("images") or []
        if not images:
            raise ImageProviderError("automatic1111 returned no images")
        data = base64.b64decode(images[0])
        if len(data) < 2048:
            raise ImageProviderError("automatic1111 returned an empty image")
        return ImageResult(data=data, mime="image/png", provider=self.name, seed=seed,
                           meta={"steps": steps})

    def health(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=4.0) as c:
                r = c.get(f"{self._base()}/sdapi/v1/options")
            return (r.status_code < 400, f"HTTP {r.status_code}")
        except httpx.HTTPError:
            return False, "not running"
