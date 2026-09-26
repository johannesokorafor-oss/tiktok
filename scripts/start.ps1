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
$python = Get-VenvPython
if (-not $python) { throw "Run .\scripts\install.ps1 first." }
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "No .env found - running setup first." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "setup.ps1")
}
Write-Head "Starting watcher + dashboard"
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
