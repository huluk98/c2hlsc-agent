[CmdletBinding()]
param(
    [ValidateRange(0, 2147483647)]
    [int]$Issue = 0,
    [switch]$RunTests,
    [string[]]$CheckProject = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Required command not found: $Name"
    }
    return $command
}

$null = Require-Command git
$null = Require-Command gh

$repoRoot = (& git rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or -not $repoRoot) {
    throw 'Run this command from inside a Git repository.'
}
Set-Location -LiteralPath $repoRoot

Write-Host '== Authentication =='
& gh auth status
if ($LASTEXITCODE -ne 0) {
    throw 'GitHub CLI authentication is not ready.'
}
$login = (& gh api user --jq '.login').Trim()

Write-Host '== Remote refresh =='
& git fetch origin --prune
if ($LASTEXITCODE -ne 0) {
    throw 'git fetch origin --prune failed.'
}

$branch = (& git branch --show-current).Trim()
$remote = (& git remote get-url origin).Trim()
$head = (& git rev-parse --short=12 HEAD).Trim()
$statusLines = @(& git status --porcelain=v1)
$dirty = $statusLines.Count -gt 0
$aheadBehind = (& git rev-list --left-right --count HEAD...origin/main).Trim()

Write-Host '== Local state =='
Write-Host "Repository: $repoRoot"
Write-Host "Operator:   @$login"
Write-Host "Origin:     $remote"
Write-Host "Branch:     $branch"
Write-Host "HEAD:       $head"
Write-Host "HEAD...origin/main counts: $aheadBehind"
if ($dirty) {
    Write-Host 'Working tree: DIRTY'
    $statusLines | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host 'Working tree: clean'
}

$blockers = [System.Collections.Generic.List[string]]::new()
if ($dirty) {
    $blockers.Add('The working tree contains changes; identify their owner before editing or synchronizing.')
}
if ($remote -notmatch '(^|[:/])huluk98/c2hlsc-agent(?:\.git)?$') {
    $blockers.Add("origin does not point to huluk98/c2hlsc-agent: $remote")
}

if ($Issue -gt 0) {
    Write-Host "== Issue #$Issue =="
    $issueJson = & gh issue view $Issue --repo huluk98/c2hlsc-agent --json number,title,state,assignees,labels,url
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read issue #$Issue."
    }
    Write-Host $issueJson
    $issueData = $issueJson | ConvertFrom-Json
    $assigneeLogins = @($issueData.assignees | ForEach-Object { $_.login })
    $statusLabels = @($issueData.labels | ForEach-Object { $_.name } | Where-Object { $_ -like 'status:*' })
    if ($issueData.state -ne 'OPEN') {
        $blockers.Add("Issue #$Issue is not open.")
    }
    if ($assigneeLogins.Count -eq 0) {
        $blockers.Add("Issue #$Issue is unassigned; claim it before implementation.")
    } elseif ($assigneeLogins -notcontains $login) {
        $blockers.Add("Issue #$Issue belongs to @$($assigneeLogins -join ', @').")
    }
    if ($statusLabels.Count -ne 1) {
        $blockers.Add("Issue #$Issue must have exactly one status:* label.")
    }
    if ($branch -ne 'main' -and $branch -notlike "work/$Issue-$login-*") {
        $blockers.Add("Branch '$branch' does not match work/$Issue-$login-<slug>.")
    }
}

Write-Host '== Open pull requests =='
& gh pr list --repo huluk98/c2hlsc-agent --state open --limit 50
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to list open pull requests.'
}

Write-Host '== Diff check =='
& git diff --check
if ($LASTEXITCODE -ne 0) {
    $blockers.Add('git diff --check failed.')
}

if ($RunTests -or $CheckProject.Count -gt 0) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    $pythonPrefix = @()
    if (-not $pythonCommand) {
        $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
        $pythonPrefix = @('-3')
    }
    if (-not $pythonCommand) {
        throw 'Neither python nor the Windows py launcher was found.'
    }
}

if ($CheckProject.Count -gt 0) {
    Write-Host '== Generated-testbench drift check =='
    $checkArgs = @()
    foreach ($project in $CheckProject) {
        $checkArgs += @('--project', $project)
    }
    & $pythonCommand.Source @pythonPrefix scripts/check_generated_testbenches.py @checkArgs
    if ($LASTEXITCODE -ne 0) {
        $blockers.Add('Generated LeVeri testbenches drifted from their manifest; regenerate instead of hand-editing.')
    }
}

if ($RunTests) {
    Write-Host '== Offline unit tests =='
    & $pythonCommand.Source @pythonPrefix -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) {
        $blockers.Add('Offline unit tests failed.')
    }
}

if ($blockers.Count -gt 0) {
    Write-Host '== Preflight verdict: STOP ==' -ForegroundColor Red
    $blockers | ForEach-Object { Write-Host "- $_" }
    exit 2
}

Write-Host '== Preflight verdict: READY ==' -ForegroundColor Green
if ($Issue -gt 0 -and $branch -eq 'main') {
    Write-Host "Next: create work/$Issue-$login-<slug> from this clean, current main branch."
} else {
    Write-Host 'Next: compare planned files with the open pull requests before editing.'
}
