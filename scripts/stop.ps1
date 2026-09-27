<#
.SYNOPSIS
  Stops a background instance started with start.ps1 -Background.
#>
[CmdletBinding()] param()
. "$PSScriptRoot\_common.ps1"
$pidFile = Get-PidFile
if (-not (Test-Path $pidFile)) { Write-Host "No PID file - nothing to stop." -ForegroundColor Yellow; return }
$procId = (Get-Content $pidFile | Select-Object -First 1).Trim()
try {
    $p = Get-Process -Id $procId -ErrorAction Stop
    Write-Host "Stopping PID $procId ..." -ForegroundColor Yellow
    $p.CloseMainWindow() | Out-Null
    Start-Sleep -Seconds 3
    if (-not $p.HasExited) { Stop-Process -Id $procId -Force }
    Write-Host "Stopped." -ForegroundColor Green
} catch {
    Write-Host "Process $procId is not running." -ForegroundColor Yellow
} finally {
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}
