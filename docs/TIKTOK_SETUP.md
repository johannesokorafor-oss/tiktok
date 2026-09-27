# TikTok API setup

The app uses **only the official TikTok Content Posting API**
(https://developers.tiktok.com). No scraping, no private endpoints, no
browser automation.

## 1. Create a developer app

1. Register at https://developers.tiktok.com and create an app.
2. Add the **Content Posting API** product.
3. Request the scopes: `user.info.basic`, `video.upload`
   (`video.publish` additionally if you want direct posting).
4. Add a **Redirect URI** that exactly matches your `.env` value —
   default: `http://127.0.0.1:8765/callback/`.
5. Note the **Client key** and **Client secret**.

> Unaudited apps can use the *inbox upload* (draft/review) flow, but any
> content posted via *direct post* is restricted to private (`SELF_ONLY`)
> visibility until TikTok audits your app. The default `UPLOAD_MODE=inbox`
> is exactly the "upload → review in app → publish" flow this project is
> built around.

## 2. Configure

In `.env`:

```
TIKTOK_CLIENT_KEY=your_client_key
TIKTOK_CLIENT_SECRET=your_client_secret
TIKTOK_REDIRECT_URI=http://127.0.0.1:8765/callback/
UPLOAD_MODE=inbox
DRY_RUN=false
```

Never commit `.env`. It is gitignored.

## 3. Authorize once

```powershell
.venv\Scripts\python -m tta auth login
```

This starts a temporary local HTTP listener on the redirect URI, opens the
TikTok consent page, verifies the OAuth `state`, exchanges the code and
stores the tokens in `data/tiktok_tokens.json` (0600 permissions,
gitignored, never logged). Access tokens are refreshed automatically; the
refresh token lasts ~1 year of use.

Check status any time:

```powershell
.venv\Scripts\python -m tta auth status
```

## 4. Upload flow used

For every job (mode `inbox`, the default):

1. `POST /v2/post/publish/inbox/video/init/` with
   `source_info = {source: FILE_UPLOAD, video_size, chunk_size, total_chunk_count}`
2. `PUT` the file to the returned `upload_url` with
   `Content-Range: bytes start-end/total` (single chunk ≤ 64 MB, otherwise
   10 MB chunks, final chunk absorbs the remainder — per the API docs)
3. `POST /v2/post/publish/status/fetch/` until `SEND_TO_USER_INBOX`
4. You get a TikTok notification → open the app → review → edit → post.

Mode `direct` additionally queries
`POST /v2/post/publish/creator_info/query/`, validates the privacy level
against the creator's options (default `SELF_ONLY`) and sends `post_info`
including the caption and `video_cover_timestamp_ms`.

### About the cover

TikTok's API does **not** accept an arbitrary external image as a cover.
Therefore the generated cover is embedded as the real first ~500 ms of the
final video, and `video_cover_timestamp_ms` points at the middle of that
segment. In the inbox flow TikTok defaults the cover to the beginning of
the video, which is exactly where the cover frames are — and you can still
adjust it during in-app review.

## Rate limits & errors

* HTTP 429 / `rate_limit_exceeded` → automatic backoff honoring
  `Retry-After`, then job-level retry (`RETRY_PENDING`)
* `spam_risk_too_many_pending_share`: TikTok allows max 5 pending inbox
  uploads per user in 24 h — publish or discard pending drafts first
* token problems → run `tta auth login` again

## What cannot be tested without credentials

Everything up to and including the final MP4 + validation runs fully
offline (dry run). The actual OAuth consent and upload require your real
client key/secret and a logged-in TikTok user; request construction,
chunking, retry, rate-limit and token-refresh logic are covered by the
test suite with mocked HTTP.
