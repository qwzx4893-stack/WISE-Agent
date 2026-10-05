[CmdletBinding()]
param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$appRoot = Join-Path $workspace 'Supergent--main'
$environment = Join-Path $appRoot '.venv'
$appPython = Join-Path $environment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'requirements.txt'))) {
    throw 'Run the installer from a complete WISE workspace checkout.'
}
& $Python -c 'import sys; assert sys.version_info[:2] == (3, 11), "WISE setup requires Python 3.11"'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 prerequisite check failed.' }
if (-not (Test-Path -LiteralPath $appPython)) {
    & $Python -m venv $environment
    if ($LASTEXITCODE -ne 0) { throw 'Creating the WISE environment failed.' }
}
& $appPython -m pip install -r (Join-Path $appRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing WISE runtime requirements failed.' }
Write-Host 'Text runtime installed. WebView2, credentials and optional runtimes require separate setup.'
Write-Host "Start with: & '$appPython' '$(Join-Path $appRoot 'wise_desktop.py')' --native-only"
