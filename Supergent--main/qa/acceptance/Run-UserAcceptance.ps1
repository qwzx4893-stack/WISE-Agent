[CmdletBinding()]
param(
    [switch]$Live,
    [switch]$Project,
    [ValidateRange(900,3600)][int]$SoakSeconds=900,
    [ValidateRange(0.01,1)][Nullable[double]]$ApprovedAdditionalUsd=$null
)
$ErrorActionPreference = 'Stop'
if (($Live -or $Project) -and $null -eq $ApprovedAdditionalUsd) {
    throw 'Paid QA requires explicit -ApprovedAdditionalUsd; never infer spending approval.'
}
$qaRepo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$qaPython = Join-Path $qaRepo '.tooling\qa-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $qaPython -PathType Leaf)) { throw 'The WISE QA Python launcher is missing.' }
$qaStartedAt = [DateTime]::UtcNow
$qaRunId = $qaStartedAt.ToString('yyyyMMddTHHmmssfffZ')
$qaFailures = [System.Collections.Generic.List[string]]::new()

function Invoke-QaStep([string]$Name, [string[]]$Arguments) {
    & $qaPython @Arguments
    if ($LASTEXITCODE -ne 0) { $qaFailures.Add($Name) }
}

function Find-NewQaReport([string]$Pattern) {
    $qaMatches = @(Get-ChildItem -LiteralPath (Join-Path $qaRepo 'qa-results') -Directory |
        Where-Object { $_.Name -like $Pattern -and $_.CreationTimeUtc -ge $qaStartedAt } |
        ForEach-Object { Join-Path $_.FullName 'report.json' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($qaMatches.Count -ne 1) { throw "Expected exactly one newly-created $Pattern report; refusing stale/ambiguous evidence." }
    return $qaMatches[0]
}

Push-Location $qaRepo
try {
    if (-not (Test-Path -LiteralPath '.tooling\flaui\FlaUI.UIA3.dll')) {
        Invoke-QaStep 'FlaUI bootstrap' @('qa/acceptance/flaui_bootstrap.py')
    }
    # Keep collecting failures; one broken case must not suppress regression.
    Invoke-QaStep 'Real UI/native' @('qa/acceptance/user_journeys.py','--native','--network-mcp')
    if ($Live) {
        Invoke-QaStep 'Live provider' @('qa/acceptance/live_ui_journeys.py','--max-additional-usd',"$ApprovedAdditionalUsd")
    }
    if ($Project) {
        Invoke-QaStep 'Repeated project' @('qa/acceptance/project_journeys.py','--max-additional-usd',"$ApprovedAdditionalUsd",
            '--trials','2','--soak-seconds',"$SoakSeconds")
    }
    $qaRegression = Join-Path $qaRepo "qa-results/regression-wrapper-$qaRunId.json"
    Invoke-QaStep 'Regression' @('qa/acceptance/stamp_regression.py','--output',$qaRegression)
    if ($Live -and $Project) {
        $qaUi = Find-NewQaReport 'user-*'
        $qaLiveReport = Find-NewQaReport 'live-ui-*'
        $qaProjectReport = Find-NewQaReport 'project-ui-*'
        $qaSemantic = Join-Path $qaRepo "qa-results/semantic-wrapper-$qaRunId.json"
        Invoke-QaStep 'Bounded artifact review' @('qa/acceptance/review_project.py','--report',$qaProjectReport,'--output',$qaSemantic)
        $qaGate = Join-Path $qaRepo "qa-results/quality-wrapper-$qaRunId.json"
        Invoke-QaStep 'Quality gate' @('qa/acceptance/quality_gate.py','--regression',$qaRegression,'--ui',$qaUi,
            '--live',$qaLiveReport,'--project',$qaProjectReport,'--semantic-review',$qaSemantic,'--output',$qaGate)
        Write-Host "Evidence gate: $qaGate"
    } else {
        Write-Host 'PARTIAL evidence only: both -Live and -Project are needed for the full execution matrix.'
    }
    Write-Host 'Release is NOT_READY until independent semantic review and explicit manual gates pass.'
    if ($qaFailures.Count -gt 0) { throw ('Recorded failing stages: ' + ($qaFailures -join ', ')) }
} finally { Pop-Location }
