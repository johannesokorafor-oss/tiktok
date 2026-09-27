# TikTok Cover & Upload Automation (Windows-first)

Drop a **video** and a **text file with the same name** into `input/` — the
application detects the pair, understands the content, generates a premium
9:16 cover, bakes that cover into the video as an early frame, validates
everything and uploads the result to TikTok through the **official Content
Posting API** as a *reviewable draft*. You open TikTok, check the post and
publish it yourself. Nothing is published automatically.

```
input/
  mystisches-video.mp4
  mystisches-video.txt      <- TITLE / DESCRIPTION / IMAGE_PROMPT
        │
        ▼ automatic
  parse → analyse → AI background → typography → FFmpeg → validate → TikTok inbox draft
        │
        ▼
output/mystisches-video/{cover.png, final_tiktok.mp4, caption.txt, job.json, upload_result.json}
```

![example cover](examples/example_cover.png)

*(the cover above was produced by the offline local renderer during the
end-to-end dry run in a sandbox without internet; with the default
Pollinations provider the background is a diffusion-model image — the
typography layer is identical)*

---

## Quick start (Windows)

```powershell
.\scripts\install.ps1      # venv + dependencies
.\scripts\setup.ps1        # creates .env, runs diagnostics
notepad .env               # add TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET
.\scripts\diagnose.ps1     # PASS / WARN / FAIL report
.\scripts\start.ps1        # watcher + dashboard at http://127.0.0.1:8765
```

Then click **Connect** in the dashboard once to authorise your TikTok account,
set `DRY_RUN=false` in `.env` when you are happy with the dry-run output, and
just keep dropping files into `input/`.

Other scripts: `stop.ps1`, `test.ps1`, `reset_state.ps1`.
On Linux/macOS use the same commands via `python -m app.main <run|scan|process|auth|diagnose|reset-state>`.

---

## What the application actually does

| Stage | Implementation |
|---|---|
| Watch folder | `watchdog` + polling reconciler, recursive or flat, SHA-256 de-duplication, file-stability gate (size/mtime settled **and** file no longer locked) |
| Parse text | tolerant parser: `TITLE/DESCRIPTION/IMAGE_PROMPT`, German aliases (`Titel/Beschreibung/Bildprompt`), markdown headings, inline values, and a fallback for free-form text. The source file is never modified |
| Understand | offline deterministic analysis: language detection (German default), topic classification, 2–4 word hook derived from the actual words in your text, caption cleanup, ≤5 relevant hashtags, style preset selection |
| Background image | provider chain with pre-flight + fallbacks (Pollinations → ComfyUI → AUTOMATIC1111 → offline renderer → Hugging Face), **runtime-resolved cost tiers** (FREE / FREE WITH QUOTA / PAID / LOCAL / UNKNOWN), engineered content-specific prompts, deterministic candidate scoring in `HIGH_QUALITY` |
| Typography | 100 % rendered by the app with Pillow — adaptive font size, wrapping, TikTok-safe margins, scrim/glow/stroke based on measured contrast, umlaut-safe |
| Video | FFmpeg: cover overlaid on the first ~120 ms (no added duration, no audio shift), 9:16 preserved / cinematic crop / blurred pad, H.264 + AAC, faststart |
| Validation | cover: dimensions, format, corruption, blankness, text inside safe box. video: ffprobe + full decode pass, streams, duration, fps, codecs, size |
| Upload | official `POST /v2/post/publish/inbox/video/init/` (scope `video.upload`) + chunked `PUT`, status polling, client-side rate limits (6/min init, 30/min status), exponential backoff |
| State | SQLite job/event history, retries, duplicate protection, crash/restart recovery of interrupted jobs, structured JSON logs with secret redaction |
| UI | local FastAPI dashboard: status, jobs, cover preview, errors, retry/force-reprocess, overrides for hook/prompt/style/caption, auth + provider status |

## Documentation

* [docs/SETUP.md](docs/SETUP.md) — installation, FFmpeg, fonts, configuration
* [docs/TIKTOK_SETUP.md](docs/TIKTOK_SETUP.md) — developer app, scopes, OAuth, draft flow, exact payloads
* [docs/IMAGE_PROVIDER.md](docs/IMAGE_PROVIDER.md) — LOCAL vs FREE CLOUD vs PAID CLOUD, verified pricing
* [docs/WORKFLOW.md](docs/WORKFLOW.md) — the full pipeline, folders, job states
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — failures and how to recover

## Safety defaults

* `DRY_RUN=true` — everything is produced locally, nothing is uploaded.
* `ALLOW_PAID_API=false` — no paid image API can be called; the startup banner
  and the dashboard always show each provider as FREE / FREE WITH QUOTA / PAID / LOCAL.
* Three explicit modes — `DRY_RUN` (default), `DRAFT_UPLOAD`, `DIRECT_POST`.
  `DIRECT_POST` additionally requires `CONFIRM_DIRECT_POST=true` and the
  `video.publish` scope, so the app can never drift into auto-publishing.
* No silent quality downgrade — every cover is labelled `AI_MODEL_GENERATED`
  or `OFFLINE_FALLBACK_GENERATED`, and `HIGH_QUALITY` fails rather than
  quietly using the offline renderer.
* No provider is called FREE without runtime verification; unverifiable
  pricing is reported as `UNKNOWN`.
* `CONTENT_IS_AIGC=false` — an AI-generated *cover* does not make your video
  AI-generated content; only set this when the video itself is AI generated.
* Secrets live in `.env` / `state/tiktok_tokens.json`, are git-ignored and are
  redacted in logs and API responses.

## Testing

```powershell
.\scripts\test.ps1          # 137 offline tests, never touches TikTok
python tests/fixtures/make_fixtures.py   # (re)build the local video fixture
```

`tests/test_end_to_end.py` runs the **real** pipeline against
`tests/fixtures/example.mp4` (generated with FFmpeg, no asset needed from you)
and writes inspectable artifacts to `tests/output/<name>/`.

## Verified state of this branch

* `pytest tests` — **137 passed** (parser, language, hook, prompt building,
  pairing, stability, duplicates, state machine incl. `VALIDATING_VIDEO`,
  restart recovery, provider registry/retry/fallback, local HTTP provider
  roundtrip, cover typography/presets/validation, FFmpeg build + cover-frame
  verification + ffprobe, TikTok request construction with mocked transports,
  OAuth/state/refresh, per-endpoint rate limiting, dashboard API, dry run,
  provider tier resolution, mode resolution, cover-embedding quality).
* `python -m app.main diagnose` — full report produced (see docs).
* Real end-to-end **DRY_RUN** with the live watcher running: two pairs dropped
  into `input/` while the app was running were processed automatically with no
  CLI interaction (German 1080×1920 clip → `INTUITION WIEDERKEHRENDE MUSTER`,
  English 1920×1080 clip → `SIGNS YOUR SOUL`, converted to 9:16), both
  `COMPLETED` with full artifacts; dashboard override + regenerate produced a
  `DARK_LUXURY` cover reading `NOTHING IS COINCIDENCE`.
* **Not tested here:** live TikTok upload and live cloud image generation — the
  build environment has no outbound internet and no TikTok credentials were
  provided. Both paths are implemented against the current official
  documentation and are covered by mocked-transport tests; run
  `.\scripts\diagnose.ps1` and a first `DRY_RUN=false` job on your machine to
  confirm.
