# Real test on your Windows 11 PC — step by step

This guide takes you from "nothing installed" to "my video is waiting in my
TikTok inbox as a draft". No programming knowledge is required: every command
is a line you can copy and paste into PowerShell.

Total time: about 20 minutes (plus the TikTok developer app approval, which is
usually instant for the `video.upload` scope).

> **What the app will and will not do.** It watches a folder, builds the cover,
> builds the final video and uploads it to TikTok **as a draft**. It never
> publishes anything. The last step is always you, inside the TikTok app.

---

## Step 0 — What you need

| Thing | Notes |
|---|---|
| Windows 10/11 | Windows 11 is what this guide was written for |
| Python 3.12 | <https://www.python.org/downloads/> — during install tick **"Add python.exe to PATH"** |
| Git (optional) | only needed for `git clone`; you can download a ZIP instead |
| A TikTok account | the one you want to post from |
| A short test video | any MP4, 5–30 seconds is ideal |
| FFmpeg | **optional** — the app ships a working fallback, see step 5 |

---

## Step 1 — Get the project onto your PC

**Option A — with Git (recommended):**

```powershell
cd $HOME\Documents
git clone https://github.com/johannesokorafor-oss/tiktok.git
cd tiktok
git checkout arena/01a0dc6a-tiktok
```

**Option B — without Git:** open the repository on GitHub, switch the branch
selector to `arena/01a0dc6a-tiktok`, choose **Code → Download ZIP**, extract it
to `C:\Users\<you>\Documents\tiktok`, then:

```powershell
cd $HOME\Documents\tiktok
```

---

## Step 2 — Open PowerShell in the project folder

Press **Windows key**, type `PowerShell`, open **Windows PowerShell**, then:

```powershell
cd $HOME\Documents\tiktok
```

If your path contains spaces, quote it: `cd "$HOME\Documents\my folder\tiktok"`.

Windows blocks local scripts by default. Allow them **for this user only**
(safe, standard setting):

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Answer `Y` if asked. If you prefer not to change the policy at all, prefix each
script call with a bypass, e.g.
`powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1`.

---

## Step 3 — Install

```powershell
.\scripts\install.ps1
```

