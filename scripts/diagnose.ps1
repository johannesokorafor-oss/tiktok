# Full system diagnostics (Python, FFmpeg, packages, dirs, DB, providers,
# TikTok config, internet, disk space).
. "$PSScriptRoot\_common.ps1"
Assert-Venv
& $VenvPy -m tta diagnose @args
exit $LASTEXITCODE
