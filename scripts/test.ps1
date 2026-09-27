<#
.SYNOPSIS
  Runs the offline test suite. Nothing is ever uploaded to TikTok.
#>
[CmdletBinding()] param([string]$Filter)
. "$PSScriptRoot\_common.ps1"
Set-Location (Get-RepoRoot)
Assert-Installed
$python = Get-VenvPython
# the suite is offline by construction; these also protect against a stray .env
$env:DRY_RUN = "true"; $env:TIKTOK_MOCK = "true"; $env:ALLOW_PAID_API = "false"
$pytestArgs = @("-m", "pytest", "-v", "tests")
if ($Filter) { $pytestArgs += @("-k", $Filter) }
& $python @pytestArgs
exit $LASTEXITCODE
