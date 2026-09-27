"""ComfyUI local HTTP API provider (LOCAL, no external cost).

Uses the documented ComfyUI HTTP API: POST /prompt to queue a workflow,
GET /history/{prompt_id} to await completion, GET /view to download the
resulting image.  A default SDXL-style text2img graph is used unless a
custom workflow JSON is configured via COMFYUI_WORKFLOW_FILE (placeholders
{{POSITIVE}}, {{NEGATIVE}}, {{WIDTH}}, {{HEIGHT}}, {{SEED}}, {{STEPS}} are
substituted).
"""
from __future__ import annotations

import json
from pathlib import Path
import random
import time
import uuid
from typing import Any, Dict

import httpx

from app.config import ProviderCost
from app.images.base import ImageProvider, ImageProviderError, ImageRequest, ImageResult


def default_workflow(ckpt: str, pos: str, neg: str, w: int, h: int, seed: int, steps: int) -> Dict[str, Any]:
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": 6.5, "sampler_name": "dpmpp_2m",
            "scheduler": "karras", "denoise": 1.0, "model": ["4", 0],
            "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "tiktok_cover", "images": ["8", 0]}},
    }


class ComfyUIProvider(ImageProvider):
    name = "comfyui"
    #: LOCAL is verifiable by construction: the request never leaves the machine
    declared_cost = ProviderCost.LOCAL
    declared_source = "documented"
    cost_note = "Runs on your own machine/GPU. No API fees; you pay only electricity/hardware."
    is_generative = True
    max_width = 4096
    max_height = 4096

    def is_configured(self) -> bool:
        return bool(self.settings.comfyui_base_url)

    @property
    def model_name(self) -> str:
        if self.settings.comfyui_workflow_file:
            return f"workflow:{Path(self.settings.comfyui_workflow_file).name}"
        return self.settings.comfyui_checkpoint

    def _base(self) -> str:
        return self.settings.comfyui_base_url.rstrip("/")

    def _build_workflow(self, req: ImageRequest, seed: int, steps: int) -> Dict[str, Any]:
        wf_file = self.settings.comfyui_workflow_file
        if wf_file:
            raw = wf_file.read_text(encoding="utf-8")
            raw = (raw.replace("{{POSITIVE}}", json.dumps(req.prompt)[1:-1])
                      .replace("{{NEGATIVE}}", json.dumps(req.negative_prompt)[1:-1])
                      .replace("{{WIDTH}}", str(req.width))
                      .replace("{{HEIGHT}}", str(req.height))
                      .replace("{{SEED}}", str(seed))
                      .replace("{{STEPS}}", str(steps)))
            return json.loads(raw)
        return default_workflow(self.settings.comfyui_checkpoint, req.prompt,
                                req.negative_prompt, req.width, req.height, seed, steps)

    def generate(self, request: ImageRequest) -> ImageResult:
        seed = request.seed if request.seed is not None else random.randint(1, 2 ** 31 - 1)
        steps = request.steps or {"FAST": 20, "BALANCED": 30, "HIGH_QUALITY": 45}.get(request.quality, 30)
        workflow = self._build_workflow(request, seed, steps)
        client_id = str(uuid.uuid4())
        timeout = self.settings.image_timeout_seconds
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(f"{self._base()}/prompt", json={"prompt": workflow, "client_id": client_id})
                if r.status_code >= 400:
                    raise ImageProviderError(f"comfyui /prompt HTTP {r.status_code}: {r.text[:200]}")
                prompt_id = r.json().get("prompt_id")
                if not prompt_id:
                    raise ImageProviderError("comfyui did not return a prompt_id")

                deadline = time.time() + timeout
                images = []
                while time.time() < deadline:
                    h = c.get(f"{self._base()}/history/{prompt_id}")
                    if h.status_code < 400:
                        hist = h.json().get(prompt_id)
                        if hist:
                            for node in hist.get("outputs", {}).values():
                                images.extend(node.get("images", []))
                            if images:
                                break
                            status = hist.get("status", {})
                            if status.get("status_str") == "error":
                                raise ImageProviderError(f"comfyui workflow error: {str(status)[:300]}")
                    time.sleep(1.0)
                if not images:
                    raise ImageProviderError("comfyui produced no image before timeout")

                img = images[0]
                v = c.get(f"{self._base()}/view", params={
                    "filename": img["filename"], "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output")})
                if v.status_code >= 400 or len(v.content) < 2048:
                    raise ImageProviderError(f"comfyui /view failed HTTP {v.status_code}")
        except httpx.HTTPError as exc:
            raise ImageProviderError(f"comfyui unreachable: {exc}") from exc
        return ImageResult(data=v.content, mime=v.headers.get("content-type", "image/png").split(";")[0],
                           provider=self.name, seed=seed, generative=True,
                           meta={"steps": steps, "model": self.model_name})

    def health(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=4.0) as c:
                r = c.get(f"{self._base()}/system_stats")
            return (r.status_code < 400, f"HTTP {r.status_code}")
        except httpx.HTTPError:
            return False, "not running"
