<#
.SYNOPSIS
  Environment diagnostics: Python, FFmpeg, permissions, database, fonts,
  image providers, TikTok configuration/OAuth, connectivity, disk space.
#>
[CmdletBinding()] param([switch]$Json)
. "$PSScriptRoot\_common.ps1"
Set-Location (Get-RepoRoot)
$python = Get-VenvPython
if (-not $python) { throw "Run .\scripts\install.ps1 first." }
if ($Json) { & $python -m app.main diagnose --json } else { & $python -m app.main diagnose }
exit $LASTEXITCODE
