# Installs the TikTok Auto-Poster: venv + Python packages + FFmpeg check.
. "$PSScriptRoot\_common.ps1"

Write-Host "== TikTok Auto-Poster installer ==" -ForegroundColor Cyan

# 1. Python
$python = Find-SystemPython
if (-not $python) {
    Write-Host "Python 3.10+ was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ (check 'Add to PATH') and re-run."
    exit 1
}
Write-Host "[ok] Python found: $python"

# 2. Virtual environment
if (-not (Test-Path $VenvPy)) {
    Write-Host "Creating virtual environment at $VenvDir ..."
    $parts = $python.Split(" ")
    & $parts[0] $parts[1..($parts.Length-1)] -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Host "venv creation failed" -ForegroundColor Red; exit 1 }
}
Write-Host "[ok] Virtual environment ready"

# 3. Packages
Write-Host "Installing Python packages ..."
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $RepoRoot "requirements.txt") --quiet
& $VenvPy -m pip install -e $RepoRoot --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "package installation failed" -ForegroundColor Red; exit 1 }
Write-Host "[ok] Packages installed"

# 4. FFmpeg
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpeg) {
    Write-Host "[ok] FFmpeg found: $($ffmpeg.Source)"
} else {
    Write-Host "[!!] FFmpeg not found on PATH." -ForegroundColor Yellow
    $answer = Read-Host "Install FFmpeg now via winget? (y/n)"
    if ($answer -eq "y") {
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        Write-Host "Re-open the terminal afterwards so PATH is refreshed." -ForegroundColor Yellow
    } else {
        Write-Host "Install FFmpeg manually (https://www.gyan.dev/ffmpeg/builds/) or set FFMPEG_PATH/FFPROBE_PATH in .env."
    }
}

Write-Host ""
Write-Host "Done. Next: .\scripts\setup.ps1" -ForegroundColor Green
