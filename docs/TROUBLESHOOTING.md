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

## Optional platforms

| Symptom | Meaning | Fix |
|---|---|---|
| YouTube `Authentication: MISSING` | no OAuth token | Connect YouTube in the dashboard (`access_type=offline` gives a refresh token) |
| YouTube upload rejected as public | the app refuses `public`/`unlisted` by design | keep `YOUTUBE_PRIVACY_STATUS=private` and change visibility in YouTube Studio |
| YouTube quota exceeded | `videos.insert` costs ~1600 units of the 10,000/day default | wait for the quota reset or request an increase through YouTube's audit/quota form |
| Spotify / Apple Music show `READY_FOR_DISTRIBUTION` | that is the final state: no upload API exists | submit the package to your distributor |
| SoundCloud back on `PREPARE_ONLY` | `SOUNDCLOUD_PRIVATE_FIELD_VERIFIED=false` | set it to `true` (default) to upload privately |
| A platform shows `DISABLED` | it is off (the default) | set `<PLATFORM>_ENABLED=true` and restart |
| `Vimeo ... Authentication: MISSING` | no token, or Vimeo upload access not approved | see [VIMEO_SETUP.md](VIMEO_SETUP.md) |
| Vimeo job fails with "not private" | your Vimeo account default privacy overrode the request | fix the account default; the app never publishes |
| Dailymotion `public refused` | `DAILYMOTION_VISIBILITY=public` | use `private` or `password` |
| SoundCloud stays `PREPARE_ONLY` | the private sharing field is unverified by design | verify it, then `SOUNDCLOUD_PRIVATE_FIELD_VERIFIED=true` |
| SoundCloud 401 after an hour | access tokens live ~1 h; refresh tokens are single use | the app refreshes automatically; reconnect if the refresh token was consumed elsewhere |
| Patreon shows "NOT DOCUMENTED IN CURRENT V2" | Patreon has no post-creation API | use `patreon_post.txt` + `patreon_video.mp4` |
| Rumble shows "NOT VERIFIED" | no official public VOD upload API | use `rumble_ready.mp4` + `rumble_metadata.txt` |
| One platform failed, others fine | intended: platforms are isolated in `job_platforms` | retry just that platform from the job view |

## Start over

```powershell
.\scripts\reset_state.ps1        # job history only
.\scripts\reset_state.ps1 -All   # also clears processing/output/failed
```

Your files in `input/` and your TikTok tokens are never touched by this.
