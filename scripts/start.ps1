# Starts watcher + dashboard as a background process (PID recorded for stop.ps1).
# Use -Foreground to run attached to this terminal instead.
param([switch]$Foreground)
. "$PSScriptRoot\_common.ps1"
Assert-Venv

$existing = Get-RunningPidFromFile
if ($existing) {
    Write-Host "Already running (PID $existing). Use .\scripts\stop.ps1 first." -ForegroundColor Yellow
    exit 1
}
Remove-Item $PidFile -ErrorAction SilentlyContinue

if ($Foreground) {
    & $VenvPy -m tta start
    exit $LASTEXITCODE
}

$logDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$proc = Start-Process -FilePath $VenvPy -ArgumentList "-m", "tta", "start" `
    -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logDir "stdout.log") `
    -RedirectStandardError  (Join-Path $logDir "stderr.log")
New-Item -ItemType Directory -Path (Split-Path $PidFile) -Force | Out-Null
Set-Content -Path $PidFile -Value $proc.Id
Write-Host "Started (PID $($proc.Id))." -ForegroundColor Green
Write-Host "Watched folder: $(Join-Path $RepoRoot 'input')"
Write-Host "Dashboard:      http://127.0.0.1:8000"
Write-Host "Stop with:      .\scripts\stop.ps1"
