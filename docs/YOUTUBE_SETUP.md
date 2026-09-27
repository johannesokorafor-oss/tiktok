# YouTube (optional) — real API upload, always private

| | |
|---|---|
| **Mode** | `UPLOAD_PRIVATE` — a genuine YouTube Data API v3 upload |
| **Result** | the video exists on your channel with `status.privacyStatus=private` |
| **API** | `videos.insert` (`part=snippet,status`) with Google's **resumable upload** protocol |
| **Account** | Google account + Google Cloud project with YouTube Data API v3 enabled |
| **Scope** | `https://www.googleapis.com/auth/youtube.upload` |
| **Default** | `YOUTUBE_ENABLED=false` |
| **Thumbnail** | **selected manually by the user in YouTube Studio** — never generated or uploaded here |

## Current API flow (verified 2026-09-27, developers.google.com/youtube/v3)

```
POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status
  Authorization: Bearer <token>
  Content-Type: application/json; charset=UTF-8
  X-Upload-Content-Type: video/mp4
  X-Upload-Content-Length: <bytes>
  {"snippet": {"title", "description", "tags", "categoryId", "defaultLanguage"},
   "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": false}}
→ 200 + Location: <session URI>          (session valid for one week)

PUT <session URI>
  Content-Length: <chunk>
  Content-Range: bytes a-b/total          (intermediate chunks = multiples of 256 KB)
→ 308 while incomplete (Range header), 200/201 with the video resource when done

PUT <session URI>  Content-Range: bytes */total      → resume after an interruption
GET  /youtube/v3/videos?part=status&id=<id>          → verify privacyStatus
```

The app chunks at `YOUTUBE_CHUNK_SIZE` (rounded down to a 256 KB multiple),
resumes from the offset YouTube reports, retries 429/5xx with bounded
exponential backoff and honours `Retry-After`, then **re-reads the video** and
fails the job if YouTube reports anything other than a private video.

## Privacy: private only, by design

`YOUTUBE_PRIVACY_STATUS` accepts **only `private`**. `public` and `unlisted`
raise `PublishingNotAllowed` before any request is made (test-enforced). You
change the visibility yourself in YouTube Studio after reviewing the upload.

> **Audit note.** YouTube documents: *"All videos uploaded via the
> videos.insert endpoint from unverified API projects created after 28 July
> 2020 will be restricted to private viewing mode."* This application treats
> that as a safety advantage and never attempts to work around the audit.
> Diagnostics print:
>
> ```
> YouTube  Enabled: YES | Mode: UPLOAD_PRIVATE | API project: unknown |
>          Automatic public publishing: DISABLED | privacyStatus=private
> ```
> Set `YOUTUBE_API_PROJECT_AUDITED=audited|unaudited|unknown` for your own records.

## Setup on Windows

1. <https://console.cloud.google.com/> → create a project → enable
   **YouTube Data API v3**.
2. OAuth consent screen → add the scope
   `https://www.googleapis.com/auth/youtube.upload`.
3. Credentials → **OAuth client ID** (type: *Web application*) → add the
   redirect URI `http://localhost:8765/youtube/callback`.
4. `notepad .env`:

```ini
YOUTUBE_ENABLED=true
YOUTUBE_MODE=UPLOAD_PRIVATE
YOUTUBE_CLIENT_ID=<client id>
YOUTUBE_CLIENT_SECRET=<client secret>
YOUTUBE_REDIRECT_URI=http://localhost:8765/youtube/callback
YOUTUBE_PRIVACY_STATUS=private
YOUTUBE_CATEGORY_ID=22
YOUTUBE_MADE_FOR_KIDS=false
```

5. `.\scripts\start.ps1` → dashboard → **Connect YouTube** → approve.
   Tokens land in `state/youtube_tokens.json` (git-ignored, never logged) and
   are refreshed automatically (`access_type=offline`).
6. `.\scripts\diagnose.ps1` → the YouTube line should show your channel.

## Metadata mapping

| Your `.txt` | YouTube field | Limit applied |
|---|---|---|
| `TITLE:` | `snippet.title` | 100 characters |
| `DESCRIPTION:` (cleaned caption) | `snippet.description` | 5000 characters |
| derived hashtags | `snippet.tags` | 500 characters total |
| detected language | `snippet.defaultLanguage` / `defaultAudioLanguage` | — |
| — | `snippet.categoryId` | `YOUTUBE_CATEGORY_ID` (default 22) |

`final_tiktok.mp4` is uploaded as-is; no YouTube-specific copy is created and
the video is never modified for a thumbnail.

## Limitations

* `videos.insert` costs ~1600 quota units; the default 10,000/day allows ~6
  uploads. Quota increases need YouTube's audit/quota form.
* Channels that are not verified are limited to 15-minute videos.
* Live upload has **not** been executed from the build environment (no network,
  no credentials) — it is covered by tests against a local fake implementing
  the documented resumable protocol.
