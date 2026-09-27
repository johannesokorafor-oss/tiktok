# Patreon (optional) — PREPARE_ONLY (no post-creation API exists)

| | |
|---|---|
| **Mode** | `PREPARE_ONLY` — the app never uploads to Patreon |
| **Reason** | Patreon **API v2 documents no endpoint to create a post or upload post media** |
| **API used** | v2 only, and only a read-only `GET /identity` check (optional) |
| **API v1** | **never used** — it retires on **7 October 2026** (extensions to 20 January 2027) |
| **Default** | `PATREON_ENABLED=false` |

## What the current API offers (verified 2026-09-27, docs.patreon.com)

Documented v2 endpoints:

```
GET  /api/oauth2/v2/identity
GET  /api/oauth2/v2/campaigns          GET /api/oauth2/v2/campaigns/{id}
GET  /api/oauth2/v2/campaigns/{id}/members     GET /api/oauth2/v2/members/{id}
GET  /api/oauth2/v2/campaigns/{id}/posts       GET /api/oauth2/v2/posts/{id}
POST /api/oauth2/v2/lives
```

Posts and media can be **read**, not created. There is no documented
post-creation or media-upload endpoint for third-party clients. The app
therefore prepares a complete package and you paste it into the Patreon editor.

Diagnostics say exactly this:

```
Patreon
Enabled: YES
Mode: PREPARE_ONLY
Post creation API: NOT DOCUMENTED IN CURRENT V2
Manual upload: REQUIRED
```

## What "Prepare for Patreon" produces

```
output/<job>/patreon_video.mp4       copy of the processed video
output/<job>/patreon_post.txt        title, body, tags, access recommendation,
                                     step-by-step manual posting instructions
output/<job>/patreon_metadata.json   machine-readable record (api_calls_made: 0)
```

Patreon's direct file upload accepts `.mov/.mp4/.mpeg/.ogg` up to **5 GB**;
the validator checks this before writing the package. The post **cover/thumbnail
is chosen by you in Patreon** — nothing is generated here.

## Setup on Windows

```ini
PATREON_ENABLED=true
PATREON_MODE=PREPARE_ONLY
PATREON_ACCESS_RECOMMENDATION=patrons-only
# optional, only for the read-only identity check in diagnostics:
PATREON_CLIENT_ID=
PATREON_CLIENT_SECRET=
PATREON_ACCESS_TOKEN=
```

Create a **v2** client at <https://www.patreon.com/portal/registration/register-clients>
if you want the identity check. New v1 clients can no longer be created and
this application refuses to call any v1 URL (a test enforces that).

## Future-proofing

`PatreonProvider.detect_post_creation_support()` is the single switch. If
Patreon documents post creation, implement `upload()` and flip that flag — the
rest of the application (registry, dashboard button, job records) already
supports it.
