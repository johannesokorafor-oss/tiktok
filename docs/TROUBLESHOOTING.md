# Troubleshooting

Start with:

```powershell
.\scripts\diagnose.ps1
```

It reports PASS/WARN/FAIL for Python, FFmpeg, ffprobe, filesystem permissions,
disk space, the watched folder, the database, TikTok credentials, OAuth,
scopes, connectivity and the active safety modes. No image provider is needed
or checked any more. Structured logs are in
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

Expected. The documented inbox/draft endpoint accepts neither a caption nor a
cover, and this application deliberately generates no cover at all: you choose
the thumbnail in the TikTok editor and paste the caption from
`output/<name>/caption.txt` (or the dashboard **Copy caption** button).

## Duplicate detected

The same video content (SHA-256) already completed. Use **Force reprocess**
in the dashboard or `python -m app.main process <file> --force`.

## Start over

```powershell
.\scripts\reset_state.ps1        # job history only
.\scripts\reset_state.ps1 -All   # also clears processing/output/failed
```

Your files in `input/` and your TikTok tokens are never touched by this.
