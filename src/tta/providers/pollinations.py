"""Pollinations.ai provider (CostClass.FREE).

Pollinations offers keyless, genuinely free image generation via a simple
GET endpoint (https://image.pollinations.ai).  No account, no API key.
Availability is probed before use; failures raise ProviderError so the
watcher stays alive and the registry can fall back to the local renderer.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import requests

from .base import CostClass, ImageProvider, ImageRequest, ProviderError

BASE_URL = "https://image.pollinations.ai"


class PollinationsProvider(ImageProvider):
    name = "pollinations"
    cost = CostClass.FREE

    def __init__(self, timeout: float = 120.0, session: requests.Session | None = None):
        self.timeout = timeout
        self.session = session or requests.Session()

    def availability(self) -> tuple[bool, str]:
        try:
            resp = self.session.get(f"{BASE_URL}/models", timeout=6)
            if resp.ok:
                return True, "image.pollinations.ai reachable (free, keyless)"
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
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_bytes(resp.content)
        # normalize to PNG at the exact requested size
        from PIL import Image

        with Image.open(tmp) as img:
            img = img.convert("RGB")
            if img.size != (request.width, request.height):
                img = img.resize((request.width, request.height), Image.LANCZOS)
            img.save(out_path, "PNG")
        tmp.unlink(missing_ok=True)
        return out_path
