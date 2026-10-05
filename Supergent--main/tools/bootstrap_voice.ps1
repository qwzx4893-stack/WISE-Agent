[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$sttEnvironment = Join-Path $repoRoot ".voice-venv"
$sttPython = Join-Path $sttEnvironment "Scripts\python.exe"
$requirements = Join-Path $PSScriptRoot "voice_runtime_requirements.txt"
$indexTtsRoot = Join-Path $PSScriptRoot "index-tts"

if (-not (Test-Path $requirements)) {
    throw "Missing voice requirements: $requirements"
}
if (-not (Test-Path (Join-Path $indexTtsRoot "uv.lock"))) {
    throw "Missing locked IndexTTS runtime: $indexTtsRoot"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to reproduce the locked IndexTTS environment. Install uv, then run this script again."
}

& $Python -m venv $sttEnvironment
& $sttPython -m pip install --upgrade pip
& $sttPython -m pip install -r $requirements

Push-Location $indexTtsRoot
try {
    # The vendored lock file is authoritative; do not resolve a different set
    # of GPU/audio packages during installation.
    & uv sync --locked
}
finally {
    Pop-Location
}

Write-Host "WISE voice environments are ready."