This creates a private Python environment in `.venv\` and installs everything.
It ends with `dependencies OK` and `Install complete`.

If it says no Python was found, install Python 3.12, **close and reopen
PowerShell**, and run it again.

---

## Step 4 — Create and fill in your `.env`

```powershell
.\scripts\setup.ps1
notepad .env
```

`setup.ps1` copies `.env.example` to `.env` and immediately runs the
diagnostics. For the **first run you do not have to change anything** — the
defaults are safe (`DRY_RUN=true`, nothing is uploaded).

The fields you will need later, for the real upload:

```ini
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_REDIRECT_URI=http://localhost:8765/tiktok/callback
TIKTOK_SCOPES=user.info.basic,video.upload
APP_MODE=DRAFT_UPLOAD
DRY_RUN=true
```

Save the file (`Ctrl+S`) and close Notepad. Never share this file — it contains
your secret.

---

## Step 5 — Check FFmpeg

```powershell
.\scripts\diagnose.ps1
```

Look at the first lines of the report:

```
[PASS] FFmpeg     ffmpeg version ...
[PASS] ffprobe    ...
```

Both lines must say **PASS**. The project installs a static FFmpeg through pip
automatically, so this normally just works. If you prefer a system install:

```powershell
winget install --id Gyan.FFmpeg -e
```

then close and reopen PowerShell and run `.\scripts\diagnose.ps1` again.

---

## Step 6 — Check the image provider (important for thumbnail quality)

In the same report, find:

```
[..] Image provider: pollinations   Tier: ... | Model: flux | High Quality: YES | ...
[..] Image quality capability       ...
```

There are two possible outcomes:

* **`AI image model available (...)`** → real AI thumbnails. Continue.
* **`HIGH QUALITY AI IMAGE PROVIDER NOT CONFIGURED`** → the app can still run,
  but the cover would be drawn by the built-in *offline fallback renderer*
  (Pillow). That is a technical fallback, **not** an AI image model, and the
  app labels every such cover `OFFLINE_FALLBACK_GENERATED`.

### Which provider should you use for the real test?

| Provider | Local or cloud | API key | Free? | Recommended |
|---|---|---|---|---|
| `pollinations` | cloud | none | **tier is checked at runtime** — see below | ✅ easiest start |
| `comfyui` | local (your GPU) | none | no API fees at all (LOCAL) | ✅ best quality if you have a GPU |
| `automatic1111` | local (your GPU) | none | no API fees at all (LOCAL) | ✅ alternative local option |
| `huggingface` | cloud | **yes** (`HUGGINGFACE_API_TOKEN`) | FREE **WITH QUOTA** — a limited monthly credit allowance, then it stops (or bills you if you enabled billing yourself) | optional |
| `offline` | local | none | LOCAL, free, **not an AI model** | fallback only |

**How the tier is decided:** the app asks the provider itself. For Pollinations
it reads the live model catalogue (`GET /models`) and only reports `FREE` if
that catalogue marks your configured model (`POLLINATIONS_MODEL`, default
`flux`) as a free/anonymous tier. If the catalogue cannot be read, the tier is
reported as **`UNKNOWN`** — the app never calls something "free" just because
it answered without an API key. Paid providers are blocked entirely while
`ALLOW_PAID_API=false` (the default).

**Recommended for your first real test — Pollinations (nothing to install):**

```ini
IMAGE_PROVIDER=auto
IMAGE_PROVIDER_PRIMARY=pollinations
POLLINATIONS_MODEL=flux
```

Then re-run `.\scripts\diagnose.ps1`. If your PC can reach the service you will
see `Image quality capability ... AI image model available (pollinations)`.
After you have checked its current pricing page yourself, you may pin the tier
with `POLLINATIONS_TIER=FREE` so the dashboard stops showing `UNKNOWN`.

**If you have an NVIDIA GPU and want maximum quality — ComfyUI:**

1. Install ComfyUI and start it with its normal launcher (default
   `http://127.0.0.1:8188`).
2. Put an SDXL checkpoint in `ComfyUI\models\checkpoints\`.
3. In `.env`:

```ini
IMAGE_PROVIDER_PRIMARY=comfyui
COMFYUI_BASE_URL=http://127.0.0.1:8188
COMFYUI_CHECKPOINT=sd_xl_base_1.0.safetensors
```

4. `.\scripts\diagnose.ps1` must show `Image provider: comfyui ... Status: HTTP 200`.

**If you want the job to fail rather than ever produce a fallback cover:**

```ini
QUALITY_MODE=HIGH_QUALITY
ALLOW_OFFLINE_IMAGE_FALLBACK=false
DRY_RUN_ALLOW_OFFLINE=false
```

---

## Step 7 — Start the watcher and the dashboard

```powershell
.\scripts\start.ps1
```

The window prints a summary and then keeps running. Open
<http://127.0.0.1:8765> in your browser (the script can open it for you).

The header shows three things you should check:

* `watcher running`
* `MODE: DRY_RUN` (for now — nothing will be uploaded)
* either `AI model: pollinations` or `HIGH QUALITY AI IMAGE PROVIDER NOT CONFIGURED`

Leave this PowerShell window open. **Ctrl+C** stops the application cleanly.

---

## Step 8 — Prepare a real test video

Copy any short MP4 into the `input` folder and name it `test-video.mp4`:

```powershell
Copy-Item "$HOME\Videos\my-clip.mp4" ".\input\test-video.mp4"
```

Vertical 1080×1920 is ideal; landscape also works (the app converts it to 9:16
with a blurred background). Do not put the `.txt` file in first — actually, the
order does not matter: the app waits for the pair.

---

## Step 9 — Create the matching text file

**The two files must share the same base name**: `test-video.mp4` +
`test-video.txt`.

A ready-made template ships with the project — copy it and edit it:

```powershell
Copy-Item .\examples\test-video.txt .\input\test-video.txt
notepad .\input\test-video.txt
```

Or create it from scratch (`notepad .\input\test-video.txt`) and paste this (German or English, your choice — the app detects the language):

```
TITLE:
Die Zeichen, die deine Seele dir zeigen will

