# Image providers

Cover backgrounds are produced by a pluggable provider system
(`src/tta/providers/`). Every provider declares an honest cost class:

| Provider | Cost class | Kind | Needs | Notes |
|---|---|---|---|---|
| `local` | `LOCAL_FREE` | procedural (NOT AI) | nothing | built-in procedural renderer (Pillow): cinematic gradients, celestial glow, light rays, silhouettes, bokeh, grain, vignette. Always available; deterministic per prompt. |
| `pollinations` | `FREE_WITH_LIMITS` | AI | internet | https://image.pollinations.ai — keyless AI image generation. Rate-limited; availability and pricing are controlled by a third party and not guaranteed to stay free. |
| `comfyui` | `LOCAL_FREE` | AI | running ComfyUI server + model | Stable-Diffusion-class AI images on your own GPU via a ComfyUI-compatible API (`POST /prompt`, `GET /history`, `GET /view`). Recommended path for guaranteed-free real AI covers. |
| `openai` | `PAID` | AI | `OPENAI_API_KEY` + `ALLOW_PAID_API=true` | gpt-image-1, 1024×1536 → resized to 1080×1920. Every call is billed. |

Every job records the honest outcome in its metadata and `job.json`:

```
"image_provider":        "pollinations" | "comfyui" | "local" | "openai"
"image_generation_mode": "AI_GENERATED" | "PROCEDURAL" | "PROCEDURAL_FALLBACK"
"image_fallback_reason": "<error>"      (only present when a fallback happened)
```

A procedural fallback is never presented as AI output.

## Selection

```
IMAGE_PROVIDER=auto        # default
```

`auto` picks the first *available* of: `comfyui` → `pollinations` →
`local`. **PAID (and unknown-cost) providers are never auto-selected.**
Explicitly selecting `openai` still refuses to run unless
`ALLOW_PAID_API=true` (default `false`) — a billable API call can never
happen silently.

If the selected provider fails at generation time, the job falls back to
the `local` renderer (disable with `IMAGE_FALLBACK_TO_LOCAL=false`, in
which case the job becomes `RETRY_PENDING`/`FAILED` — the watcher never
crashes because of a provider).

## Style presets

The content-understanding step picks one of three style presets from the
actual topic of your text:

* `CINEMATIC_MYSTICAL` — night skies, celestial glow, silhouettes, fog,
  light rays (spiritual/mystical content)
* `DARK_LUXURY` — near-black + gold, light shafts, bokeh (money/success)
* `CLEAN_MODERN` — fresh gradients, soft shapes, rings (tips/tech/everyday)

The enhanced prompt sent to cloud/ComfyUI providers combines your
`IMAGE_PROMPT`, the style descriptors, 9:16 composition hints and a
negative prompt that explicitly excludes text/watermarks — the hook is
always rendered programmatically afterwards (see `src/tta/typography.py`).

## ComfyUI

```
IMAGE_PROVIDER=comfyui         # or leave auto
COMFYUI_URL=http://127.0.0.1:8188
COMFYUI_WORKFLOW=path\to\workflow_api.json    # optional
```

Without `COMFYUI_WORKFLOW` a built-in SDXL txt2img workflow is used
(checkpoint `sd_xl_base_1.0.safetensors`). A custom workflow (export via
"Save (API format)" in ComfyUI) may contain the placeholders
`"{prompt}"`, `"{negative_prompt}"`, `"{width}"`, `"{height}"`, `"{seed}"`.

## Adding a provider

Subclass `tta.providers.base.ImageProvider`, implement `availability()`
and `generate()`, raise `ProviderError` on failure, give it an honest
`cost`, and register it in `tta/providers/registry.py:build_registry`.
