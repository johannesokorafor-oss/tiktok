"""Hugging Face Inference Providers image adapter.

Cost status (verified 2026-09-26): a free Hugging Face account includes a
small monthly allowance of Inference Providers credits; once the included
credits are used up, requests fail unless the account has PRO / pay-as-you-go
billing enabled.  It is therefore classified FREE WITH QUOTA, not FREE.  The
adapter refuses to run when ALLOW_PAID_API=false *and*
HUGGINGFACE_ACCEPT_QUOTA is not explicitly acknowledged in the environment.
"""
from __future__ import annotations

import os

import httpx

from app.config import ProviderCost
from app.images.base import (ImageProvider, ImageProviderError, ImageRequest, ImageResult,
                             PaidProviderBlocked)


class HuggingFaceProvider(ImageProvider):
    name = "huggingface"
    declared_cost = ProviderCost.FREE_WITH_QUOTA
    declared_source = "documented"
    cost_note = ("Free account includes a limited monthly Inference credits allowance; beyond it "
                 "requests fail unless you enable PRO / pay-as-you-go billing yourself.")
    is_generative = True

    @property
    def model_name(self) -> str:
        return self.settings.huggingface_model

    def requires_auth(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return bool(self.settings.huggingface_api_token)

    def is_configured(self) -> bool:
        return bool(self.settings.huggingface_api_token)

    def _acknowledged(self) -> bool:
        return os.getenv("HUGGINGFACE_ACCEPT_QUOTA", "").strip().lower() in {"1", "true", "yes"}

    def generate(self, request: ImageRequest) -> ImageResult:
        if not self.is_configured():
            raise ImageProviderError("huggingface: HUGGINGFACE_API_TOKEN is not set")
        if not self.settings.allow_paid_api and not self._acknowledged():
            raise PaidProviderBlocked(
                "huggingface is FREE WITH QUOTA: set HUGGINGFACE_ACCEPT_QUOTA=true to use your "
                "included monthly credits, or ALLOW_PAID_API=true to permit billed usage."
            )
        url = (self.settings.huggingface_base_url.rstrip("/")
               + f"/hf-inference/models/{self.settings.huggingface_model}")
        payload = {
            "inputs": request.prompt,
            "parameters": {
                "negative_prompt": request.negative_prompt,
                "width": request.width,
                "height": request.height,
            },
        }
        headers = {"Authorization": f"Bearer {self.settings.huggingface_api_token}",
                   "Accept": "image/png"}
        try:
            with httpx.Client(timeout=self.settings.image_timeout_seconds) as c:
                r = c.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ImageProviderError(f"huggingface request failed: {exc}") from exc
        if r.status_code == 402:
            raise ImageProviderError("huggingface: monthly free credits exhausted (HTTP 402)")
        if r.status_code == 429:
            raise ImageProviderError("huggingface rate limit (HTTP 429)")
        if r.status_code >= 400:
            raise ImageProviderError(f"huggingface HTTP {r.status_code}: {r.text[:200]}")
        if not r.headers.get("content-type", "").startswith("image/") or len(r.content) < 2048:
            raise ImageProviderError("huggingface returned no image data")
        return ImageResult(data=r.content, mime=r.headers["content-type"].split(";")[0],
                           provider=self.name, seed=request.seed, generative=True,
                           meta={"model": self.settings.huggingface_model})

    def health(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "no HUGGINGFACE_API_TOKEN"
        if not self.settings.allow_paid_api and not self._acknowledged():
            return False, "blocked: set HUGGINGFACE_ACCEPT_QUOTA=true to use included credits"
        return True, "token configured"
