# Setup

## Requirements

* Windows 10/11 (Linux/macOS work too — the code is pure Python + FFmpeg)
* Python 3.10+ (3.11 recommended)
* FFmpeg + ffprobe (installer offers winget install)
* ~2 GB free disk space

## Windows installation

```powershell
cd tiktok
.\scripts\install.ps1
```

The installer:

1. finds Python (`py -3` / `python`), refuses anything below 3.10
2. creates `.venv\`
3. installs `requirements.txt` and the `tta` package (editable)
4. checks for FFmpeg and offers `winget install Gyan.FFmpeg`

Then:

```powershell
.\scripts\setup.ps1
```

which creates `.env` from `.env.example`, creates the runtime folders
(`input/ processing/ failed/ archive/ covers/ logs/ output/ data/`) and runs
full diagnostics.

## Manual installation (any OS)

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
tta diagnose
```

FFmpeg: install from https://www.gyan.dev/ffmpeg/builds/ (Windows),
`apt install ffmpeg` (Debian/Ubuntu) or `brew install ffmpeg` (macOS).
If FFmpeg is not on PATH, set `FFMPEG_PATH` and `FFPROBE_PATH` in `.env`.

Fonts: DejaVu Sans (Bold) is bundled under `assets/fonts/`, so cover
typography (incl. German umlauts) works out of the box on every machine.
On Windows, Arial/Segoe UI are preferred automatically when present.

## Configuration

All settings live in `.env` (see `.env.example` for the full annotated
list). The important ones:

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | run everything except the TikTok upload |
| `IMAGE_PROVIDER` | `auto` | `auto` / `local` / `pollinations` / `comfyui` / `openai` |
| `ALLOW_PAID_API` | `false` | hard gate for paid providers |
| `STABILIZE_SECONDS` | `3` | quiet time before a file counts as fully copied |
| `PAIR_TIMEOUT` | `600` | seconds to wait for the second file of a pair |
| `UPLOAD_MODE` | `inbox` | `inbox` (draft/review) or `direct` |
| `COVER_DURATION_MS` | `500` | length of the embedded cover segment |
| `TTA_HOME` | repo dir | root for all runtime folders |

## Running

```powershell
.\scripts\start.ps1        # background; dashboard at http://127.0.0.1:8000
.\scripts\start.ps1 -Foreground
.\scripts\stop.ps1
.\scripts\diagnose.ps1
.\scripts\test.ps1
```

## First end-to-end test (no TikTok account needed)

```powershell
.venv\Scripts\python -m tta sample     # creates demo_reise.mp4/.txt in input\
.\scripts\start.ps1
```

Watch the dashboard: the job should reach `COMPLETED` within ~30 s, with a
cover preview and all artifacts in `output\demo_reise-<id>\`.
