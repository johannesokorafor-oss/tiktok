# Apple Music (optional) — PREPARE_ONLY (release delivery goes through a distributor)

| | |
|---|---|
| **Mode** | `PREPARE_ONLY` — the app never uploads to Apple Music |
| **Reason** | the Apple Music API is **catalog/library only**; there is no public artist-release ingestion API for this workflow |
| **Result** | `output/<job>/apple_music/` release package, status `READY_FOR_DISTRIBUTION` |
| **Default** | `APPLE_MUSIC_ENABLED=false` |
| **Artwork** | supplied by you / your distributor — never generated |

## What the current API does and does not do (checked 2026-09-27)

* The **Apple Music API** (and MusicKit) provide access to the Apple Music
  catalog and to a user's personal library, plus playback. They are not a
  delivery pipeline for new releases.
* **Apple Music for Artists** is primarily artist/profile management and
  analytics (claiming your profile, trends, promotional assets), not release
  ingestion.
* Independent artists and labels deliver releases through an approved **music
  distributor** (or, for larger catalogues, an Apple-approved encoding
  partner).

`AppleMusicProvider` therefore implements `prepare()` only and contains no HTTP
client at all (test-enforced).

## What "Prepare for Apple Music" produces

```
output/<job>/audio_master.flac                          shared lossless master
output/<job>/apple_music/apple_music_ready.flac         audio for your distributor
output/<job>/apple_music/caption_apple_music.txt        title, artist, notes, tags
output/<job>/apple_music/apple_music_metadata.json      machine-readable metadata
```

The JSON records `api_upload_supported: false`, `api_calls_made: 0`, the audio
master's technical details and a `missing_fields` list (for example the artist
name) so nothing is silently invented.

## Configuration

```ini
APPLE_MUSIC_ENABLED=true
APPLE_MUSIC_MODE=PREPARE_ONLY
APPLE_MUSIC_ARTIST=Your Artist Name
APPLE_MUSIC_ALBUM=Optional release title
APPLE_MUSIC_GENRE=Optional genre
APPLE_MUSIC_RELEASE_DATE=2026-12-01
# APPLE_MUSIC_EXPLICIT=false
```

## How to release

1. Choose a distributor (the project neither recommends nor requires one).
2. Create the release there, upload `apple_music_ready.flac`, paste the
   prepared metadata and add **your own artwork**.
3. The distributor delivers the release to Apple Music; you can then manage the
   artist profile in Apple Music for Artists.

Audio note: Apple's mastering guidance favours high-resolution lossless
sources. The shared `audio_master.flac` is a lossless FLAC extracted once from
your video and reused by every music platform — it is never re-transcoded per
platform.
