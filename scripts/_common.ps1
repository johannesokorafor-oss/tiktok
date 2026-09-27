# Shared helpers for all scripts (dot-source this file).
$ErrorActionPreference = "Stop"
$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:VenvDir  = Join-Path $RepoRoot ".venv"
$script:VenvPy   = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    # Linux/macOS layout (e.g. PowerShell Core)
    $alt = Join-Path $VenvDir "bin/python"
    if (Test-Path $alt) { $script:VenvPy = $alt }
}
$script:PidFile = Join-Path $RepoRoot "data\tta.pid"

function Find-SystemPython {
    foreach ($cand in @("py -3", "python", "python3")) {
        try {
            $parts = $cand.Split(" ")
            $v = & $parts[0] $parts[1..($parts.Length-1)] --version 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -match "Python 3\.(1[0-9])") {
                return $cand
            }
        } catch { }
    }
    return $null
}

function Assert-Venv {
    if (-not (Test-Path $VenvPy)) {
        Write-Host "Virtual environment not found. Run .\scripts\install.ps1 first." -ForegroundColor Red
        exit 1
    }
}
