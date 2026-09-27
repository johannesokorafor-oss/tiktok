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
$script:PidFile = Join-Path (Join-Path $RepoRoot "data") "tta.pid"

function Find-SystemPython {
    # Returns a hashtable @{ Exe = ...; Args = @(...) } or $null.
    # Note: arrays passed to native executables expand element-by-element,
    # and an empty array contributes no arguments - no manual splitting.
    $candidates = @(
        @{ Exe = "py";      Args = @("-3") },
        @{ Exe = "python";  Args = @() },
        @{ Exe = "python3"; Args = @() }
    )
    foreach ($cand in $candidates) {
        if (-not (Get-Command $cand.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $v = & $cand.Exe $cand.Args --version 2>$null
            if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3\.(1[0-9])") {
                return $cand
            }
        } catch { }
    }
    return $null
}

function Get-RunningPidFromFile {
    # Returns the PID from $PidFile if that process is alive, else $null.
    if (-not (Test-Path $PidFile)) { return $null }
    $raw = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $procId = 0
    if (-not [int]::TryParse("$raw", [ref]$procId)) { return $null }
    if (Get-Process -Id $procId -ErrorAction SilentlyContinue) { return $procId }
    return $null
}

function Assert-Venv {
    if (-not (Test-Path $VenvPy)) {
        Write-Host "Virtual environment not found. Run .\scripts\install.ps1 first." -ForegroundColor Red
        exit 1
    }
}
