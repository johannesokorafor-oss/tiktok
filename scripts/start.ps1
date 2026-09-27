<#
.SYNOPSIS
  Starts the folder watcher and the local dashboard.
.PARAMETER Background
  Start detached and write state\app.pid so stop.ps1 can stop it.
#>
[CmdletBinding()] param([switch]$Background, [switch]$NoBrowser)
. "$PSScriptRoot\_common.ps1"
$root = Get-RepoRoot
Set-Location $root
Assert-Installed
$python = Get-VenvPython
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "No .env found - running setup first." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "setup.ps1")
}
Write-Head "Starting watcher + dashboard"
Write-Host "Drop <name>.mp4 + <name>.txt into the input folder - everything else is automatic." -ForegroundColor Gray
if (-not $Background) { Write-Host "Press Ctrl+C to stop (the watcher shuts down cleanly)." -ForegroundColor Gray }
$argsList = @("-m", "app.main", "run")
if (-not $NoBrowser) { $argsList += "--open-browser" }

if ($Background) {
    $proc = Start-Process -FilePath $python -ArgumentList $argsList -WorkingDirectory $root -PassThru
    New-Item -ItemType Directory -Force -Path (Join-Path $root "state") | Out-Null
    $proc.Id | Out-File -Encoding ascii (Get-PidFile)
    Write-Host "Started in background (PID $($proc.Id)). Stop with .\scripts\stop.ps1" -ForegroundColor Green
} else {
    & $python @argsList
    exit $LASTEXITCODE
}