DESCRIPTION:
Eine ruhige, mystische Betrachtung über Intuition, wiederkehrende Muster und
Momente, die sich spirituell bedeutsam anfühlen. Warum tauchen dieselben Zahlen
und Träume immer wieder auf? Nichts davon ist Zufall, wenn du genauer hinsiehst.

IMAGE_PROMPT:
A cinematic mystical night scene with a solitary human silhouette standing
beneath a vast celestial sky, subtle glowing symbols in the atmosphere, deep
blue and violet tones, dramatic volumetric light, realistic cinematic
photography, strong depth, highly detailed, premium visual style.
```

Save with `Ctrl+S`. Notepad saves UTF-8 by default on Windows 11, so umlauts
are fine.

Optional extra lines you can add:

```
COVER_TEXT: NICHTS IST ZUFALL
STYLE: CINEMATIC_MYSTICAL
```

`COVER_TEXT` forces the 2–4 word hook, `STYLE` forces one of
`CINEMATIC_MYSTICAL`, `DARK_LUXURY`, `CLEAN_MODERN`.

---

## Step 10 — Watch the automatic workflow

You do not run any command. Within a few seconds the dashboard shows a new job
and the PowerShell window logs the progress. Expected sequence:

```
DISCOVERED
VALIDATING
GENERATING_METADATA
GENERATING_IMAGE
IMAGE_PREFLIGHT
BUILDING_VIDEO
VALIDATING_VIDEO
UPLOAD_SKIPPED        (only in DRY_RUN)
COMPLETED
```

With `DRY_RUN=false` the last steps are `UPLOADING → UPLOADED → COMPLETED`
instead of `UPLOAD_SKIPPED`.

Typical duration: 30–90 seconds for a short clip.

---

## Step 11 — Check the generated cover

In the dashboard click the job. You see the cover preview and, directly under
it, a badge:

| Badge | Meaning |
|---|---|
| **AI MODEL GENERATED** | a real image model produced the background |
| **OFFLINE FALLBACK GENERATED** | the built-in Pillow renderer produced it — not AI quality |

Open the file itself:

```powershell
Start-Process ".\output\test-video\cover.png"
```

Check: 1080×1920, headline is 2–4 words, large, inside the frame, umlauts
correct.

If you want a different hook or style, use the dashboard fields
**hook / style / image prompt / caption** and press **Save & regenerate cover**.

---

## Step 12 — Check the final MP4

```powershell
Start-Process ".\output\test-video\final_tiktok.mp4"
```

Check: it opens and plays, audio is present and in sync, the picture is
vertical, and the very first moment shows your cover (about 0.12 s) before the
video continues normally. Your original file in `input\` is untouched.

The folder contains:

```
output\test-video\
    source.mp4            copy of your input
    metadata.txt          copy of your text file
    cover.png             the cover
    final_tiktok.mp4      the file that gets uploaded
    caption.txt           caption + hashtags to paste in TikTok
    job.json              everything the app decided and measured
    upload_result.json    upload status / publish id
```

**Do not continue to TikTok until steps 11 and 12 look good.**

---

## Step 13 — Create your TikTok developer app

Full details: [TIKTOK_SETUP.md](TIKTOK_SETUP.md). Short version:

1. Go to <https://developers.tiktok.com/>, log in with your TikTok account.
2. **Manage apps → Connect an app**, give it a name.
3. Add the product **Content Posting API**.
4. Add scopes: `user.info.basic` and `video.upload`
   (**not** `video.publish` — you do not want direct posting).
5. Add the redirect URI **exactly**:
   `http://localhost:8765/tiktok/callback`
6. Copy **Client key** and **Client secret**.

Put them into `.env`:

