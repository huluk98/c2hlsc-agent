param(
    [string]$RunRoot = 'runs\paper_20260831',
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$python = 'C:\Users\luke\c2hlsc-agent\.venv\Scripts\python.exe'
$rtllm = 'C:\Users\luke\RTLLM'
$chstone = 'C:\Users\luke\bench\CHStone'
$rosetta = 'C:\Users\luke\bench\rosetta'

Set-Location -LiteralPath $repo
if (-not [IO.Path]::IsPathRooted($RunRoot)) {
    $RunRoot = Join-Path $repo $RunRoot
}
$RunRoot = [IO.Path]::GetFullPath($RunRoot)
$required = @($python, $rtllm, $chstone, $rosetta, $RunRoot)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required continuation path does not exist: $path"
    }
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$logRoot = Join-Path $RunRoot "resume_logs\$stamp"
if (-not $DryRun) {
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
}

function New-Run([string]$Name, [string[]]$Arguments) {
    [PSCustomObject]@{ Name = $Name; Arguments = $Arguments }
}

function Invoke-RunGroup([string]$Group, [object[]]$Runs) {
    Write-Output "=== $Group ==="
    if ($DryRun) {
        foreach ($run in $Runs) {
            Write-Output ($python + ' ' + ($run.Arguments -join ' '))
        }
        return
    }

    $started = @()
    foreach ($run in $Runs) {
        $stdout = Join-Path $logRoot ($run.Name + '.stdout.log')
        $stderr = Join-Path $logRoot ($run.Name + '.stderr.log')
        $process = Start-Process -FilePath $python -ArgumentList $run.Arguments `
            -WorkingDirectory $repo -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
        $started += [PSCustomObject]@{
            Name = $run.Name; Process = $process; Stdout = $stdout; Stderr = $stderr
        }
        Write-Output "started $($run.Name) pid=$($process.Id)"
    }

    while (@($started | Where-Object { -not $_.Process.HasExited }).Count -gt 0) {
        Start-Sleep -Seconds 2
    }

    $failed = @()
    foreach ($item in $started) {
        $item.Process.Refresh()
        Write-Output "$($item.Name) exit=$($item.Process.ExitCode)"
        if (Test-Path -LiteralPath $item.Stdout) {
            Get-Content -LiteralPath $item.Stdout -Tail 12
        }
        if ((Test-Path -LiteralPath $item.Stderr) -and
                (Get-Item -LiteralPath $item.Stderr).Length -gt 0) {
            Write-Output "stderr: $($item.Stderr)"
            Get-Content -LiteralPath $item.Stderr -Tail 12
        }
        if ($item.Process.ExitCode -ne 0) {
            $failed += $item.Name
        }
    }
    if ($failed.Count -gt 0) {
        throw "$Group stopped: $($failed -join ', ') returned nonzero. Check $logRoot and resume again; later groups were not started."
    }
}

$hlsCommon = @(
    '--use-llm', '--llm-backend', 'claude-cli', '--llm-model', 'opus',
    '--auto-repair', '--max-iterations', '3', '--timeout', '2400', '--resume'
)
$hlsTop = @(
    (New-Run 'chstone_llm_s1' (@(
        'scripts\run_chstone.py', '--benchmark', $chstone
    ) + $hlsCommon + @(
        '--out-dir', (Join-Path $RunRoot 'chstone_llm_s1'), '--workers', '2', '--label', 'llm_s1'
    ))),
    (New-Run 'chstone_llm_s2' (@(
        'scripts\run_chstone.py', '--benchmark', $chstone
    ) + $hlsCommon + @(
        '--out-dir', (Join-Path $RunRoot 'chstone_llm_s2'), '--workers', '2', '--label', 'llm_s2'
    )))
)
$hlsInner = @(
    (New-Run 'chstone_inner_llm_s1' (@(
        'scripts\run_chstone.py', '--benchmark', $chstone, '--inner-kernel'
    ) + $hlsCommon + @(
        '--out-dir', (Join-Path $RunRoot 'chstone_inner_llm_s1'), '--workers', '2',
        '--label', 'inner_llm_s1'
    ))),
    (New-Run 'chstone_inner_llm_s2' (@(
        'scripts\run_chstone.py', '--benchmark', $chstone, '--inner-kernel'
    ) + $hlsCommon + @(
        '--out-dir', (Join-Path $RunRoot 'chstone_inner_llm_s2'), '--workers', '2',
        '--label', 'inner_llm_s2'
    ))),
    (New-Run 'rosetta_llm_s2' (@(
        'scripts\run_rosetta.py', '--benchmark', $rosetta, '--agent'
    ) + $hlsCommon + @(
        '--out-dir', (Join-Path $RunRoot 'rosetta_llm_s2'), '--workers', '1'
    )))
)

$rtlCommon = @(
    'scripts\run_rtllm_v2.py', '--benchmark', $rtllm, '--samples', '2', '--workers', '2',
    '--resume', '--llm-backend', 'claude-cli', '--llm-model', 'opus'
)
$rtlGroup1 = @(
    (New-Run 'rtllm_baseline' ($rtlCommon + @(
        '--out-dir', (Join-Path $RunRoot 'rtllm_baseline'),
        '--max-repair-rounds', '2', '--evidence-policy', 'logs'
    ))),
    (New-Run 'rtllm_noplan' ($rtlCommon + @(
        '--out-dir', (Join-Path $RunRoot 'rtllm_noplan'), '--no-plan',
        '--max-repair-rounds', '2', '--evidence-policy', 'logs'
    )))
)
$rtlGroup2 = @(
    (New-Run 'rtllm_rounds0' ($rtlCommon + @(
        '--out-dir', (Join-Path $RunRoot 'rtllm_rounds0'),
        '--max-repair-rounds', '0', '--evidence-policy', 'logs'
    ))),
    (New-Run 'rtllm_ev_self' ($rtlCommon + @(
        '--out-dir', (Join-Path $RunRoot 'rtllm_ev_self'),
        '--max-repair-rounds', '2', '--evidence-policy', 'self'
    )))
)
$rtlGroup3 = @(
    (New-Run 'rtllm_ev_none' ($rtlCommon + @(
        '--out-dir', (Join-Path $RunRoot 'rtllm_ev_none'),
        '--max-repair-rounds', '2', '--evidence-policy', 'none'
    ))),
    (New-Run 'rtllm_ev_oracle' ($rtlCommon + @(
        '--out-dir', (Join-Path $RunRoot 'rtllm_ev_oracle'),
        '--max-repair-rounds', '2', '--evidence-policy', 'oracle'
    )))
)

if (-not $DryRun) {
    Write-Output 'Probing the same sandboxed Claude CLI client used by the runners...'
    $probeCode = @'
from c2hlsc_agent.llm import ClaudeCLIClient
print(ClaudeCLIClient(model="opus", timeout=180).complete(
    "You are a health check.", "Return exactly READY and nothing else."
).strip())
'@
    $probe = $probeCode | & $python - 2>&1
    if ($LASTEXITCODE -ne 0 -or (($probe -join "`n").Trim() -ne 'READY')) {
        throw "Claude backend probe failed before any checkpoint was changed: $($probe -join ' ')"
    }
    Write-Output 'backend READY'
}

