<#
.SYNOPSIS
  Deletes the job database (and optionally generated artifacts).
  Files in input/ and your TikTok tokens are never touched.
#>
[CmdletBinding()] param([switch]$All, [switch]$Force)
. "$PSScriptRoot\_common.ps1"
Set-Location (Get-RepoRoot)
Assert-Installed
$python = Get-VenvPython
if (-not $Force) {
    $answer = Read-Host "This deletes the job history$(if($All){' and processing/output/covers/failed'}). Continue? (y/N)"
    if ($answer -notmatch '^[yY]') { Write-Host "Aborted."; return }
}
if ($All) { & $python -m app.main reset-state --all } else { & $python -m app.main reset-state }
