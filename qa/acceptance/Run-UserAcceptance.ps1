[CmdletBinding()]
param([switch]$Live,[switch]$Project,[ValidateRange(900,3600)][int]$SoakSeconds=900)
$ErrorActionPreference = 'Stop'
$qaRepo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$qaPython = Join-Path $qaRepo '.tooling\qa-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $qaPython -PathType Leaf)) { throw 'The WISE QA Python launcher is missing.' }
Push-Location $qaRepo
try {
    if (-not (Test-Path -LiteralPath '.tooling\flaui\FlaUI.UIA3.dll')) {
        & $qaPython qa/acceptance/flaui_bootstrap.py
        if ($LASTEXITCODE -ne 0) { throw 'FlaUI installation failed.' }
    }
    & $qaPython qa/acceptance/user_journeys.py --native --network-mcp
    if ($LASTEXITCODE -ne 0) { throw 'Real UI acceptance failed. Read the generated REPORT.md.' }
    if ($Live) {
        & $qaPython qa/acceptance/live_ui_journeys.py
        if ($LASTEXITCODE -ne 0) { throw 'Live provider acceptance failed. Read the generated report.json.' }
    }
    if ($Project) {
        & $qaPython qa/acceptance/project_journeys.py --trials 2 --soak-seconds $SoakSeconds
        if ($LASTEXITCODE -ne 0) { throw 'Repeated project acceptance failed. Read the generated report.json.' }
    }
    & $qaPython qa/acceptance/stamp_regression.py
    if ($LASTEXITCODE -ne 0) { throw 'Regression tests failed.' }
    if ($Live) {
        & $qaPython qa/acceptance/quality_gate.py --regression qa-results/regression-final-20261001.json
        if ($LASTEXITCODE -ne 0) { throw 'Evidence gate failed: missing or stale evidence.' }
    }
    Write-Host 'Verified scope passed. Whole-product readiness still requires the unverified release gates.'
} finally { Pop-Location }
