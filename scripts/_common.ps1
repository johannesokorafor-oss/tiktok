# Shared helpers for all scripts. Dot-source: . "$PSScriptRoot\_common.ps1"
$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 defaults to the legacy code page; force UTF-8 so German
# umlauts in titles, captions and logs render correctly in the console.
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [System.Text.UTF8Encoding]::new()
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
} catch { }

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

function Test-VenvReady {
    $python = Get-VenvPython
    if (-not $python) { return $false }
    & $python -c "import fastapi, uvicorn, httpx, watchdog, PIL" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Assert-Installed {
    if (-not (Test-VenvReady)) {
        throw "The virtual environment is missing or incomplete. Run: .\scripts\install.ps1"
    }
}

function Get-FFmpegInfo {
    # Reports which FFmpeg/ffprobe the application will actually use.
    $python = Get-VenvPython
    if (-not $python) { return "unknown (no virtual environment yet)" }
    $code = "from app.video.ffmpeg import find_ffmpeg, find_ffprobe;" +
            "print(find_ffmpeg(None)); print(find_ffprobe(None))"
    try { return (& $python -c $code) -join " | " } catch { return "not found" }
}
