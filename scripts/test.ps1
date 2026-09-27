# Runs the full test suite.
. "$PSScriptRoot\_common.ps1"
Assert-Venv
& $VenvPy -m pytest (Join-Path $RepoRoot "tests") @args
exit $LASTEXITCODE
