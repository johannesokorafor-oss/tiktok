# Troubleshooting

Always start with:

```powershell
.\scripts\diagnose.ps1
```

It checks Python, packages, ffmpeg/ffprobe, fonts, directories, database,
image providers, TikTok configuration, internet reachability and disk
space.

## Common problems

### "ffmpeg not found" / "ffprobe not found"
Install FFmpeg (`winget install Gyan.FFmpeg`) and re-open the terminal, or
set explicit paths in `.env`:
```
FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
FFPROBE_PATH=C:\ffmpeg\bin\ffprobe.exe
```

### Files sit in input/ and nothing happens
* Is the watcher running? (`.\scripts\start.ps1`, check dashboard)
* Do both files share the same base name (`clip.mp4` + `clip.txt`)?
* Is the extension supported? (`.mp4 .mov .mkv .webm` / `.txt .md`)
* A lone video shows up under "Waiting for pair" on the dashboard until
  its text file arrives (`PAIR_TIMEOUT`, default 10 min).
* Still being copied? Processing starts only after the size has been
  stable for `STABILIZE_SECONDS`.

### Job state DUPLICATE
The exact same video bytes were already processed. That is intentional —
use the dashboard Retry button (forces reprocess) or
`tta retry <job_id> --force`.

### Job FAILED with "image generation failed"
The selected cloud/ComfyUI provider was unreachable and local fallback was
disabled. Set `IMAGE_FALLBACK_TO_LOCAL=true` (default) or fix the
provider; then Retry.

### Upload errors
* `not authenticated` → `python -m tta auth login`
* `access_token_invalid` → login again (refresh token expired/revoked)
* `spam_risk_too_many_pending_share` → TikTok allows max 5 pending inbox
  uploads per 24 h; publish or discard pending drafts in the app
* `url_ownership_unverified` → only relevant for PULL_FROM_URL, which this
  app does not use
* rate limits (HTTP 429) are retried automatically with backoff

### OAuth redirect fails
`TIKTOK_REDIRECT_URI` in `.env` must match the URI registered in the
developer portal **exactly** (scheme, host, port, trailing slash).
`http://127.0.0.1:8765/callback/` must be free — close anything else
listening on port 8765, or change the port in both places.

### Dashboard unreachable
Port 8000 taken? Set `DASHBOARD_PORT` in `.env`. The read-only
`tta dashboard` cannot retry jobs — use `tta start`.

### Cover text looks wrong / too long
Cover text is derived from your TITLE. Make the title carry the 2–4 words
you want on the cover, or regenerate with `tta regen-cover <job_id>` after
editing nothing but luck (new art seed). The hook itself can be changed by
editing the text file and re-processing with `--force`.

### Restart lost nothing?
Correct — jobs interrupted mid-flight are requeued as RETRY_PENDING on the
next start. Check `tta jobs`.

### Logs
`logs/tta.log` (rotating). Tokens/secrets are automatically redacted.
