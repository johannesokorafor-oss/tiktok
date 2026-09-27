# TikTok Auto-Poster

Windows-first local automation: drop a video + a text file into a watched
folder and get back a TikTok-ready upload with a premium, AI/procedurally
generated 9:16 cover — fully automatic, via the **official TikTok Content
Posting API** (upload/review draft flow, no auto-publish).

```
DROP FILES IN FOLDER  →  WAIT  →  OPEN TIKTOK  →  FINAL REVIEW / PUBLISH
```

## How it works

You place a pair into `input/`:

```
my_video.mp4     (or .mov / .mkv / .webm)
my_video.txt     (or .md)
```

The text file looks like this (case-insensitive, tolerant — see
[docs/WORKFLOW.md](docs/WORKFLOW.md)):

```
TITLE:
Die Reise beginnt in dir

DESCRIPTION:
Manchmal zeigt dir das Universum Zeichen ...

IMAGE_PROMPT:
Eine einsame Silhouette auf einem Berg unter leuchtendem Nachthimmel
```

The app then automatically:

1. detects the pair and **waits until both files are fully written**
   (size-stability + readability checks)
2. parses the text (`TITLE/DESCRIPTION/IMAGE_PROMPT`, `Title:/Prompt:`, …)
3. derives a short hook, a 2–4 word cover text, language (DE/EN), visual
   style (`CINEMATIC_MYSTICAL` / `DARK_LUXURY` / `CLEAN_MODERN`), an enhanced
   image prompt and a caption with hashtags — all from the actual content
4. generates a premium 1080×1920 background (provider system: local
   procedural renderer, free Pollinations cloud, local ComfyUI, optional
   paid providers — paid is hard-blocked unless `ALLOW_PAID_API=true`)
5. renders the hook **programmatically** with Pillow (umlauts, wrapping,
   adaptive size, shadow/stroke, safe margins — never left to the image model)
6. re-encodes the video to TikTok-ready 9:16 H.264/AAC (smart crop or
   blurred-pad, originals never modified)
7. embeds the cover as real leading video frames and computes the matching
   `video_cover_timestamp_ms`
8. validates the final MP4 with ffprobe
9. uploads through the official Content Posting API **inbox/draft flow**
   (you review and publish inside TikTok) — or skips uploading in dry run
10. persists every job in SQLite and keeps watching for the next pair

## Quick start (Windows)

```powershell
git clone <this repo>
cd tiktok
.\scripts\install.ps1      # venv + packages + FFmpeg check
.\scripts\setup.ps1        # .env + folders + diagnostics
.\scripts\start.ps1        # watcher + dashboard (http://127.0.0.1:8000)
```

Then drop `my_video.mp4` + `my_video.txt` into `input\`.

Everything runs in **dry-run mode by default** (`DRY_RUN=true`): the whole
pipeline executes locally but nothing is sent to TikTok. To upload for real:

1. follow [docs/TIKTOK_SETUP.md](docs/TIKTOK_SETUP.md) to create a TikTok
   developer app and put the credentials into `.env`
2. run `.venv\Scripts\python -m tta auth login`
3. set `DRY_RUN=false` in `.env` and restart

## CLI

```
tta start                 watcher + dashboard (the normal way to run)
tta watch                 watcher only
tta dashboard             read-only dashboard
tta process VIDEO TEXT    process a single pair right now (--force to
                          override duplicate protection)
tta retry JOB_ID          retry a failed/duplicate job
tta regen-cover JOB_ID    regenerate a job's cover
tta jobs                  list recent jobs
tta auth login|status|logout
tta diagnose              full system diagnostics
tta sample                create a demo pair in input/
```

(Use `.venv\Scripts\tta.exe` / `.venv/bin/tta` or `python -m tta`.)

## Dashboard

`tta start` serves a local dashboard on port 8000 showing watcher status,
provider/auth status, all jobs with cover previews, errors, and
retry / regenerate-cover buttons.

## Output

```
output/<name>-<jobid>/
  source.mp4            exact copy of your original video
  metadata.txt          copy of your text file
  background.png        generated art without text
  cover.png             final 1080x1920 cover with the hook rendered on it
  final_tiktok.mp4      the video that gets uploaded
  job.json              full job record incl. content plan + validation
  upload_result.json    TikTok publish_id/status, or the dry-run notice
```

Originals are moved (bytes untouched) to `archive/` on success or
`failed/` on failure. Duplicate content (SHA-256) is detected and **never
uploaded twice** unless you force it.

## Documentation

* [docs/SETUP.md](docs/SETUP.md) — installation in detail
* [docs/TIKTOK_SETUP.md](docs/TIKTOK_SETUP.md) — developer app, OAuth, scopes
* [docs/IMAGE_PROVIDER.md](docs/IMAGE_PROVIDER.md) — provider system & costs
* [docs/WORKFLOW.md](docs/WORKFLOW.md) — file formats, states, cover logic
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)

## Tests

```powershell
.\scripts\test.ps1        # or: python -m pytest tests/
```

108 tests cover parsing, pairing, stabilization, duplicates, the state
machine, typography, FFmpeg processing, cover-timestamp math, ffprobe
validation, OAuth/token handling, TikTok request construction, retries,
rate limits and the full dry-run pipeline (TikTok itself is mocked in
tests; everything else is real).

## Security

* secrets live in `.env` / `data/tiktok_tokens.json` — both gitignored
* tokens are stored with 0600 permissions and are redacted from all logs
* no scraping, no private endpoints — official API only
