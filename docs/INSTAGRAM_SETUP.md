# Instagram (optional — manual preparation by default)

Instagram is an **optional second platform**, **off by default**
(`INSTAGRAM_ENABLED=false`). While it is off the application makes no Instagram
API calls, needs no credentials and cannot produce Instagram errors. The TikTok
workflow is completely unaffected either way.

## The two Instagram modes

| | `PREPARE_ONLY` (**default**) | `API_STAGE_ONLY` (optional, advanced) |
|---|---|---|
| Meta API calls | **none at all** | official Content Publishing API |
| What you get | `instagram_ready.mp4`, `caption_instagram.txt`, `instagram_preparation.json` | a media **container** on Meta's servers + its id/status |
| Credentials needed | **none** | Meta app + Instagram Professional account + OAuth |
| Expires | never (local files) | **yes, after 24 h** |
| Appears in Instagram app drafts | no (nothing was sent) | **no** — a container is not an app draft |
| How you post | upload the file in the Instagram app yourself | finalize through the supported workflow |
| Runs automatically in a watched-folder job | no (manual button) | yes, after the TikTok upload |

`PREPARE_ONLY` is the default because an API container is *not* what most people
expect from a "draft": it is invisible in the app and disappears after 24 hours.

---

---

## PREPARE_ONLY — the default workflow

1. Let a job finish as usual (TikTok draft upload, or just `DRY_RUN`).
2. In the dashboard, select the job and press **Prepare for Instagram**.
3. The app validates the finished MP4 against the current Reels requirements:
   * if it already satisfies them, `instagram_ready.mp4` is a **copy of
     `final_tiktok.mp4`** — no second transcode;
   * only if it does not, an Instagram-specific variant is encoded
     (1080×1920, H.264 High + AAC 48 kHz stereo, ≤25 Mbps, faststart).
4. You get, in `output/<name>/`:

```
instagram_ready.mp4          the file to upload
caption_instagram.txt        caption built from the same TITLE/DESCRIPTION
instagram_preparation.json   status, paths, the requirements and your file's specs
```

```json
{
  "platform": "instagram",
  "mode": "PREPARE_ONLY",
  "status": "READY_FOR_MANUAL_UPLOAD",
  "video_path": "...\\output\\mein-video\\instagram_ready.mp4",
  "caption_path": "...\\output\\mein-video\\caption_instagram.txt",
  "cover_policy": "MANUAL",
  "auto_publish": false,
  "api_calls_made": 0
}
```

5. Open Instagram on your phone, upload `instagram_ready.mp4` as a Reel, paste
   the caption, **choose your own cover**, publish.

No Meta account, no app review, no token, nothing that can expire. The
`POST /{ig-user-id}/media` endpoint is **not** called in this mode.

---

## The one thing you must understand first (about the optional API mode)

> **An Instagram media container created through the API is NOT the same as a
> draft in the Instagram mobile app.**

TikTok has a real, visible draft/inbox workflow: the upload appears as a
notification in your TikTok inbox and you finish the post in the app.
**Instagram has no equivalent.** Meta's Content Publishing API works with
*media containers*:

```
1. POST /{ig-user-id}/media            -> container id        (this app does this)
2. upload the video bytes              -> container filled    (this app does this)
3. GET  /{container-id}?fields=status_code -> FINISHED         (this app polls this)
4. POST /{ig-user-id}/media_publish    -> the post goes live  (this app NEVER does this)
```

This application stops after step 3 and stores the container id and status.
The UI states it exactly like this:

> Instagram media uploaded and ready to publish. Instagram does not provide the
> same visible draft/inbox workflow as TikTok. The media remains unpublished and
> must be finalized through the supported workflow.

A staged container does **not** show up in the Instagram app's drafts. If you
want the Reel to go live you must finalize it through the supported workflow
(the publish step), which this application deliberately does not perform.
If you prefer to post manually in the app instead, use `final_tiktok.mp4` and
`caption_instagram.txt` from the output folder.

**Container expiration:** Meta expires unpublished media containers **24 hours**
after creation. The app stores the creation time, shows the remaining lifetime,
detects the `EXPIRED` status (and the 24 h age even if the API has not caught up
yet), and stops polling instead of retrying forever.

---

## API_STAGE_ONLY — everything below is only for this optional mode

Set `INSTAGRAM_MODE=API_STAGE_ONLY` explicitly if you want it. It is fully
implemented and tested, but it is **not** equivalent to TikTok's draft upload.

## 1. Account requirements

| Requirement | Detail |
|---|---|
| Account type | Instagram **Professional** account — **Business** or **Creator** |
| Personal accounts | **Not supported.** The Content Publishing API cannot access them and this app will not use unofficial methods |
| Facebook Page | **Not required** for the default *Instagram Login* configuration |
| Meta app | A Meta/Facebook developer app with the **Instagram** product added |

