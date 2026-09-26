# Shared helpers for all scripts. Dot-source: . "$PSScriptRoot\_common.ps1"
$ErrorActionPreference = "Stop"

function Get-RepoRoot { Split-Path -Parent $PSScriptRoot }
function Get-VenvPython {
    $root = Get-RepoRoot
    $win  = Join-Path $root ".venv\Scripts\python.exe"
    $nix  = Join-Path $root ".venv/bin/python"
    if (Test-Path $win) { return $win }
    if (Test-Path $nix) { return $nix }
    return $null
}
function Get-PythonLauncher {
    # Returns @{ Exe = <executable>; Prefix = <string[]> } for the best interpreter found.
    $candidates = @(
        @{ Exe = "py";      Prefix = @("-3.13") },
        @{ Exe = "py";      Prefix = @("-3.12") },
        @{ Exe = "py";      Prefix = @("-3.11") },
        @{ Exe = "python";  Prefix = @() },
        @{ Exe = "python3"; Prefix = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $callArgs = @($c.Prefix + @("-c", "import sys; print(sys.version_info >= (3,11))"))
            $out = & $c.Exe @callArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -match "True") { return $c }
        } catch { }
    }
    throw "No Python 3.11+ interpreter found. Install Python 3.12 from https://python.org and re-run."
}
function Write-Head($text) {
    Write-Host ""
    Write-Host ("=" * 70) -ForegroundColor DarkCyan
    Write-Host " $text" -ForegroundColor Cyan
    Write-Host ("=" * 70) -ForegroundColor DarkCyan
}
function Get-PidFile { Join-Path (Get-RepoRoot) "state\app.pid" }
