# Dailymotion (optional) — real API v2 upload, private visibility

| | |
|---|---|
| **Mode** | `UPLOAD_PRIVATE` — a genuine API upload |
| **Result** | a video object on your channel with `visibility=private` |
| **API** | Dailymotion **API v2 only** (the legacy API is never used) |
| **Account** | Dailymotion account with a **private API key** and a profile/channel id |
| **Scope** | `video.manage` |
| **Default** | `DAILYMOTION_ENABLED=false` |

## Current API flow (verified 2026-09-27, developers.dailymotion.com)

```
POST /oauth/token                       (scope=video.manage)
POST /v2/files/upload_sessions          → upload_url + progress_url
POST {upload_url}   multipart/form-data, field "file"   → file url
POST /v2/profiles/{profile_id}/videos
     {"title", "description", "category", "visibility": "private",
      "is_for_kids": false, "source": {"file_url": ...}}
GET  /v2/videos/{id}                    → verify visibility
```

## Honest wording — this is not a TikTok-style draft

Dailymotion's own documentation calls step 4 *"create and publish the video"*:
the video object is created on your channel at that moment. What the app
controls is **`visibility`**, i.e. *who may watch it*. With
`visibility=private` the video is not publicly listed and only you (or someone
with the private URL) can watch it — but it is **not** an invisible draft that
you later finish, the way TikTok's inbox works.

`visibility=public` is refused by this application, and the result is re-read
after creation: if Dailymotion reports `public`, the job is marked FAILED.

## Setup on Windows

1. Dailymotion Studio → **Organization → API keys → Create API key**
   (choose a **Private** key; the secret is shown once).
2. Note your **profile id** (channel id) — it appears in Studio and in the API.
3. `notepad .env`:

```ini
DAILYMOTION_ENABLED=true
DAILYMOTION_MODE=UPLOAD_PRIVATE
DAILYMOTION_CLIENT_ID=<api key>
DAILYMOTION_CLIENT_SECRET=<api secret>
DAILYMOTION_PROFILE_ID=<your profile/channel id>
DAILYMOTION_SCOPE=video.manage
DAILYMOTION_VISIBILITY=private
DAILYMOTION_CATEGORY=news
DAILYMOTION_IS_FOR_KIDS=false
```

If your key requires user credentials for the password grant, also set
`DAILYMOTION_USERNAME` / `DAILYMOTION_PASSWORD`; otherwise the client-credentials
grant is used.

4. `.\scripts\diagnose.ps1` → the Dailymotion line should show
   `Authentication: OK | ... visibility=private (public is refused)`.

## Limitations

* Standard accounts: max 2 GB / 60 min per video — the validator warns.
* `category` and `is_for_kids` are mandatory fields of the create call; adjust
  the category in `.env` to something appropriate for your content.
* Live upload has **not** been executed here (no network/credentials); it is
  covered by tests against a local fake of the documented v2 endpoints.
