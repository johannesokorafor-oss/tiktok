# Stops the background watcher started by start.ps1.
. "$PSScriptRoot\_common.ps1"

if (-not (Test-Path $PidFile)) {
    Write-Host "No PID file found - nothing to stop (or it runs in another terminal)." -ForegroundColor Yellow
    exit 0
}
$procId = Get-RunningPidFromFile
if ($procId) {
    Stop-Process -Id $procId -Force
    Write-Host "Stopped process $procId." -ForegroundColor Green
} else {
    Write-Host "Recorded process is not running (stale PID file)." -ForegroundColor Yellow
}
Remove-Item $PidFile -ErrorAction SilentlyContinue