```powershell
notepad .env
```

```ini
TIKTOK_CLIENT_KEY=<your client key>
TIKTOK_CLIENT_SECRET=<your client secret>
TIKTOK_REDIRECT_URI=http://localhost:8765/tiktok/callback
TIKTOK_SCOPES=user.info.basic,video.upload
APP_MODE=DRAFT_UPLOAD
```

Leave `DRY_RUN=true` for the moment. Restart the app so it reads the new file:
press **Ctrl+C** in the PowerShell window, then `.\scripts\start.ps1` again.

---

## Step 14 — Authorise (OAuth)

1. Open <http://127.0.0.1:8765> and click **Connect**.
2. TikTok opens; log in and approve the requested permissions.
3. You are sent back to a page saying **"TikTok connected"**.
4. The dashboard header now shows `tiktok connected`.

Verify from PowerShell (open a *second* window, keep the app running):

```powershell
cd $HOME\Documents\tiktok
.\.venv\Scripts\python.exe -m app.main auth status
```

You should see `"authenticated": true` and your scopes. Tokens are stored in
`state\tiktok_tokens.json` (access token ~24 h, refreshed automatically).

---

## Step 15 — Your first real DRAFT_UPLOAD

1. Stop the app with **Ctrl+C**.
2. `notepad .env` and change exactly one line:

```ini
DRY_RUN=false
```

Keep `APP_MODE=DRAFT_UPLOAD` and `TIKTOK_POST_MODE=UPLOAD`. Never set
`CONFIRM_DIRECT_POST=true` for this test — the app refuses to direct-post
without it, which is what you want.

3. Start again and confirm the header/banner says **`MODE: DRAFT_UPLOAD`**:

```powershell
.\scripts\start.ps1
```

