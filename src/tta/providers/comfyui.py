"""ComfyUI-compatible local API provider (CostClass.LOCAL).

Talks to a locally running ComfyUI server (default http://127.0.0.1:8188)
via its standard HTTP API: POST /prompt, poll GET /history/{id},
download via GET /view.  A custom workflow JSON template can be supplied
(COMFYUI_WORKFLOW); placeholders ``{prompt}``, ``{negative_prompt}``,
``{width}``, ``{height}``, ``{seed}`` are substituted.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import requests

from .base import CostClass, ImageProvider, ImageRequest, ProviderError

# Minimal SD txt2img workflow in ComfyUI API format.
DEFAULT_WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "cfg": 7, "denoise": 1, "sampler_name": "euler", "scheduler": "normal",
            "seed": "{seed}", "steps": 25,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
    "5": {"class_type": "EmptyLatentImage",
          "inputs": {"batch_size": 1, "width": "{width}", "height": "{height}"}},
    "6": {"class_type": "CLIPTextEncode",
          "inputs": {"clip": ["4", 1], "text": "{prompt}"}},
    "7": {"class_type": "CLIPTextEncode",
          "inputs": {"clip": ["4", 1], "text": "{negative_prompt}"}},
    "8": {"class_type": "VAEDecode",
          "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage",
          "inputs": {"filename_prefix": "tta", "images": ["8", 0]}},
}


class ComfyUIProvider(ImageProvider):
    name = "comfyui"
    cost = CostClass.LOCAL

    def __init__(self, base_url: str, workflow_path: str = "",
                 timeout: float = 600.0, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.workflow_path = workflow_path
        self.timeout = timeout
        self.session = session or requests.Session()

    def availability(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "COMFYUI_URL not configured"
        try:
            resp = self.session.get(f"{self.base_url}/system_stats", timeout=4)
            if resp.ok:
                return True, f"ComfyUI reachable at {self.base_url}"
            return False, f"ComfyUI returned HTTP {resp.status_code}"
        except requests.RequestException as exc:
            return False, f"ComfyUI unreachable at {self.base_url}: {exc.__class__.__name__}"

    # ------------------------------------------------------------------
    def _build_workflow(self, request: ImageRequest) -> dict:
        if self.workflow_path:
            template = Path(self.workflow_path).read_text(encoding="utf-8")
        else:
            template = json.dumps(DEFAULT_WORKFLOW)
        seed = request.seed if request.seed is not None else uuid.uuid4().int % (2**31)
        replacements = {
            '"{seed}"': str(seed),
            '"{width}"': str(request.width),
            '"{height}"': str(request.height),
            "{prompt}": json.dumps(request.prompt)[1:-1],
            "{negative_prompt}": json.dumps(request.negative_prompt)[1:-1],
        }
        for placeholder, value in replacements.items():
            template = template.replace(placeholder, value)
        return json.loads(template)

    def generate(self, request: ImageRequest, out_path: Path) -> Path:
        workflow = self._build_workflow(request)
        client_id = uuid.uuid4().hex
        try:
            resp = self.session.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": client_id},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"ComfyUI submit failed: {exc}") from exc
        if not resp.ok:
            raise ProviderError(f"ComfyUI submit HTTP {resp.status_code}: {resp.text[:300]}")
        prompt_id = resp.json().get("prompt_id")
        if not prompt_id:
            raise ProviderError(f"ComfyUI response missing prompt_id: {resp.text[:300]}")

        deadline = time.monotonic() + self.timeout
        images_info = None
        while time.monotonic() < deadline:
            time.sleep(1.5)
            try:
                hist = self.session.get(
                    f"{self.base_url}/history/{prompt_id}", timeout=10
                ).json()
            except requests.RequestException as exc:
                raise ProviderError(f"ComfyUI history poll failed: {exc}") from exc
            entry = hist.get(prompt_id)
            if not entry:
                continue
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise ProviderError(f"ComfyUI workflow error: {json.dumps(status)[:300]}")
            outputs = entry.get("outputs", {})
            for node in outputs.values():
                if node.get("images"):
                    images_info = node["images"][0]
                    break
            if images_info:
                break
        if not images_info:
            raise ProviderError("ComfyUI generation timed out")

        try:
            img_resp = self.session.get(
                f"{self.base_url}/view",
                params={
                    "filename": images_info["filename"],
                    "subfolder": images_info.get("subfolder", ""),
                    "type": images_info.get("type", "output"),
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"ComfyUI image download failed: {exc}") from exc
        if not img_resp.ok:
            raise ProviderError(f"ComfyUI /view HTTP {img_resp.status_code}")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_bytes(img_resp.content)
        from PIL import Image

        with Image.open(tmp) as img:
            img = img.convert("RGB")
            if img.size != (request.width, request.height):
                img = img.resize((request.width, request.height), Image.LANCZOS)
            img.save(out_path, "PNG")
        tmp.unlink(missing_ok=True)
        return out_path
