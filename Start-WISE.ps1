[CmdletBinding()]
param(
    [int]$Port = 8765
)

# Starts WISE Core inside WISE's own native Windows window.
# Browser app-mode is intentionally disabled for the product launcher.
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSCommandPath
$hostName = '127.0.0.1'
$desktopLauncher = Join-Path $workspace 'wise_desktop.py'
$pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
$pythonShim = if ($pythonCommand) { $pythonCommand.Source } else { Join-Path $env:USERPROFILE '.aurator\venv\Scripts\python.exe' }
$python = $pythonShim

if (Test-Path -LiteralPath $pythonShim) {
    try {
        $resolvedPython = (& $pythonShim -c 'import sys; print(sys.executable)' | Select-Object -Last 1).Trim()
        if ($resolvedPython -and (Test-Path -LiteralPath $resolvedPython)) {
            $python = $resolvedPython
        }
    } catch {
        $python = $pythonShim
    }
}

$pythonWindowed = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (Test-Path -LiteralPath $pythonWindowed) {
    $python = $pythonWindowed
}

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python was not found. WISE Core cannot be started.'
}
if (-not (Test-Path -LiteralPath $desktopLauncher)) {
    throw "WISE desktop supervisor was not found at $desktopLauncher."
}

Start-Process -FilePath $python -ArgumentList @(
    $desktopLauncher,
    '--native-only',
    '--host', $hostName,
    '--port', $Port
) -WorkingDirectory $workspace -WindowStyle Normal

Write-Host 'WISE is opening in its native Windows window.'
