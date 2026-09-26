# Workflow

## Folders

```
input/        you drop video + txt here (never modified, never moved)
processing/   per-job scratch (background.png, final_tiktok.mp4, frame check)
output/       finished artifacts, one folder per video
failed/       JSON marker per permanently failed job
archive/      free for your own archiving
covers/       every generated cover (also served to the dashboard)
logs/         app.jsonl (structured) + console output
state/        jobs.sqlite3 and tiktok_tokens.json
```

`output/<video_name>/`

```
source.mp4            byte-identical copy of your input video (original untouched)
cover.png             1080x1920 final cover
final_tiktok.mp4      processed video with the cover baked in
caption.txt           cleaned caption + hashtags, ready to paste
metadata.txt          copy of your original text file
source_reference.txt  path + SHA-256 of the source video
job.json              plan, prompts, provider, scores, build + validation report
upload_result.json    publish_id, status, chunks, errors, timestamps
```

## Input text format

```
TITLE:
Die Zeichen, die deine Seele dir zeigen will

DESCRIPTION:
Eine mystische Reflexion über Intuition …

IMAGE_PROMPT:
Cinematic mystical night scene, solitary silhouette …
```

Also accepted: `Title:` / `Titel:` / `Beschreibung:` / `Caption:` / `Prompt:` /
`Bildprompt:` / `## Title`, inline values (`Title: …`), any case, and plain
free-form text (first line becomes the title, the rest the description).
Optional extra fields: `COVER_TEXT:` (force the hook) and `STYLE:`
(`CINEMATIC_MYSTICAL` / `DARK_LUXURY` / `CLEAN_MODERN`).

## Pipeline

1. **Discover** – video found; if the sidecar text is missing the job waits in
   `WAITING_FOR_PAIR`.
2. **Stabilise** – both files must have unchanged size/mtime for
   `STABILITY_SECONDS` and must not be locked by another process.
3. **Validate** – SHA-256 of both files, duplicate check, ffprobe on the source.
4. **Understand** – language, topic, hook (2–4 words, ideally 3, derived from
   your own words), caption, ≤5 hashtags, style preset, enhanced image prompt
   and negative prompt.
5. **Cover** – background from the provider chain → Pillow typography →
   validation (1080×1920, decodable, not blank, text inside the safe box).
6. **Build video** – FFmpeg overlays the cover on the first `COVER_FRAME_HOLD_MS`
   (default 120 ms). Duration and audio are unchanged; 9:16 sources are kept,
   near-9:16 sources are cropped, far-off aspect ratios get a blurred pad.
   Output: H.264 High + AAC 48 kHz, `+faststart`, keyframe forced at 0.
7. **Verify** (`VALIDATING_VIDEO`) – ffprobe + full decode pass, stream/duration/fps/codec/size
   checks, and the cover frame is extracted again and kept for inspection.
8. **Upload** – `DRY_RUN=true` stops here. Otherwise the inbox/draft flow runs
   and the status is polled and stored.
9. **Publish artifacts** – everything is copied into `output/<name>/`.

## Job states

```
DISCOVERED → WAITING_FOR_PAIR → VALIDATING → GENERATING_METADATA → GENERATING_IMAGE
          → BUILDING_VIDEO → VALIDATING_VIDEO → UPLOADING → UPLOADED → COMPLETED
                                     ↘ RETRY_PENDING ↘ FAILED      ↘ DUPLICATE
```

### Restart safety

SQLite is the authoritative store, so a crash, reboot or Ctrl+C never loses a
job. On startup `recover_interrupted()` moves every job that was left in a
working state (`VALIDATING`, `GENERATING_METADATA`, `GENERATING_IMAGE`,
`BUILDING_VIDEO`, `VALIDATING_VIDEO`, `UPLOADING`) back to `RETRY_PENDING`,
clears its stale lock, records a `RECOVERED_AFTER_RESTART` event, and the next
watcher pass finishes it. Finished, failed and waiting jobs are left alone.

Illegal transitions are rejected by the state machine. Every transition and
event is stored in `job_events` and logged:

```
2026-09-26T07:23:11 [JOB 88410d7068cc] DISCOVERED mystisches-video.mp4
2026-09-26T07:23:11 [JOB 88410d7068cc] VALIDATING mystisches-video.mp4
2026-09-26T07:23:11 [JOB 88410d7068cc] METADATA_READY lang=de hook='…' style=CINEMATIC_MYSTICAL
2026-09-26T07:23:05 [JOB 88410d7068cc] COVER_GENERATED mystisches-video_88410d7068cc.png
2026-09-26T07:23:11 [JOB 88410d7068cc] VIDEO_BUILT final_tiktok.mp4
2026-09-26T07:23:11 [JOB 88410d7068cc] TIKTOK_UPLOAD_STARTED mode=UPLOAD size=…
2026-09-26T07:23:11 [JOB 88410d7068cc] TIKTOK_DRAFT_READY publish_id=… status=SEND_TO_USER_INBOX
```

## Duplicate protection

The SHA-256 of the video is stored. A file that was already completed or
uploaded is marked `DUPLICATE` instead of being uploaded twice. Use
**Force reprocess** in the dashboard or `python -m app.main process <file>
--force` to override.

## Dashboard

`http://127.0.0.1:8765` — watcher status and folder, current and recent jobs,
state, cover preview, upload status/publish id, errors, retry and force
reprocess, open-output-folder, authentication status, provider health, and
overrides for hook / image prompt / style / caption (applied when you
regenerate). Automatic mode needs none of this.

## Retries and failures

Retryable stages (image generation, FFmpeg, upload/network/429) go to
`RETRY_PENDING` with exponential backoff, bounded by `TIKTOK_MAX_RETRIES`.
Everything else fails fast with the exact error stored on the job, a marker in
`failed/` and the source files untouched. Nothing is ever skipped silently.
