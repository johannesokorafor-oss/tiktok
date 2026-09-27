<#
.SYNOPSIS
  First-run configuration: creates .env and verifies the environment.
#>
[CmdletBinding()] param()
. "$PSScriptRoot\_common.ps1"
$root = Get-RepoRoot
Set-Location $root
Write-Head "Setup"

$env_file = Join-Path $root ".env"
if (-not (Test-Path $env_file)) {
    Copy-Item (Join-Path $root ".env.example") $env_file
    Write-Host "Created .env from .env.example" -ForegroundColor Green
} else {
    Write-Host ".env already exists - leaving it untouched." -ForegroundColor Yellow
}

Write-Host @"

Next steps:
  1. Open .env and fill in:
       TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET   (TikTok developer app)
       TIKTOK_REDIRECT_URI                        (must match the app config)
  2. Keep DRY_RUN=true for the first run.
  3. Modes: APP_MODE=DRAFT_UPLOAD (default, reviewable draft) | DIRECT_POST
     (needs CONFIRM_DIRECT_POST=true) - and DRY_RUN=true overrides both.
  4. Run .\scripts\diagnose.ps1
  5. Run .\scripts\start.ps1 and click "Connect" in the dashboard.
"@ -ForegroundColor Cyan

$python = Get-VenvPython
if ($python) { & $python -m app.main diagnose }
else { Write-Host "Run .\scripts\install.ps1 first." -ForegroundColor Red }
