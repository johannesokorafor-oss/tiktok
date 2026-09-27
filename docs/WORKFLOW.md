# Workflow

## Folders

```
input/        you drop video + txt here (never modified, never moved)
processing/   per-job scratch (background.png, final_tiktok.mp4, frame check)
output/       finished artifacts, one folder per video
failed/       JSON marker per permanently failed job
archive/      free for your own archiving
logs/         app.jsonl (structured) + console output
state/        jobs.sqlite3 and tiktok_tokens.json
```

`output/<video_name>/`

```
source.mp4            byte-identical copy of your input video (original untouched)
final_tiktok.mp4      the file that is uploaded (your content; re-encoded only
                      when technically required)
caption.txt           cleaned caption + hashtags, ready to paste
metadata.txt          copy of your original text file
source_reference.txt  path + SHA-256 of the source video
job.json              plan, prompts, provider, scores, build + validation report
upload_result.json    publish_id, status, chunks, errors, timestamps

when Instagram PREPARE_ONLY was used on the job:
instagram_ready.mp4         file to upload manually (copy of final_tiktok.mp4
                            when it already meets the Reels requirements)
caption_instagram.txt       caption prepared for Instagram
instagram_preparation.json  status READY_FOR_MANUAL_UPLOAD, paths, requirements

when Instagram API_STAGE_ONLY is enabled:
caption_instagram.txt   caption prepared for Instagram
instagram.json          container id, status, account, Reels validation
instagram_status.json   short status record incl. 24 h expiry information
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
`IMAGE_PROMPT:` is optional. It is stored with the job metadata for your own
later use and never triggers any image generation.

## Modes

| Mode | What happens | How to select |
|---|---|---|
| `DRY_RUN` | video processing + validation + artifacts, **no upload** | `DRY_RUN=true` (default) |
| `DRAFT_UPLOAD` | official inbox/draft upload; you review and publish in TikTok | `DRY_RUN=false` (default `APP_MODE`) |
| `DIRECT_POST` | Direct Post API | `APP_MODE=DIRECT_POST` **and** `CONFIRM_DIRECT_POST=true` **and** the `video.publish` scope |

`DRY_RUN` always wins, and DIRECT_POST can never be reached by accident: if it
is requested without the confirmation flag or the scope, the app logs a mode
warning and uses `DRAFT_UPLOAD`. The effective mode is printed at startup, in
`diagnose` and in the dashboard header.

## Pipeline

1. **Discover** – video found; if the sidecar text is missing the job waits in
   `WAITING_FOR_PAIR`.
2. **Stabilise** – both files must have unchanged size/mtime for
   `STABILITY_SECONDS` and must not be locked by another process.
3. **Validate** – SHA-256 of both files, duplicate check, ffprobe on the source.
4. **Understand** – language, topic, TikTok title, caption style preset, enhanced image prompt
   and negative prompt.
5. **Prepare video** – the least invasive option is chosen:
   `passthrough` (already TikTok compatible → byte-identical copy),
   `remux` (stream copy into MP4 + faststart, no re-encode), or
   `transcode` (incompatible codec or aspect ratio → H.264 High + AAC 48 kHz).
   **Nothing is added to the video**: no cover frame, no intro, no overlay.
6. **Verify** (`VALIDATING_VIDEO`) – ffprobe + full decode pass, stream,
   duration, fps, codec, resolution and size checks.
7. **Platform: TikTok** – `DRY_RUN=true` stops here. Otherwise the inbox/draft flow runs
   and the status is polled and stored.
8. **Platform: Instagram** *(optional)* – in the default `PREPARE_ONLY` mode the
   automatic run makes **no Meta API call at all**; it only notes
   `INSTAGRAM_PREPARE_AVAILABLE`. You press **Prepare for Instagram** on a
   finished job to get `instagram_ready.mp4`, `caption_instagram.txt` and
   `instagram_preparation.json` for a manual upload.
   Only the opt-in `API_STAGE_ONLY` mode stages a media container
   (`INSTAGRAM_UPLOAD_STARTED` → `INSTAGRAM_READY_TO_PUBLISH`); it is never
   published, a container is **not** an Instagram app draft and it expires after
   24 h. Either way a problem here never affects the TikTok result: each
   platform has its own row in `job_platforms`.
9. **Publish artifacts** – everything is copied into `output/<name>/`.
10. **You finish manually** – TikTok: open the inbox notification, pick your own
    thumbnail, review `caption.txt`, press Post. Instagram: the container is
    staged only (see docs/INSTAGRAM_SETUP.md — it is *not* an app draft).

## Job states

```
DISCOVERED → WAITING_FOR_PAIR → VALIDATING → GENERATING_METADATA
          → BUILDING_VIDEO → VALIDATING_VIDEO → UPLOADING → UPLOADED → COMPLETED
                                     ↘ RETRY_PENDING ↘ FAILED      ↘ DUPLICATE
```

In `DRY_RUN` the upload states are replaced by a single `UPLOAD_SKIPPED` event.
`GENERATING_IMAGE` exists only as a legacy value so databases written by older
versions still load; the current pipeline never enters it.

> **Thumbnail/cover creation is intentionally manual.** The application uploads
> the video to TikTok and the user completes the cover selection and final
> editing inside TikTok.

### Platform records

Each platform has its own row in `job_platforms`
(`job_id, platform, status, external_id, started_at, completed_at, error,
metadata_json`) with the statuses `DISABLED`, `NOT_CONFIGURED`, `AUTH_REQUIRED`,
`UPLOADING`, `PROCESSING`, `READY_TO_PUBLISH`, `EXPIRED`, `FAILED`, `SKIPPED`.
Instagram state is never written into TikTok fields, and one platform failing
never destroys the other platform's result.

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
2026-09-27T07:23:11 [JOB 88410d7068cc] DISCOVERED mystisches-video.mp4
2026-09-27T07:23:11 [JOB 88410d7068cc] VALIDATING mystisches-video.mp4
2026-09-27T07:23:11 [JOB 88410d7068cc] METADATA_READY lang=de topic=spiritual hashtags=#seele #zeichen
2026-09-27T07:23:11 [JOB 88410d7068cc] VIDEO_READY final_tiktok.mp4 (passthrough)
2026-09-27T07:23:11 [JOB 88410d7068cc] TIKTOK_UPLOAD_STARTED mode=UPLOAD size=…
2026-09-27T07:23:11 [JOB 88410d7068cc] TIKTOK_DRAFT_READY publish_id=… status=SEND_TO_USER_INBOX
```

## Duplicate protection

The SHA-256 of the video is stored. A file that was already completed or
uploaded is marked `DUPLICATE` instead of being uploaded twice. Use
**Force reprocess** in the dashboard or `python -m app.main process <file>
--force` to override.

## Dashboard

`http://127.0.0.1:8765` — watcher status and folder, current and recent jobs,
state, upload status/publish id, errors, retry and force reprocess,
open-output-folder, authentication status, and
caption/title overrides (applied when you reprocess) and a **Copy caption**
button. It also states clearly that the thumbnail is your manual step in
TikTok. Automatic mode needs none of this.

## Retries and failures

Retryable stages (FFmpeg, upload/network/429) go to
`RETRY_PENDING` with exponential backoff, bounded by `TIKTOK_MAX_RETRIES`.
Everything else fails fast with the exact error stored on the job, a marker in
`failed/` and the source files untouched. Nothing is ever skipped silently.
