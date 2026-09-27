# First-time setup: .env, runtime directories, diagnostics.
. "$PSScriptRoot\_common.ps1"
Assert-Venv

$envFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $RepoRoot ".env.example") $envFile
    Write-Host "[ok] Created .env from .env.example - edit it to add TikTok credentials." -ForegroundColor Yellow
} else {
    Write-Host "[ok] .env already exists (left untouched)"
}

foreach ($dir in @("input", "processing", "failed", "archive", "covers", "logs", "output", "data")) {
    $path = Join-Path $RepoRoot $dir
    if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
}
Write-Host "[ok] Runtime directories ready"

Write-Host ""
Write-Host "Running diagnostics ..." -ForegroundColor Cyan
& $VenvPy -m tta diagnose
Write-Host ""
Write-Host "Setup finished. Start the watcher with .\scripts\start.ps1" -ForegroundColor Green
