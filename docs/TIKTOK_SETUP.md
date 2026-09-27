# TikTok setup (official Content Posting API only)

This application uses **only** documented TikTok for Developers endpoints.
No scraping, no browser automation, no private endpoints.
Documentation was re-checked on **2026-09-26**; the endpoints, scopes and
limits below are what the app implements.

> **First time on Windows?** Follow [REAL_WINDOWS_TEST.md](REAL_WINDOWS_TEST.md)
> — it walks through this file's steps 1–2 with copy-pasteable commands.

## 0. Checklist of what you will need

| Item | Where it comes from | Goes into |
|---|---|---|
| TikTok account | the account you want to post from | used during OAuth |
| Developer account | <https://developers.tiktok.com/> (log in with that TikTok account) | — |
| App with **Content Posting API** | developer portal → Manage apps | — |
| **Client key** | app details page | `TIKTOK_CLIENT_KEY` |
| **Client secret** | app details page (keep private) | `TIKTOK_CLIENT_SECRET` |
| Redirect URI | you enter it in the portal | `TIKTOK_REDIRECT_URI` |
| Scopes `user.info.basic`, `video.upload` | requested in the portal, approved at consent | `TIKTOK_SCOPES` |

## 1. Create the developer app

1. Go to <https://developers.tiktok.com/> and log in with the TikTok account
   you want to upload to.
