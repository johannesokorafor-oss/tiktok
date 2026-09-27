# TikTok Draft Upload Automation (Windows-first)

Drop a **video** and a **text file with the same name** into `input/` — the
application detects the pair, derives title/caption/hashtags from your text,
prepares a TikTok-compatible MP4 and uploads it through the **official TikTok
Content Posting API** as a *draft in your inbox*. You then open TikTok, choose
your own thumbnail, review the caption and publish.

> **Thumbnail/cover creation is intentionally manual. The application uploads the video to TikTok and the user completes the cover selection and final editing inside TikTok.**
> No image APIs, no ComfyUI, no diffusion models, no image costs — and the app
> never modifies the beginning of your video to fake a cover.

```
input/
  mein-video.mp4
  mein-video.txt            <- TITLE / DESCRIPTION / (optional IMAGE_PROMPT)
        │
        ▼ automatic
  pair detected → parse → caption + hashtags → TikTok-compatible MP4 → validate → inbox draft
        │
        ▼
output/mein-video/{source.mp4, metadata.txt, final_tiktok.mp4, caption.txt, job.json, upload_result.json}
        │
        ▼ manual, in the TikTok app
  choose your own cover → review caption → Post
```
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
| Understand | offline deterministic analysis: language detection (German default), topic classification, TikTok title, caption cleanup, ≤5 relevant hashtags. `IMAGE_PROMPT` is optional and only carried through as metadata |
| Video | FFmpeg, least-invasive processing: **passthrough** when the file is already TikTok compatible, **remux** (stream copy) for other containers, **transcode** only for incompatible codecs or aspect ratios. Nothing is ever added to the video |
| Validation | ffprobe + full decode pass, streams, duration, fps, codecs, resolution, size |
| Upload | official `POST /v2/post/publish/inbox/video/init/` (scope `video.upload`) + chunked `PUT`, status polling, client-side rate limits (6/min init, 30/min status), exponential backoff |
| State | SQLite job/event history, retries, duplicate protection, crash/restart recovery of interrupted jobs, structured JSON logs with secret redaction |
| UI | local FastAPI dashboard: status, jobs, VIDEO READY / CAPTION READY / DRAFT UPLOAD steps, caption + Copy caption, errors, retry/force reprocess, caption+title overrides, auth status |

## Documentation

* **[docs/REAL_WINDOWS_TEST.md](docs/REAL_WINDOWS_TEST.md) — start here for a real Windows 11 test (step by step)**
* [docs/SETUP.md](docs/SETUP.md) — installation, FFmpeg, fonts, configuration
* [docs/TIKTOK_SETUP.md](docs/TIKTOK_SETUP.md) — developer app, scopes, OAuth, draft flow, exact payloads
* [docs/WORKFLOW.md](docs/WORKFLOW.md) — the full pipeline, folders, job states
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — failures and how to recover

## Safety defaults

* `DRY_RUN=true` — everything is produced locally, nothing is uploaded.
* **No cover generation at all** — no image APIs are contacted, no thumbnail
  files are produced, and your video's first frames are never replaced.
* Three explicit modes — `DRY_RUN` (default), `DRAFT_UPLOAD`, `DIRECT_POST`.
  `DIRECT_POST` additionally requires `CONFIRM_DIRECT_POST=true` and the
  `video.publish` scope, so the app can never drift into auto-publishing.
* `CONTENT_IS_AIGC=false` — only set this when the video itself is AI generated.
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

* `pytest tests` — see the final count in the test run below; the suite covers
  parser, language, caption/hashtags, pairing, stability, duplicates, state
  machine, restart recovery, least-invasive video processing, **video
  integrity (no cover frame, no added intro, no audio shift)**, ffprobe
  validation, TikTok request construction with mocked transports,
  OAuth/state/refresh, per-endpoint rate limiting, dashboard API and dry run.
* Real end-to-end **DRY_RUN** with the live watcher: a pair dropped into
  `input/` while the app was running is processed automatically with no CLI
  interaction and reaches `COMPLETED`.
* **Not tested here:** live TikTok upload — the build environment has no
  outbound internet and no TikTok credentials. The upload path is implemented
  against the current official documentation and covered by mocked-transport
  tests; your first `DRY_RUN=false` run on Windows is its first real execution.
