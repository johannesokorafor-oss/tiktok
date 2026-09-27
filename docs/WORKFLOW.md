# Workflow reference

## Input pairing

The watcher scans `input/` (poll interval `SCAN_INTERVAL`, default 2 s)
and pairs files by base name:

* video: `.mp4`, `.mov`, `.mkv`, `.webm`
* text: `.txt`, `.md`

`holiday.mp4` + `holiday.txt` → one job. A lone video waits up to
`PAIR_TIMEOUT` (default 10 min) for its text file, then fails and is moved
to `failed/`.

### Write-completion detection

A file is only processed when:

1. its size has not changed for `STABILIZE_SECONDS` (default 3 s), and
2. it is non-empty, and
3. it can actually be opened for reading (not locked by the copying app).

Slow copies (network shares, phone transfers) are therefore safe.

## Text format

Case-insensitive keys, `:` or `=`, values inline or on the following
lines, markdown headings/bold tolerated, German aliases accepted
(`Titel`, `Beschreibung`, `Bildprompt`). Recognized keys:

```
TITLE / Titel
DESCRIPTION / Beschreibung / Desc / Caption
IMAGE_PROMPT / Image Prompt / Prompt / Bildprompt / Cover_Prompt
```

If no keys are found at all: first non-empty line = title, rest =
description. Files are read-only — never modified.

## Content understanding

Fully local heuristics (no API calls):

* **language**: DE/EN marker words + umlaut frequency
* **keywords**: stopword-filtered frequency ranking
* **hook**: the title (or first sentence), trimmed to ≤ ~9 words
* **cover text**: 2–4 meaningful words from the title (topped up from
  keywords only when the title is too thin) — German for German material,
  English for English
* **style**: keyword buckets → `CINEMATIC_MYSTICAL` / `DARK_LUXURY` /
  `CLEAN_MODERN`
* **caption**: title + shortened description + content-derived hashtags

## Job states

```
DISCOVERED → VALIDATING → GENERATING_METADATA → GENERATING_IMAGE
→ BUILDING_VIDEO → VALIDATING_VIDEO → UPLOADING → UPLOADED → COMPLETED

WAITING_FOR_PAIR   video present, text missing (or vice versa)
DUPLICATE          same SHA-256 as an already-processed job
RETRY_PENDING      recoverable failure; retried automatically
                   (MAX_RETRIES, default 2)
FAILED             gave up; sources moved to failed/
```

State is persisted in `data/jobs.db` (SQLite). After a crash/restart,
jobs stuck in transient states are automatically requeued as
`RETRY_PENDING` — nothing is silently lost.

## Video processing

1. **Normalize** to 1080×1920, H.264 (crf 19) + AAC 128k 44.1 kHz stereo,
   30 fps, `yuv420p`, `+faststart`. Silent audio is injected when the
   source has none.
   * source already ~9:16 → plain scale
   * mild mismatch (< 12 % loss) → center crop
   * landscape/square → blurred-background pad (content preserved)
2. **Cover embedding**: the finished cover (background + typography) is
   prepended as real frames (`COVER_DURATION_MS`, default 500 ms) with
   silent audio, concatenated with uniform codec parameters (no audio
   glitch), and `video_cover_timestamp_ms` = middle of that segment,
   clamped against the final duration.
3. **Validation** (ffprobe): resolution, codecs, pixel format, duration,
   timestamp-inside-duration. A failed validation fails the job before
   any upload.

The original video file is never re-written; all processing happens on
copies inside the job's output folder.

## Duplicate protection

Every video is hashed (SHA-256). If the same content was already
COMPLETED/UPLOADED, the new job ends as `DUPLICATE` and nothing is
uploaded. Override with:

```
tta process video.mp4 video.txt --force
tta retry <job_id> --force        # or the dashboard's Retry button
```

## Dry run

`DRY_RUN=true` (default) runs *everything* — detection, parsing,
understanding, image generation, typography, video build, ffprobe
validation, persistence, archiving — and only skips the TikTok call,
writing `upload_result.json` with `"skipped": true`. This is the offline
end-to-end validation path.