2. Open **Manage apps** and create/connect an app (name, description, icon).
3. Add the product **Content Posting API** to that app.
4. Request the scopes you need:
   * `video.upload` — *"Share content to creator's account as a draft to
     further edit and post in TikTok"* → **this is the default mode of this
     application**.
   * `user.info.basic` — recommended, used for account identification.
   * `video.publish` — only if you want `TIKTOK_POST_MODE=DIRECT_POST`
     (direct posting; requires TikTok's app audit).
5. Add the **Redirect URI** exactly as in your `.env` (character for
   character, including the trailing slash-free path and the port):
   `http://localhost:8765/tiktok/callback`
   This is the local callback served by this application's dashboard while it
   is running — nothing needs to be publicly reachable for the draft flow.
6. Copy the **Client key** and **Client secret** into `.env`:

```
TIKTOK_CLIENT_KEY=...
TIKTOK_CLIENT_SECRET=...
TIKTOK_REDIRECT_URI=http://localhost:8765/tiktok/callback
TIKTOK_SCOPES=user.info.basic,video.upload
```

Never commit these. `.env`, `tokens/`, `credentials/` and
`state/tiktok_tokens.json` are git-ignored.

> **Unaudited apps:** until TikTok audits your app, posts created through
> Direct Post are forced to private visibility. The inbox/draft flow used by
> default is unaffected by that restriction because *you* finish the post
> inside the TikTok app.

## 2. OAuth

Authorisation code flow (v2):

| Step | Endpoint |
|---|---|
| Authorise | `GET https://www.tiktok.com/v2/auth/authorize/?client_key=…&scope=…&response_type=code&redirect_uri=…&state=…` |
| Token | `POST https://open.tiktokapis.com/v2/oauth/token/` (form-encoded, `grant_type=authorization_code`) |
| Refresh | same endpoint, `grant_type=refresh_token` |
| Revoke | `POST https://open.tiktokapis.com/v2/oauth/revoke/` |

* Access tokens expire after ~24 h, refresh tokens after ~365 days.
* TikTok **rotates the refresh token** on every refresh — the app always
  persists the new value (`state/tiktok_tokens.json`, user-only permissions).
* The local callback validates a single-use CSRF `state` value.
* Tokens are never written to logs; log output and API responses are redacted.

### First authorization (one time)

1. Start the app (`.\scripts\start.ps1`) — the callback URL only works while
   it runs.
2. Open <http://127.0.0.1:8765> and press **Connect**.
3. TikTok asks you to log in and shows the **consent screen** listing exactly
   the scopes your app requested (`user.info.basic`, `video.upload`). Approve.
4. TikTok redirects to `http://localhost:8765/tiktok/callback?code=…&state=…`;
   the app validates the single-use `state`, exchanges the code for tokens and
   shows **"TikTok connected"**.
5. The dashboard header switches to `tiktok connected`. Tokens are written to
   `state/tiktok_tokens.json` (git-ignored, user-only permissions) and are
   refreshed automatically before they expire.

Connect from the dashboard (**Connect** button) or:

```
python -m app.main auth login      # prints the authorisation URL
python -m app.main auth status
python -m app.main auth refresh
python -m app.main auth logout
```

## 3. What the app sends (UPLOAD / draft mode — the default)

```
POST https://open.tiktokapis.com/v2/post/publish/inbox/video/init/
Authorization: Bearer <access token>
Content-Type: application/json; charset=UTF-8

{"source_info": {"source": "FILE_UPLOAD",
                 "video_size": <bytes>,
                 "chunk_size": <bytes>,
                 "total_chunk_count": <n>}}
```

Response → `data.publish_id`, `data.upload_url`. The file is then transferred:

```
PUT <upload_url>
Content-Type: video/mp4
Content-Length: <chunk bytes>
Content-Range: bytes <first>-<last>/<total>
```

Chunk rules implemented: 5 MB–64 MB per chunk, final chunk up to 128 MB,
max 1000 chunks, files below 5 MB are sent as a single chunk. The upload URL
is valid for one hour.

Finally the app polls:

```
POST https://open.tiktokapis.com/v2/post/publish/status/fetch/
{"publish_id": "..."}
```

and stores the status (e.g. `SEND_TO_USER_INBOX`), the publish id, all errors
and timestamps in SQLite and in `output/<name>/upload_result.json`.

**The video lands in your TikTok inbox as a draft.** Open the TikTok app,
tap the notification, review it, adjust the caption/cover if you want and
press *Post*. This is exactly the "user does the final click" workflow.

### Caption and cover in draft mode

> **The Upload/Draft endpoint does not accept a separate cover image.** The
> generated cover is embedded into the video so it can be selected as the
> TikTok cover during the TikTok editing flow.


The documented inbox endpoint accepts **only** `source_info` — it takes no
`title`, `privacy_level` or `video_cover_timestamp_ms`. Therefore:

* The generated caption is **not** sent to TikTok in this mode. It is written
  to `output/<name>/caption.txt` and shown in the dashboard, ready to paste
  when you finish the post.
* The cover cannot be uploaded as a standalone PNG either. Instead the cover
  is **baked into the video** as the very first frame (default 120 ms overlay,
  no added duration, audio untouched), so it is the frame TikTok shows by
  default in the editor and the one you get by leaving the cover slider at the
  start.

## 4. Optional: DIRECT_POST mode

Set `TIKTOK_POST_MODE=DIRECT_POST` and `TIKTOK_SCOPES=user.info.basic,video.publish`.
The app then:

1. calls `POST /v2/post/publish/creator_info/query/` and uses a privacy level
   the creator actually allows (prefers `SELF_ONLY`, i.e. private, so the post
   still stays reviewable),
2. calls `POST /v2/post/publish/video/init/` with

```json
{"post_info": {"title": "<caption>", "privacy_level": "SELF_ONLY",
               "disable_duet": false, "disable_comment": false,
               "disable_stitch": false, "video_cover_timestamp_ms": 60},
 "source_info": {"source": "FILE_UPLOAD", "...": "..."}}
```

`video_cover_timestamp_ms` points at the middle of the baked-in cover frame,
which is how the documented API selects a cover (a frame of the video, not an
uploaded image).

`is_aigc: true` is added **only** when `CONTENT_IS_AIGC=true`. An AI-generated
thumbnail does not make your video AI-generated content, so the default is
`false` and the flag is never set automatically.

## 5. Rate limits and retries (enforced per endpoint, client-side)

Each endpoint has **its own** documented limit and its own client-side limiter
(`RATE_LIMITS` in `app/tiktok/client.py`) — no single global number is used:

| Operation | Endpoint | Documented limit (per user access token) |
|---|---|---|
| Draft/inbox init | `POST /v2/post/publish/inbox/video/init/` | 6 requests / minute |
| Direct Post init | `POST /v2/post/publish/video/init/` | 6 requests / minute |
| Status polling | `POST /v2/post/publish/status/fetch/` | 30 requests / minute |
| Creator info | `POST /v2/post/publish/creator_info/query/` | 20 requests / minute |
| Pending posts | (account level) | max 5 unposted draft shares per 24 h |
| Daily posts | (account level) | TikTok-side cap, surfaces as `spam_risk_too_many_posts` |

Unknown endpoints fall back to the most conservative limit (6/min). TikTok can
change these numbers, so the client additionally honours HTTP 429 and
`Retry-After` and applies bounded exponential backoff (`TIKTOK_MAX_RETRIES`,
default 5 — never an unlimited retry loop).

Plus a daily per-account posting cap enforced by TikTok
(`spam_risk_too_many_posts`). The client throttles locally, retries HTTP 429
and 5xx with exponential backoff (`TIKTOK_MAX_RETRIES`, default 5), honours
`Retry-After`, and never retries non-retryable errors such as
`scope_not_authorized`.

## 6. Media requirements the app enforces before uploading

MP4/MOV/WebM, ≤ 4 GB, ≤ 600 s, short side ≥ 360 px, valid H.264 video +
AAC audio (when the source has audio), full decode pass without errors.
Nothing that fails validation is ever uploaded.
