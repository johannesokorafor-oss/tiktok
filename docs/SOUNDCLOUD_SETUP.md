# SoundCloud (optional) — audio, private upload only when verified

| | |
|---|---|
| **Mode** | `UPLOAD_PRIVATE` **only if you verified the private field**, otherwise automatically `PREPARE_ONLY` |
| **Result** | a private track, or local `soundcloud_ready.flac` + caption for a manual upload |
| **API** | official SoundCloud API, OAuth 2.1 + **PKCE** |
| **Account** | SoundCloud account with an **approved API application** (access is granted manually by SoundCloud) |
| **Default** | `SOUNDCLOUD_ENABLED=false`, `SOUNDCLOUD_PRIVATE_FIELD_VERIFIED=false` |

## SoundCloud is an audio platform

The MP4 is never uploaded as if it were a video. The app extracts the audio
track losslessly with FFmpeg:

```
soundcloud_ready.flac     FLAC, lossless (default; wav/mp3 configurable)
caption_soundcloud.txt    title + description + tags from your .txt
soundcloud.json           status, validation, paths
```

Validated against SoundCloud's documented limits: audio stream present, codec
accepted, duration ≤ 6 h, file ≤ 4 GB, sample rate/channels sane.
**No artwork is generated** — you set the track artwork in SoundCloud.

## Current API (verified 2026-09-27, developers.soundcloud.com/docs)

```
authorize : https://secure.soundcloud.com/authorize   (PKCE S256 required)
token     : POST https://secure.soundcloud.com/oauth/token
            grant_type=authorization_code | refresh_token
API       : https://api.soundcloud.com, header  Authorization: OAuth <token>
upload    : POST /tracks   multipart/form-data
            track[title], track[artist], track[asset_data]
```

Access tokens last ~1 hour and **refresh tokens are single use** — the app
always stores the rotated refresh token and never loops on a failed refresh.

## Why private upload is gated

The current official API Guide's upload example documents **only**
`track[title]`, `track[artist]` and `track[asset_data]`. It does not document a
private-sharing parameter for that call. This application therefore refuses to
guess a field name. Until you verify it yourself in the current OpenAPI spec
(<https://developers.soundcloud.com/docs/api/explorer/open-api>), SoundCloud is
automatically downgraded and reports:

> SoundCloud private upload is not currently verified against the current API
> contract.

If you have verified the field:

```ini
SOUNDCLOUD_PRIVATE_FIELD_VERIFIED=true
SOUNDCLOUD_PRIVATE_FIELD=track[sharing]
SOUNDCLOUD_PRIVATE_VALUE=private
```

The app then uploads with that field **and re-reads the response**: if
SoundCloud reports the track as public, the job fails with an explicit error so
you can delete it.

## Setup on Windows

1. Apply for API access at <https://developers.soundcloud.com/> (manual review).
2. Register the app, note client id/secret, add the redirect URI
   `http://localhost:8765/soundcloud/callback`.
3. `notepad .env`:

```ini
SOUNDCLOUD_ENABLED=true
SOUNDCLOUD_MODE=UPLOAD_PRIVATE
SOUNDCLOUD_CLIENT_ID=<client id>
SOUNDCLOUD_CLIENT_SECRET=<client secret>
SOUNDCLOUD_REDIRECT_URI=http://localhost:8765/soundcloud/callback
SOUNDCLOUD_AUDIO_FORMAT=flac
SOUNDCLOUD_PRIVATE_FIELD_VERIFIED=false
```

4. Connect the account (OAuth 2.1 + PKCE) and check
   `.\scripts\diagnose.ps1`.
5. Dashboard button: **Upload Private to SoundCloud** when verified, otherwise
   **Prepare for SoundCloud**.

## Limitations

* API access requires SoundCloud's manual approval; without it nothing works.
* Live upload has **not** been executed here — covered by tests against a local
  fake implementing the documented endpoints.
