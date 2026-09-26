# Image providers

The cover is produced in two stages:

* **Stage A** – an AI (or local) model renders a *background only*, prompted
  explicitly to contain **no text**.
* **Stage B** – the application renders the 2–4 word hook with Pillow.
  Spelling, umlauts and layout are therefore always correct.

Only Stage A uses a provider. A provider failure never crashes the watcher:
the chain falls through to the next provider and the error is recorded on the
job.

## Classification

Every provider reports one of `FREE`, `FREE WITH QUOTA`, `PAID`, `LOCAL`.
The class is printed at startup, shown in the dashboard and in
`diagnose.ps1`. With `ALLOW_PAID_API=false` (default) no `PAID` provider is
ever called.

| Provider | Class | Status verified 2026-09-26 | Notes |
|---|---|---|---|
| `pollinations` | **FREE** | Public keyless endpoint `https://image.pollinations.ai/prompt/<prompt>`; core models (`flux`, `turbo`) are offered at no cost and without signup. Anonymous use is rate-limited under fair use (roughly one request every ~15 s reported for anonymous callers) and there is no SLA. Separate "premium" models (GPT Image, Seedream, Nano Banana) are credit-metered. | Default primary. The adapter refuses any model outside the verified free set unless `ALLOW_PAID_API=true`. An optional free registered token (`POLLINATIONS_TOKEN`) raises the limits. |
| `comfyui` | **LOCAL** | Runs on your own machine; no API fee. | Uses the documented ComfyUI HTTP API (`POST /prompt`, `GET /history/{id}`, `GET /view`). Bring your own checkpoint, or point `COMFYUI_WORKFLOW_FILE` at an exported *API format* workflow with `{{POSITIVE}} {{NEGATIVE}} {{WIDTH}} {{HEIGHT}} {{SEED}} {{STEPS}}` placeholders. |
| `automatic1111` | **LOCAL** | Runs on your own machine; no API fee. | Stable Diffusion WebUI started with `--api`, endpoint `/sdapi/v1/txt2img`. |
| `offline` | **LOCAL** | Pure Pillow rendering, no network at all. | Real procedural cinematic background (gradients, light shafts, particles, haze, silhouettes, vignette, grain) — not a placeholder, but visibly below diffusion quality. Last resort so the pipeline never dead-ends; disable with `ALLOW_OFFLINE_FALLBACK=false`. |
| `huggingface` | **FREE WITH QUOTA** | A free Hugging Face account includes a limited monthly Inference-Providers credit allowance; once it is used up requests fail (HTTP 402) unless you enable PRO / pay-as-you-go billing yourself. | Therefore **not** advertised as free. Requires `HUGGINGFACE_API_TOKEN` **and** an explicit `HUGGINGFACE_ACCEPT_QUOTA=true` (or `ALLOW_PAID_API=true`) before it will run. |

Sources for the cost statements are the providers' own current pricing/API
pages and 2026 provider round-ups; anything that could not be verified is not
described as free. If you add a provider adapter, set its `cost` and
`cost_note` honestly — that string is what the user sees.

## Selection order

```
IMAGE_PROVIDER=auto                     # or a single provider name
IMAGE_PROVIDER_PRIMARY=pollinations
IMAGE_PROVIDER_FALLBACK=comfyui,automatic1111,offline
```

`auto` builds the chain from PRIMARY → FALLBACK → remaining known providers,
skipping any that are not configured and any PAID provider while
`ALLOW_PAID_API=false`. The default order reflects: image quality → genuinely
free availability → reliability → resolution control → speed → zero cost.

## Timeouts, retries and fallback

Every provider call is bounded by `IMAGE_TIMEOUT_SECONDS` (default 180 s).
Transient failures — HTTP 429, timeouts, 5xx, connection/network errors — are
retried `IMAGE_MAX_RETRIES` times (default 2) with exponential backoff
(`IMAGE_BACKOFF_BASE_SECONDS`, capped at 30 s). Non-transient failures (bad
model, blocked paid provider, malformed response) are **not** retried: the
chain immediately moves to the next provider. If every provider fails, the job
goes to `RETRY_PENDING`/`FAILED` with the full per-attempt log stored in
`job.json → cover.attempts`; the watcher itself never crashes.

Example from a real run with no internet in the sandbox:

```
image provider pollinations failed (TLS/SSL connection closed); retrying in 2.0s
image provider pollinations failed (TLS/SSL connection closed); retrying in 4.0s
image provider comfyui failed (Connection refused); retrying in 2.0s
...
COVER_GENERATED mein-video_2236edb3b68e.png     <- offline renderer took over
```

## Prompt engineering

Your `IMAGE_PROMPT` is never sent raw. `app/content/analyzer.py` builds an
enhanced prompt from title, description, your prompt, detected language,
topic and style preset, specifying subject, environment, composition, camera,
lighting, colour, depth, emotional tone, realism, vertical 9:16 framing and an
explicit clean area in the top 45 % for the typography. A negative prompt
blocks text, letters, logos, watermarks, UI, extra limbs, malformed faces,
duplicates, cheap stock look and clutter.

## Quality modes

| Mode | Candidates | Behaviour |
|---|---|---|
| `FAST` | 1 | fewer diffusion steps where the provider supports it |
| `BALANCED` (default) | 1 | one high-quality image, no extra API calls |
| `HIGH_QUALITY` | 3 | three candidates scored deterministically and the best one selected |

Scoring (`app/images/registry.py:score_image`) combines global contrast,
edge detail, calmness of the typography region, focal strength of the lower
frame and highlight/shadow clipping. Selection is never random.

## Adding a provider

Implement `app/images/base.ImageProvider` (`name`, `cost`, `cost_note`,
`is_configured`, `generate`, optional `health`) and register the class in
`PROVIDER_CLASSES` in `app/images/registry.py`. Nothing else changes.
