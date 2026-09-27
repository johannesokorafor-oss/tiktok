<#
.SYNOPSIS
  Creates the virtual environment and installs all Python dependencies.
#>
[CmdletBinding()] param([switch]$Recreate)
. "$PSScriptRoot\_common.ps1"
$root = Get-RepoRoot
Set-Location $root
Write-Head "Installing TikTok Cover & Upload Automation"

$venv = Join-Path $root ".venv"
if ($Recreate -and (Test-Path $venv)) { Remove-Item -Recurse -Force $venv }

if (-not (Test-Path $venv)) {
    $py = Get-PythonLauncher
    Write-Host "Creating virtual environment with $($py.Exe) $($py.Prefix -join ' ')..." -ForegroundColor Yellow
    $venvArgs = @($py.Prefix + @("-m", "venv", $venv))
    & $py.Exe @venvArgs
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed with exit code $LASTEXITCODE" }
}
$python = Get-VenvPython
if (-not $python) { throw "Virtual environment creation failed." }

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $python -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }

# quick import check so problems surface here and not on first use
& $python -c "import fastapi, uvicorn, httpx, watchdog, PIL, pydantic_settings; print('dependencies OK')"
if ($LASTEXITCODE -ne 0) { throw "dependency verification failed" }

foreach ($d in @("input","processing","output","failed","archive","covers","logs","state")) {
    $p = Join-Path $root $d
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "`nFFmpeg was not found on PATH." -ForegroundColor Yellow
    Write-Host "  A bundled static FFmpeg (imageio-ffmpeg) will be used automatically," -ForegroundColor Yellow
    Write-Host "  but a full install is recommended (winget install Gyan.FFmpeg)." -ForegroundColor Yellow
}
Write-Host "`nInstall complete. Next: .\scripts\setup.ps1" -ForegroundColor Green
