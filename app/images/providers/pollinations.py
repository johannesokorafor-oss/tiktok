"""Pollinations.ai image provider.

**Cost classification is resolved at runtime, not hard-coded.** The adapter
queries the provider's own model catalogue (``GET /models``) and only reports
FREE when that catalogue actually marks the configured model as free/anonymous
tier. If the catalogue is unreachable, changes shape, or does not describe the
tier, the provider is reported as ``UNKNOWN`` - an endpoint that answers
without an API key is *not* proof that it is free.

Set ``POLLINATIONS_TIER`` in .env to assert a tier yourself (reported with
source ``configured``) if you have verified the current pricing.
"""
from __future__ import annotations

import urllib.parse

import httpx

from app.config import ProviderCost
from typing import Optional

from app.images.base import (ImageProvider, ImageProviderError, ImageRequest, ImageResult,
                             PaidProviderBlocked, TierInfo)

#: tier names the provider's catalogue uses for keyless/free access
FREE_TIERS = {"anonymous", "free", "seed", "public"}
#: models historically offered without credits - only used to refuse obviously
#: metered models when the catalogue cannot be read, never to claim "free"
KNOWN_KEYLESS_MODELS = {"flux", "turbo", "kontext", "sdxl"}


class PollinationsProvider(ImageProvider):
    name = "pollinations"
    #: nothing is assumed until the catalogue has been read
    declared_cost = ProviderCost.UNKNOWN
    declared_source = "unverified"
    cost_note = ("Tier is resolved from the provider's live model catalogue. A keyless "
                 "endpoint is not treated as proof of a free tier; premium/credit-metered "
                 "models are refused unless ALLOW_PAID_API=true.")
    is_generative = True
    max_width = 2048
    max_height = 2048

    def is_configured(self) -> bool:
        return bool(self.settings.pollinations_base_url)

    @property
    def model_name(self) -> str:
        return self.settings.pollinations_model.strip().lower()

    # ------------------------------------------------------------------
    def _catalogue(self) -> list:
        url = self.settings.pollinations_base_url.rstrip("/") + "/models"
        with httpx.Client(timeout=min(15.0, self.settings.image_timeout_seconds),
                          follow_redirects=True) as c:
            resp = c.get(url)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("models", data.get("data", []))
        return data if isinstance(data, list) else []

    def _probe_tier(self) -> Optional[TierInfo]:
        configured = (getattr(self.settings, "pollinations_tier", "") or "").strip().upper()
        if configured:
            try:
                return TierInfo(ProviderCost(configured.replace("_", " ")), "configured",
                                "tier asserted via POLLINATIONS_TIER in .env",
                                model=self.model_name)
            except ValueError:
                pass
        try:
            entries = self._catalogue()
        except Exception as exc:  # noqa: BLE001
            return TierInfo(ProviderCost.UNKNOWN, "unverified",
                            f"model catalogue unreachable ({exc.__class__.__name__}); "
                            "current pricing could not be verified",
                            model=self.model_name)
        model = self.model_name
        for entry in entries:
            if isinstance(entry, str):
                if entry.lower() != model:
                    continue
                return TierInfo(ProviderCost.UNKNOWN, "unverified",
                                "catalogue lists the model but reports no tier/pricing field",
                                model=model)
            if not isinstance(entry, dict):
                continue
            names = {str(entry.get(k, "")).lower() for k in ("name", "id", "model")}
            if model not in names:
                continue
            tier_value = str(entry.get("tier", "")).lower()
            price = entry.get("price") or entry.get("pricing") or entry.get("cost")
            if entry.get("premium") is True or (isinstance(price, (int, float)) and price > 0):
                return TierInfo(ProviderCost.PAID, "runtime",
                                f"catalogue marks '{model}' as premium/credit-metered",
                                model=model)
            if tier_value in FREE_TIERS:
                return TierInfo(ProviderCost.FREE, "runtime",
                                f"catalogue reports tier '{tier_value}' for '{model}' "
                                "(rate limited, no SLA)", model=model)
            if tier_value:
                return TierInfo(ProviderCost.FREE_WITH_QUOTA, "runtime",
                                f"catalogue reports tier '{tier_value}' for '{model}'",
                                model=model)
            return TierInfo(ProviderCost.UNKNOWN, "unverified",
                            f"catalogue entry for '{model}' contains no tier/pricing field",
                            model=model)
        return TierInfo(ProviderCost.UNKNOWN, "unverified",
                        f"model '{model}' is not listed in the current catalogue",
                        model=model)

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
        model = self.model_name
        tier = self.tier()
        if not self.settings.allow_paid_api:
            if tier.tier == ProviderCost.PAID:
                raise PaidProviderBlocked(
                    f"pollinations model '{model}' is reported as {tier.tier.value} "
                    f"({tier.detail}) and ALLOW_PAID_API=false")
            if model not in KNOWN_KEYLESS_MODELS and tier.tier == ProviderCost.UNKNOWN:
                raise PaidProviderBlocked(
                    f"pollinations model '{model}' has an unverified tier and is not one of "
                    f"the historically keyless models {sorted(KNOWN_KEYLESS_MODELS)}; "
                    "set POLLINATIONS_TIER or ALLOW_PAID_API=true to use it")
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
                           seed=request.seed, generative=True,
                           meta={"model": model, "tier": tier.tier.value,
                                 "tier_source": tier.source})

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
