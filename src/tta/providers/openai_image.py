"""OpenAI Images provider (CostClass.PAID).

Hard-gated: refuses to run unless ALLOW_PAID_API=true AND the provider was
explicitly selected.  Never used by IMAGE_PROVIDER=auto.
"""

from __future__ import annotations

import base64
from pathlib import Path

import requests

from .base import (
    CostClass,
    ImageProvider,
    ImageRequest,
    PaidProviderBlocked,
    ProviderError,
    write_normalized_png,
)

API_URL = "https://api.openai.com/v1/images/generations"


class OpenAIImageProvider(ImageProvider):
    name = "openai"
    cost = CostClass.PAID

    def __init__(self, api_key: str, allow_paid: bool,
                 model: str = "gpt-image-1", session: requests.Session | None = None):
        self.api_key = api_key
        self.allow_paid = allow_paid
        self.model = model
        self.session = session or requests.Session()

    def availability(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "OPENAI_API_KEY not configured"
        if not self.allow_paid:
            return False, "PAID provider blocked (set ALLOW_PAID_API=true to enable)"
        return True, "configured (PAID - every call costs money)"

    def generate(self, request: ImageRequest, out_path: Path) -> Path:
        if not self.allow_paid:
            raise PaidProviderBlocked(
                "OpenAI Images is a PAID provider; refusing because ALLOW_PAID_API=false"
            )
        if not self.api_key:
            raise ProviderError("OPENAI_API_KEY not configured")
        payload = {
            "model": self.model,
            "prompt": request.prompt,
            # closest supported portrait size; resized to target afterwards
            "size": "1024x1536",
            "n": 1,
        }
        try:
            resp = self.session.post(
                API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=300,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"OpenAI request failed: {exc}") from exc
        if not resp.ok:
            raise ProviderError(f"OpenAI HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json().get("data") or []
        if not data or "b64_json" not in data[0]:
            raise ProviderError("OpenAI response contained no image data")
        try:
            raw = base64.b64decode(data[0]["b64_json"])
        except (ValueError, TypeError) as exc:
            raise ProviderError(f"OpenAI returned undecodable image data: {exc}") from exc
        return write_normalized_png(raw, out_path, request.width, request.height)
