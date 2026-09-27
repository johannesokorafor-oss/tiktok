# Setup

## Requirements

| Component | Notes |
|---|---|
| Windows 10/11 (Linux/macOS also work) | Windows-native, no Docker required |
| Python 3.12+ recommended (3.11 minimum) | `py -3.12` is used by `install.ps1` if present |
| FFmpeg + ffprobe | optional system install; otherwise the `ffmpeg-binaries` wheel provides static binaries automatically |
| A TrueType font | Windows system fonts or DejaVu/Liberation/Noto on Linux. **No font files are shipped in this repository** |
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

### Fonts

The headline is rendered by the application, so the font must support German
umlauts. Discovery order: `FONT_BOLD` from `.env` → Segoe UI Bold / Arial Bold /
Bahnschrift (Windows) → DejaVu/Liberation/Noto Bold (Linux/macOS) → any bold
font found in the system font directories. Set explicitly if you prefer a
specific look:

```
FONT_BOLD=C:\Windows\Fonts\seguibl.ttf
FONT_REGULAR=C:\Windows\Fonts\segoeui.ttf
```

## Configuration

All configuration lives in `.env` (see `.env.example` for the annotated list).
The most important switches:

| Key | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | produce everything locally, upload nothing |
| `TIKTOK_MOCK` | `false` | exercise upload code without network (rehearsal/tests only) |
| `ALLOW_PAID_API` | `false` | hard block on paid image APIs |
| `QUALITY_MODE` | `BALANCED` | `FAST` / `BALANCED` / `HIGH_QUALITY` (3 scored candidates) |
| `STYLE_PRESET` | `AUTO` | or fix to `CINEMATIC_MYSTICAL` / `DARK_LUXURY` / `CLEAN_MODERN` |
| `DEFAULT_LANGUAGE` | `de` | fallback when language detection is inconclusive |
| `STABILITY_SECONDS` | `4` | how long a file must be unchanged before processing |
| `WATCH_RECURSIVE` | `true` | set `false` for flat watching |
| `COVER_FRAME_HOLD_MS` | `120` | how long the cover frame is visible at the start |
| `TIKTOK_POST_MODE` | `UPLOAD` | `UPLOAD` (inbox draft) or `DIRECT_POST` |
| `IMAGE_MAX_RETRIES` | `2` | retries per provider before the chain falls through |
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
3. Inspect `output/<name>/cover.png` and `final_tiktok.mp4`.
4. Connect TikTok, then set `DRY_RUN=false`.
