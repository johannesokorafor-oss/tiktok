"""Pollinations.ai image provider.

Cost status (verified 2026-09-26): Pollinations exposes a keyless HTTP image
endpoint (https://image.pollinations.ai/prompt/<prompt>) whose core models
(e.g. `flux`) are offered at no cost and without signup; anonymous use is
rate limited under fair use, and separate "premium" models are credit
metered.  This adapter only ever requests the free model configured in
POLLINATIONS_MODEL and never sends billing credentials, so it is classified
FREE (rate limited, no SLA).  See docs/IMAGE_PROVIDER.md for the sources.
"""
from __future__ import annotations

import urllib.parse

import httpx

from app.config import ProviderCost
from app.images.base import ImageProvider, ImageProviderError, ImageRequest, ImageResult

FREE_MODELS = {"flux", "turbo", "kontext", "sdxl"}


class PollinationsProvider(ImageProvider):
    name = "pollinations"
    cost = ProviderCost.FREE
    cost_note = ("Keyless public endpoint, free core models (flux/turbo), rate limited under "
                 "fair use, no SLA. Premium models are credit-metered and are NOT used.")

    def is_configured(self) -> bool:
        return bool(self.settings.pollinations_base_url)

    def _url(self, req: ImageRequest) -> str:
        model = self.settings.pollinations_model.strip().lower()
        prompt = req.prompt
        if req.negative_prompt:
            prompt = f"{prompt} | avoid: {req.negative_prompt}"
        encoded = urllib.parse.quote(prompt[:1800], safe="")
        params = {
            "width": req.width,
            "height": req.height,
            "model": model,
            "nologo": "true",
            "enhance": "false",
            "safe": "false",
        }
        if req.seed is not None:
            params["seed"] = req.seed
        base = self.settings.pollinations_base_url.rstrip("/")
        return f"{base}/prompt/{encoded}?" + urllib.parse.urlencode(params)

    def generate(self, request: ImageRequest) -> ImageResult:
        model = self.settings.pollinations_model.strip().lower()
        if model not in FREE_MODELS and not self.settings.allow_paid_api:
            raise ImageProviderError(
                f"pollinations model '{model}' is not in the verified free model set "
                f"{sorted(FREE_MODELS)} and ALLOW_PAID_API=false"
            )
        headers = {"Accept": "image/*"}
        if self.settings.pollinations_token:
            headers["Authorization"] = f"Bearer {self.settings.pollinations_token}"
        url = self._url(request)
        try:
            with httpx.Client(timeout=self.settings.image_timeout_seconds, follow_redirects=True) as c:
                resp = c.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ImageProviderError(f"pollinations request failed: {exc}") from exc
        if resp.status_code == 429:
            raise ImageProviderError("pollinations rate limit (HTTP 429)")
        if resp.status_code >= 400:
            raise ImageProviderError(f"pollinations HTTP {resp.status_code}: {resp.text[:200]}")
        ctype = resp.headers.get("content-type", "")
        if not ctype.startswith("image/") or len(resp.content) < 2048:
            raise ImageProviderError(f"pollinations returned non-image content ({ctype})")
        return ImageResult(data=resp.content, mime=ctype.split(";")[0], provider=self.name,
                           seed=request.seed, meta={"model": model})

    def health(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "not configured"
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as c:
                r = c.get(self.settings.pollinations_base_url.rstrip("/") + "/models")
            if r.status_code < 500:
                return True, f"reachable (HTTP {r.status_code})"
            return False, f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            return False, f"unreachable: {exc.__class__.__name__}"