Invoke-RunGroup 'HLS-C top-level retries' $hlsTop
Invoke-RunGroup 'HLS-C inner-kernel and Rosetta retries' $hlsInner
Invoke-RunGroup 'RTL retries: baseline and no-plan' $rtlGroup1
Invoke-RunGroup 'RTL retries: generation-only and self-evidence' $rtlGroup2
Invoke-RunGroup 'RTL retries: blind and oracle-evidence' $rtlGroup3

if ($DryRun) {
    Write-Output 'Dry run complete; no backend call or checkpoint write was made.'
    exit 0
}

& $python scripts\consolidate_paper_results.py $RunRoot
if ($LASTEXITCODE -ne 0) { throw 'Consolidation failed.' }
& $python scripts\report_pass_fail_paths.py $RunRoot
if ($LASTEXITCODE -ne 0) { throw 'Per-design report generation failed.' }

$targetSweeps = @(
    'chstone_llm_s1', 'chstone_llm_s2', 'chstone_inner_llm_s1',
    'chstone_inner_llm_s2', 'rosetta_llm_s2', 'rtllm_baseline', 'rtllm_noplan',
    'rtllm_rounds0', 'rtllm_ev_self', 'rtllm_ev_none', 'rtllm_ev_oracle'
)
$consolidated = Get-Content -Raw -LiteralPath (Join-Path $RunRoot 'consolidated.json') |
    ConvertFrom-Json
$unknown = @(
    $consolidated.records | Where-Object {
        $_.sweep -in $targetSweeps -and ($_.verdict -eq 'unknown' -or $_.llm_error)
    }
)
if ($unknown.Count -gt 0) {
    $cells = $unknown | ForEach-Object { "$($_.sweep)/$($_.id)" }
    throw "Final validation found $($unknown.Count) incomplete cell(s): $($cells -join ', ')"
}

Write-Output "All continuation cells are complete. Final reports: $RunRoot"
Write-Output "Controller logs: $logRoot"
