# Troubleshooting

Start with:

```powershell
.\scripts\diagnose.ps1
```

It reports PASS/WARN/FAIL for Python, FFmpeg, ffprobe, filesystem permissions,
disk space, database, font, every image provider, TikTok credentials, OAuth,
scopes, connectivity and the active safety modes. Structured logs are in
`logs/app.jsonl`; every line carries the job id.

## Nothing happens when I drop files

* Both files must share the **base name**: `clip.mp4` + `clip.txt`.
* Supported video extensions: `.mp4 .mov .mkv .webm` (`VIDEO_EXTENSIONS`),
  metadata: `.txt .md`.
* The job shows `WAITING_FOR_PAIR` until the text file exists.
* Processing starts only after `STABILITY_SECONDS` of unchanged size/mtime and
  once the file is no longer locked — large copies over a network share can
  take a while.
* If the folder is on a network/virtual drive, filesystem events may not fire;
  the poll loop (`POLL_INTERVAL_SECONDS`) still picks the files up, or click
  **Scan now**.

## `metadata file could not be parsed`

The file was empty or contained no usable text. Any non-empty text works —
worst case the first line becomes the title. Check the encoding is UTF-8.

## `all image providers failed`

* `pollinations` unreachable → check internet/proxy; it is rate-limited for
  anonymous callers, so a 429 can simply mean "try again in a few seconds"
  (the job goes to `RETRY_PENDING` automatically).
* `comfyui` / `automatic1111` `not running` → start them, or remove them from
  `IMAGE_PROVIDER_FALLBACK`.
* `huggingface` blocked → it is FREE WITH QUOTA; set
  `HUGGINGFACE_ACCEPT_QUOTA=true` to spend your included credits.
* Keep `ALLOW_OFFLINE_FALLBACK=true` so a cover can always be produced
  locally; regenerate later from the dashboard for a better background.

## `No high-quality image provider is configured`

`QUALITY_MODE=HIGH_QUALITY` refuses to use the offline fallback renderer, so
the job fails instead of silently shipping a weaker cover. Either configure a
real provider (Pollinations reachable, a Hugging Face token, or a local
ComfyUI/AUTOMATIC1111), or set `DRY_RUN_ALLOW_OFFLINE=true` for dry runs, or
use `QUALITY_MODE=BALANCED`. `diagnose` shows exactly which providers were
checked and why each was unusable.

## Provider tier shows `UNKNOWN`

The provider's current pricing could not be verified at runtime (usually the
catalogue endpoint was unreachable). The app will still use it, but it will not
claim it is free. Verify the pricing yourself and set the tier explicitly (e.g.
`POLLINATIONS_TIER=FREE`), or set `ALLOW_UNKNOWN_TIER_PROVIDERS=false` to
refuse unverified providers.

## `generated cover failed validation`

The cover was blank, the wrong size or the headline left the safe box. Set a
shorter `COVER_TEXT:` / hook override, or pick another style preset. The
background that was used is kept in `processing/<name>_<job>/background.png`.

## `ffmpeg failed to build the video`

* Read the last FFmpeg lines in the job error and `logs/app.jsonl`.
* Corrupt or zero-length source: the job fails in `VALIDATING` with
  "source video is not readable".
* No FFmpeg on PATH: the static wheel is used automatically; set
  `FFMPEG_PATH`/`FFPROBE_PATH` if you need a specific build.

## `processed video failed validation`

Reported reasons include duration > 600 s, file > 4 GB, short side < 360 px,
missing audio although the source had audio, or a failed decode pass. Nothing
is uploaded in that case — fix the source and drop it again.

## TikTok errors

| Symptom | Cause / fix |
|---|---|
| `not authenticated with TikTok` | click **Connect** in the dashboard, or `python -m app.main auth login` |
| `OAuth state validation failed` | the authorisation link expired (10 min) or was reused — start again |
| `TikTok refresh token expired` | re-authenticate; refresh tokens last ~365 days |
| `scope_not_authorized` | the app or the user has not granted `video.upload` (or `video.publish` for DIRECT_POST). Re-authorise after changing scopes |
| HTTP 429 / `rate_limit_exceeded` | 6 init requests per minute per user token; the client backs off automatically |
| `spam_risk_too_many_posts` | TikTok's daily cap for API posts — wait and retry later |
| upload stalls / `upload_url` rejected | the upload URL is valid for one hour; retry the job to get a fresh one |
| post never appears | in UPLOAD mode it is a **draft in your TikTok inbox** — open the app and tap the notification |

## Caption or cover missing in TikTok

In the default UPLOAD/draft mode the documented inbox endpoint accepts no
caption and no cover field. The caption is in `output/<name>/caption.txt`
(and in the dashboard) to paste, and the cover is the first frame of the
uploaded video, so it is the default cover in the TikTok editor. Use
`TIKTOK_POST_MODE=DIRECT_POST` (scope `video.publish`, audit required) if you
want the caption and `video_cover_timestamp_ms` sent by the API.

## Duplicate detected

The same video content (SHA-256) already completed. Use **Force reprocess**
in the dashboard or `python -m app.main process <file> --force`.

## Start over

```powershell
.\scripts\reset_state.ps1        # job history only
.\scripts\reset_state.ps1 -All   # also clears processing/output/covers/failed
```

Your files in `input/` and your TikTok tokens are never touched by this.
