# Spotify (optional) — PREPARE_ONLY (release delivery goes through a distributor)

| | |
|---|---|
| **Mode** | `PREPARE_ONLY` — the app never uploads to Spotify |
| **Reason** | the official **Spotify Web API has no artist-release ingestion endpoint** |
| **Result** | `output/<job>/spotify/` release package, status `READY_FOR_DISTRIBUTION` |
| **Default** | `SPOTIFY_ENABLED=false` |
| **Artwork** | supplied by you / your distributor — never generated |

## What the current API does and does not do (checked 2026-09-27)

The Spotify Web API covers catalog metadata, search, playlists, playback and
user-library operations. It contains **no endpoint that ingests a new
release**. Spotify for Artists is for claiming and managing your artist
profile, pitching to editorial playlists and viewing analytics — not for
uploading audio. Independent artists deliver releases through a **music
distributor**, which sends the release to Spotify and other services.

This application therefore refuses to pretend: `SpotifyProvider` implements
`prepare()` only, has no HTTP client at all (test-enforced), and never contacts
Spotify.

## What "Prepare for Spotify" produces

```
output/<job>/audio_master.flac          shared lossless master (extracted once)
output/<job>/spotify/spotify_ready.flac lossless audio for your distributor
output/<job>/spotify/caption_spotify.txt title, artist, release, notes, tags
output/<job>/spotify/spotify_metadata.json machine-readable metadata
```

`spotify_metadata.json` contains `api_upload_supported: false`,
`api_calls_made: 0`, the audio master's technical details (codec, sample rate,
channels, bit depth, duration, size) and a `missing_fields` list telling you
exactly what you still have to supply (for example the artist name).

## Metadata you should configure

```ini
SPOTIFY_ENABLED=true
SPOTIFY_MODE=PREPARE_ONLY
SPOTIFY_ARTIST=Your Artist Name
SPOTIFY_ALBUM=Optional release title
SPOTIFY_GENRE=Optional genre
SPOTIFY_RELEASE_DATE=2026-12-01
# SPOTIFY_EXPLICIT=false        # only set it if you actually know
```

Nothing is invented: the track title, description, tags and language come from
your `.txt` file, and anything missing is reported rather than guessed.

## How to release

1. Choose a music distributor yourself. Several exist with different pricing
   and royalty models — this project deliberately **does not recommend or
   require a specific one**, and no distributor API is a dependency.
2. Create the release in your distributor's interface.
3. Upload `spotify_ready.flac`, paste the metadata from
   `caption_spotify.txt` / `spotify_metadata.json`, and add **your own
   artwork** (typically square, 3000×3000 px).
4. Submit; the distributor delivers to Spotify. Afterwards you can claim the
   release in Spotify for Artists.

If you later use a distributor that offers an official API, that belongs in a
separate distributor adapter — it is intentionally not implemented here.