Switch your account to Professional in the Instagram app:
Settings → Account type and tools → Switch to professional account.

---

## 2. Two official login configurations (this matters for uploads)

Meta documents two ways to reach the same publishing API:

| | **Instagram API with Instagram Login** (default here) | **Instagram API with Facebook Login** |
|---|---|---|
| `INSTAGRAM_LOGIN_MODE` | `INSTAGRAM_LOGIN` | `FACEBOOK_LOGIN` |
| Host | `graph.instagram.com` | `graph.facebook.com` (+ `rupload.facebook.com`) |
| User logs in with | Instagram credentials | Facebook credentials |
| Facebook Page needed | no | yes (Page linked to the IG account) |
| Permissions | `instagram_business_basic`, `instagram_business_content_publish` | `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` |
| Token | Instagram User access token, 60 days, dedicated refresh endpoint | Page/User token mechanics |
| **Local file upload (`upload_type=resumable`)** | **not documented as available** | **available** |

**Consequence for this Windows-first app:** with *Instagram Login* Meta expects a
public HTTPS `video_url` for the container — the resumable upload session to
`rupload.facebook.com` is documented for apps that implemented *Facebook Login
for Business*. So you have exactly two supported options:

1. **`INSTAGRAM_LOGIN_MODE=FACEBOOK_LOGIN`** → the app uploads your local
   `final_tiktok.mp4` directly via the documented resumable upload. No public
   hosting, no web server on your PC. Requires the Facebook-Login app setup.
2. **`INSTAGRAM_LOGIN_MODE=INSTAGRAM_LOGIN`** (default) → you provide a public
   HTTPS URL for the finished file via `INSTAGRAM_PUBLIC_VIDEO_URL_TEMPLATE`
   (your own storage/CDN, e.g. an S3/Cloudflare R2 bucket you control).

The app never invents a workaround: it will not spin up a public web server and
will not use random file-sharing sites. If neither option is configured, the
Instagram status is `NOT_CONFIGURED` with that exact explanation, and your
TikTok upload continues normally.

---

## 3. Create the Meta app

1. Go to <https://developers.facebook.com/apps/> and create an app.
2. Add the **Instagram** product → **API setup with Instagram login**.
3. Note the **Instagram App ID** and **Instagram App Secret**
   (App Dashboard → Instagram → API setup with Instagram login → *Business login
   settings*).
4. Add the OAuth redirect URI **exactly**:
   `http://localhost:8765/instagram/callback`
5. Request the permissions your app will use:
   * `instagram_business_basic`
   * `instagram_business_content_publish`
6. For your own account, Standard Access is enough during development. Serving
   other people's accounts requires Meta App Review and business verification.

---

## 4. Configure `.env`

```ini
INSTAGRAM_ENABLED=true
# PREPARE_ONLY (default) needs nothing else at all.
# Only set API_STAGE_ONLY if you really want Meta media containers:
INSTAGRAM_MODE=API_STAGE_ONLY
INSTAGRAM_AUTO_PUBLISH=false
INSTAGRAM_COVER_MODE=MANUAL

INSTAGRAM_LOGIN_MODE=INSTAGRAM_LOGIN
INSTAGRAM_UPLOAD_METHOD=AUTO
# only needed for the INSTAGRAM_LOGIN path:
INSTAGRAM_PUBLIC_VIDEO_URL_TEMPLATE=https://your-bucket.example.com/{filename}

INSTAGRAM_CLIENT_ID=<Instagram App ID>
INSTAGRAM_CLIENT_SECRET=<Instagram App Secret>
INSTAGRAM_REDIRECT_URI=http://localhost:8765/instagram/callback
INSTAGRAM_SCOPES=instagram_business_basic,instagram_business_content_publish
```

If Meta changes the permission names, set `INSTAGRAM_SCOPES` accordingly — the
app does not hard-code them.

---

## 5. Connect the account (OAuth)

The flow follows Meta's *Business Login for Instagram*:

| Step | Endpoint |
|---|---|
| Authorize | `GET https://www.instagram.com/oauth/authorize?client_id=&redirect_uri=&response_type=code&scope=&state=` |
| Short-lived token (1 h) | `POST https://api.instagram.com/oauth/access_token` |
| Long-lived token (60 days) | `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token` |
| Refresh (another 60 days) | `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token` |

In the dashboard press **Connect Instagram**, approve the permissions, and you
are returned to a confirmation page. Notes enforced by the app:

* the `state` value is single-use and validated (CSRF protection);
* the short-lived token is immediately exchanged for a long-lived one;
* a long-lived token can only be refreshed when it is **at least 24 hours old**
  and not yet expired — there is **no grace period**, an expired token means
  re-authorising;
