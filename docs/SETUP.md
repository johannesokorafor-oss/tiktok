# Setup

> **Thumbnail/cover creation is intentionally manual. The application uploads the video to TikTok and the user completes the cover selection and final editing inside TikTok.**

## Requirements

| Component | Notes |
|---|---|
| Windows 10/11 (Linux/macOS also work) | Windows-native, no Docker required |
| Python 3.12+ recommended (3.11 minimum) | `py -3.12` is used by `install.ps1` if present |
| FFmpeg + ffprobe | optional system install; otherwise the `ffmpeg-binaries` wheel provides static binaries automatically |
| Disk space | a few GB for processed copies |

## Install

```powershell
git clone <this repo>
cd tiktok
.\scripts\install.ps1
.\scripts\setup.ps1
```

`install.ps1` creates `.venv`, installs `requirements.txt` and creates the
working folders. `setup.ps1` copies `.env.example` to `.env` and runs the
diagnostics.

### FFmpeg

A full FFmpeg install is recommended:

```powershell
winget install Gyan.FFmpeg
```

If FFmpeg is not on `PATH`, the app falls back to the static binaries from the
`ffmpeg-binaries` wheel (ffmpeg **and** ffprobe). You can also pin paths:

```
FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
FFPROBE_PATH=C:\ffmpeg\bin\ffprobe.exe
```

## Configuration

All configuration lives in `.env` (see `.env.example` for the annotated list).
The most important switches:

| Key | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | produce everything locally, upload nothing |
| `TIKTOK_MOCK` | `false` | exercise upload code without network (rehearsal/tests only) |
| `QUALITY_MODE` | `BALANCED` | video encoding only: `FAST` / `BALANCED` / `HIGH_QUALITY` |
| `VIDEO_NORMALIZATION` | `auto` | `auto` = re-encode only when required, `always`, `never` |
| `ENFORCE_VERTICAL` | `true` | convert non-9:16 sources to 1080×1920 |
| `DEFAULT_LANGUAGE` | `de` | fallback when language detection is inconclusive |
| `STABILITY_SECONDS` | `4` | how long a file must be unchanged before processing |
| `WATCH_RECURSIVE` | `true` | set `false` for flat watching |
| `TIKTOK_POST_MODE` | `UPLOAD` | `UPLOAD` (inbox draft) or `DIRECT_POST` |
| `COPY_SOURCE_TO_OUTPUT` | `true` | also copy the source video to `output/<name>/source.mp4` |
| `CONTENT_IS_AIGC` | `false` | only for genuinely AI-generated videos |

Folders can be redirected (`INPUT_DIR`, `OUTPUT_DIR`, …) to any absolute path,
e.g. a NAS share.

## Running

```powershell
.\scripts\start.ps1                 # foreground: watcher + dashboard
.\scripts\start.ps1 -Background     # detached, stop with .\scripts\stop.ps1
```

Equivalent CLI commands:

```
python -m app.main run          # watcher + dashboard
python -m app.main watch        # watcher only
python -m app.main scan         # one pass, then exit
python -m app.main process video.mp4 [--metadata file.txt] [--force]
python -m app.main auth login|status|refresh|logout
python -m app.main diagnose [--json]
python -m app.main reset-state [--all]
```

## First run checklist

1. `.\scripts\diagnose.ps1` → no `FAIL`.
2. Copy `examples/beispiel_video.txt` next to a test video in `input/`
   (same base name) and watch the dashboard.
3. Inspect `output/<name>/final_tiktok.mp4` and `caption.txt`.
4. Connect TikTok, then set `DRY_RUN=false`.