4. Drop a **new** pair into `input\` (the app skips duplicates on purpose):

```powershell
Copy-Item "$HOME\Videos\my-clip.mp4" ".\input\first-upload.mp4"
Copy-Item ".\input\test-video.txt"   ".\input\first-upload.txt"
```

5. Watch the states: `… BUILDING_VIDEO → VALIDATING_VIDEO → UPLOADING →
   UPLOADED → COMPLETED`, and in the log
   `TIKTOK_DRAFT_READY publish_id=… status=SEND_TO_USER_INBOX`.

---

## Step 16 — Check your TikTok inbox

On your phone:

1. Open the **TikTok app** with the same account.
2. Go to **Inbox** (the notifications tab). You get a notification that a video
   was uploaded from a third-party app and is ready to be edited/posted.
   It usually appears within a minute.
3. Tap it — the video opens in TikTok's normal posting editor.

If nothing arrives, check `output\first-upload\upload_result.json`: `status`
should be `SEND_TO_USER_INBOX` or `PUBLISH_COMPLETE`, and any API error is
recorded there and in the dashboard.

---

## Step 17 — Finish the post yourself in TikTok

1. In the editor, set the **cover**: the first frame is the cover this app
   generated, so leaving the cover slider at the very beginning selects it.
2. Paste the caption: open `output\first-upload\caption.txt`, or click
   **Copy caption** in the dashboard job view.
3. Set your privacy, comments, duet/stitch settings as usual.
4. Press **Post**.

That's the whole workflow. From now on: drop `video.mp4` + `video.txt` into
`input\`, and everything up to the TikTok draft happens automatically.

---

## Daily use

```powershell
cd $HOME\Documents\tiktok
.\scripts\start.ps1              # leave running; Ctrl+C to stop
# or run detached:
.\scripts\start.ps1 -Background
.\scripts\stop.ps1
```

Useful:

```powershell
.\scripts\diagnose.ps1           # health report
.\scripts\test.ps1               # 137 offline tests, never touches TikTok
.\scripts\reset_state.ps1        # forget job history (input files are kept)
```

---

## Troubleshooting

| Problem | What it means | Fix |
|---|---|---|
| Cover badge says **OFFLINE_FALLBACK_GENERATED** | No AI image provider was reachable, so the built-in Pillow renderer drew the background. It is honest, but it is not AI quality. | Configure a provider (step 6): reachable Pollinations, local ComfyUI/A1111, or a Hugging Face token. Re-run `.\scripts\diagnose.ps1` until `Image quality capability` says *AI image model available*, then **regenerate cover** on the job. |
| `HIGH QUALITY AI IMAGE PROVIDER NOT CONFIGURED` | Same situation, reported before/while running. | See above. With `QUALITY_MODE=HIGH_QUALITY` the job fails instead of producing a fallback cover — that is intentional. |
| `TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET missing in .env` | The developer app credentials are not configured. | Step 13; then restart the app so `.env` is re-read. |
| `scope_not_authorized` / `TikTok scopes … required for UPLOAD: video.upload` | Your app or your consent does not include `video.upload`. | Add the scope in the TikTok developer portal, set `TIKTOK_SCOPES=user.info.basic,video.upload`, then **Disconnect** and **Connect** again. |
| OAuth callback fails ("state validation failed", browser error, page not found) | The link expired (10 min), was reused, or the redirect URI does not match. | The redirect URI in the portal and in `.env` must be byte-identical (`http://localhost:8765/tiktok/callback`). The app must be running while you click Connect. Then click **Connect** again. |
| `FFmpeg not found` / `ffprobe not found` | Neither a system FFmpeg nor the bundled static build was usable. | `winget install --id Gyan.FFmpeg -e`, reopen PowerShell; or set `FFMPEG_PATH` / `FFPROBE_PATH` in `.env`. Re-run `.\scripts\diagnose.ps1`. |
| Image provider shows `not running` / `unreachable` | ComfyUI/A1111 is not started, or there is no internet/proxy access to the cloud provider. | Start the local UI (ComfyUI with its launcher, A1111 with `--api`), or check the network. The chain automatically tries the next provider. |
| Provider tier shows **UNKNOWN** | The app could not verify the provider's current pricing, so it refuses to call it "free". | Check the provider's pricing page yourself, then set e.g. `POLLINATIONS_TIER=FREE`. Or set `ALLOW_UNKNOWN_TIER_PROVIDERS=false` to refuse unverified providers. |
| Job state **DUPLICATE** | The same video content (SHA-256) was already processed — protection against double uploads. | Use a different video, or press **Force reprocess** in the dashboard. |
| HTTP 429 / `rate_limit_exceeded` / `spam_risk_too_many_posts` | TikTok's per-token limits (6 upload inits/min, 30 status calls/min) or the daily/pending-drafts cap. | Nothing to fix — the app backs off and retries automatically. For the daily cap, wait and retry later. |
| `TikTok upload failed` / job `FAILED` after `UPLOADING` | The API rejected the request or the transfer broke. | Read the error on the job and in `output\<name>\upload_result.json`, then press **Retry**. Upload URLs expire after 1 hour, so a retry starts a fresh upload. |
| Path problems ("cannot find path", nothing is detected) | Spaces or OneDrive-redirected folders in the path, or the files are not in `input\`. | Quote paths: `cd "C:\path with spaces\tiktok"`. Check `Get-Location` and `Get-ChildItem .\input`. You can also point `INPUT_DIR=` / `OUTPUT_DIR=` in `.env` to simple paths like `C:\TikTok\input`. |
| `running scripts is disabled on this system` | PowerShell execution policy. | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`, or run `powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1`. |

---

## What was verified where

* The complete local pipeline (watcher → parsing → cover → FFmpeg →
  validation → artifacts, `DRY_RUN`) and the 137-test suite were executed in
  the build environment and pass.
* **Not executed there:** live cloud image generation and live TikTok API
  calls — that environment has no outbound internet and no TikTok credentials.
  Those paths are implemented against the official documentation and covered by
  tests with mocked HTTP transports. **Your Windows run in steps 13–17 is the
  first real execution of them.** If anything differs from this guide, the
  exact error is shown on the job and stored in `upload_result.json`.