* Instagram tokens are stored in `state/instagram_tokens.json`, separate from
  the TikTok tokens, and are never written to logs or API responses.

Check it with **Test Instagram Connection** — it calls `GET /me` and reports the
account type, and reads `GET /{ig-user-id}/content_publishing_limit` so you see
your real remaining publishing quota rather than a guessed number.

---

## 6. What happens during a job

```
video.mp4 + video.txt
   → processed / validated MP4              (unchanged behaviour)
   → TikTok DRAFT_UPLOAD  → TIKTOK_DRAFT_READY
   → Instagram UPLOAD_ONLY → INSTAGRAM_UPLOAD_STARTED
                            INSTAGRAM_CONTAINER_CREATED
                            INSTAGRAM_MEDIA_UPLOADED       (resumable only)
                            INSTAGRAM_CONTAINER_STATUS …
                            INSTAGRAM_READY_TO_PUBLISH
   → COMPLETED
```

* In `PREPARE_ONLY` the watcher never contacts Meta: it only records
  `INSTAGRAM_PREPARE_AVAILABLE` so you know the manual button is ready.
* `DRY_RUN=true` → Instagram API staging is skipped entirely.
* An Instagram failure never damages the TikTok result: the platforms are stored
  in separate `job_platforms` rows and the job still completes.
* The final MP4 is validated against the current documented Reels requirements
  (MP4/MOV, H.264/HEVC, AAC, 3 s–15 min, ≥540×960, 23–60 fps, ≤1 GB). If the file
  already satisfies both platforms it is reused — **never transcoded twice**.
* No cover is sent: neither `cover_url` nor `thumb_offset`. You choose the cover
  in Instagram.

Extra artifacts in `output/<name>/` when Instagram is enabled:

```
caption_instagram.txt   caption built from the same TITLE/DESCRIPTION metadata
instagram.json          full result: container id, status, account, validation
instagram_status.json   short status record incl. expiry and the note above
```

---

## 7. Publishing is disabled by design

* `INSTAGRAM_AUTO_PUBLISH=false` is the hard default.
* Setting it to `true` changes nothing: the app logs
  `Instagram automatic publishing is disabled by design.` and still refuses.
* `InstagramClient.media_publish()` exists only to raise
  `PublishingDisabledError`; no code path calls it.
* A test (`test_source_tree_never_calls_media_publish`) fails the build if any
  code ever POSTs to `media_publish`, and the fake Meta server in the test suite
  asserts that the endpoint is never hit.
* There is no "Publish to Instagram" button in the dashboard.

The manual actions offered are **Prepare for Instagram** (local files, default)
and, only in `API_STAGE_ONLY`, **Stage via Instagram API** / **Refresh container
status** for re-staging an expired container. There is no publish button.

---

## 8. Troubleshooting

| Problem | Meaning | Fix |
|---|---|---|
| `Instagram integration disabled` | `INSTAGRAM_ENABLED=false` (default) | Set it to `true` and restart the app |
| Nothing happens automatically for Instagram | `PREPARE_ONLY` is the default and is manual on purpose | Select the job and press **Prepare for Instagram** |
| `INSTAGRAM_CLIENT_ID / INSTAGRAM_CLIENT_SECRET missing` | credentials not in `.env` | Section 4, then restart |
| `not connected - click Connect Instagram` | no token yet | Dashboard → Connect Instagram |
| `missing permissions: …` | the consent did not include a required scope | Re-connect and approve all permissions; check `INSTAGRAM_SCOPES` |
| `this is not an Instagram Professional account` | personal account | Switch to Business/Creator in the Instagram app |
| `Instagram Login cannot upload local files` | documented Meta limitation | Set `INSTAGRAM_PUBLIC_VIDEO_URL_TEMPLATE`, or use `INSTAGRAM_LOGIN_MODE=FACEBOOK_LOGIN` |
| status `EXPIRED` | container older than 24 h | Press **Prepare Instagram Upload** to stage it again |
| status `ERROR` | Meta rejected the media | Read `instagram.json`; usually a Reels spec violation |
| HTTP 429 / code 4 / 17 / 613 | Meta rate limits | The client retries with exponential backoff automatically |
| Token expired | long-lived token older than 60 days | Reconnect the account (expired tokens cannot be refreshed) |

---

## 9. What was and was not verified

The integration is implemented against the current official Meta documentation
(Instagram Platform → Content Publishing, Business Login for Instagram),
re-checked on 2026-09-27, and is covered by tests that run against a local fake
of those endpoints.

**No live Instagram API call has been made from the build environment** — it has
no outbound internet access and no Meta credentials. Your first real run with
`INSTAGRAM_ENABLED=true` and a connected account is the first live execution.
