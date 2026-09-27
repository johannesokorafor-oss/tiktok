# Vimeo (optional) — real API upload, always private

| | |
|---|---|
| **Mode** | `UPLOAD_PRIVATE` — a genuine API upload |
| **Result** | the video exists on your Vimeo account with `privacy.view=nobody`; **not public** |
| **API** | official Vimeo API, resumable **tus** upload |
| **Account** | any Vimeo account, but the **app/account needs upload access approval** |
| **Scopes** | `upload`, `edit` (`private` is useful too) |
| **Default** | `VIMEO_ENABLED=false` |

## Current API flow (verified 2026-09-27, developer.vimeo.com)

```
POST https://api.vimeo.com/me/videos
  Authorization: bearer <token>
  Content-Type: application/json
  Accept: application/vnd.vimeo.*+json;version=3.4
  {"upload": {"approach": "tus", "size": <bytes>},
   "name": ..., "description": ..., "privacy": {"view": "nobody"}}
→ uri, link, upload.upload_link

PATCH {upload.upload_link}          (repeat / resume)
  Tus-Resumable: 1.0.0
  Upload-Offset: <offset>
  Content-Type: application/offset+octet-stream

HEAD {upload.upload_link}           → Upload-Offset == Upload-Length when done
GET  /videos/{id}                   → verify privacy.view
```

The app uploads in chunks (`VIMEO_CHUNK_SIZE`, default 32 MB), resumes from the
offset Vimeo reports after an interruption, and verifies the transferred length
before reporting success. Afterwards it re-reads the video and **fails the job
if Vimeo reports a public privacy value**.

## Upload access approval

> Vimeo requires API **upload access** to be requested for your application.
> Free/basic accounts and unapproved apps get `403` on `POST /me/videos`.

The diagnostics say this explicitly instead of pretending it is a bug:

```
[WARN] Vimeo  Enabled: YES | Mode: UPLOAD_PRIVATE | Authentication: MISSING |
       Required API access: Vimeo upload access approval + token scopes: upload, edit
```

Vimeo also enforces weekly/total storage quotas — an upload can still be
rejected when the quota is used up.

## Setup on Windows

1. Create an app at <https://developer.vimeo.com/apps>.
2. Request **upload access** for that app (Vimeo review).
3. Generate a personal access token with the scopes `upload edit private`
   (or complete the OAuth flow and paste the resulting token).
4. `notepad .env`:

```ini
VIMEO_ENABLED=true
VIMEO_MODE=UPLOAD_PRIVATE
VIMEO_ACCESS_TOKEN=<your token>
VIMEO_PRIVACY_VIEW=nobody
```

5. `.\scripts\diagnose.ps1` → the Vimeo line should show `Authentication: OK`.
6. Dashboard → **Upload Private to Vimeo** on a finished job, or let an enabled
   Vimeo run automatically after the TikTok stage (`DRY_RUN=false`).

## Limitations

* `VIMEO_PRIVACY_VIEW` accepts only non-public values (`nobody`, `password`,
  `disable`). `anybody`, `unlisted` and `users` are **refused** by the app.
* Your Vimeo account's *default privacy* settings can still override things on
  Vimeo's side; the app therefore re-reads and reports the real value.
* Live upload has **not** been executed from the build environment (no network,
  no credentials) — it is covered by tests against a local fake tus server.
