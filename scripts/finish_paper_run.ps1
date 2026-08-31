# Finish the 6-arm retry, verify generation parity, then run baseline at --samples 10.
# Launched detached so it survives the agent session that started it: earlier attempts died
# with the session's job wrapper, leaving the lane register claiming a run that was dead.
$ErrorActionPreference = 'Continue'
Set-Location 'C:\Users\luke\c2hlsc-rtllm'
$py = 'C:\Users\luke\c2hlsc-agent\.venv\Scripts\python.exe'
$R  = 'runs\paper_20260831'
$common = @('--benchmark','C:\Users\luke\RTLLM','--samples','2','--workers','3','--resume',
            '--llm-backend','claude-cli','--llm-model','opus')

function Run-Arm($name, $extra) {
  $out = Join-Path $R "rtllm_$name"
  $log = Join-Path $R "arm_$name.retry2.log"
  $args = $common + @('--out-dir', $out) + $extra
  & $py 'scripts\run_rtllm_v2.py' @args *> $log
  Write-Output "$name exit=$LASTEXITCODE"
}

# Two at a time keeps total concurrency at 6, the ceiling this box tolerates.
$j1 = Start-Job { param($p,$r,$c) Set-Location 'C:\Users\luke\c2hlsc-rtllm'
  & $p 'scripts\run_rtllm_v2.py' @c --out-dir "$r\rtllm_ev_self" --evidence-policy self *> "$r\arm_ev_self.retry2.log"
} -ArgumentList $py,$R,$common
$j2 = Start-Job { param($p,$r,$c) Set-Location 'C:\Users\luke\c2hlsc-rtllm'
  & $p 'scripts\run_rtllm_v2.py' @c --out-dir "$r\rtllm_ev_none" --evidence-policy none *> "$r\arm_ev_none.retry2.log"
} -ArgumentList $py,$R,$common
Wait-Job $j1,$j2 | Out-Null
Receive-Job $j1,$j2 | Out-Null
Remove-Job $j1,$j2

Run-Arm 'ev_oracle' @('--evidence-policy','oracle')

# Gate: never start the deep-sampling arm under settings that differ from the arms it will
# be compared against.
& $py 'scripts\check_generation_parity.py' $R --quiet
if ($LASTEXITCODE -ne 0) {
  Write-Output 'PARITY FAILED - n=10 not started'
  exit 1
}
& $py 'scripts\run_rtllm_v2.py' --benchmark 'C:\Users\luke\RTLLM' --out-dir "$R\rtllm_baseline_n10" `
    --samples 10 --workers 3 --resume --llm-backend claude-cli --llm-model opus *> "$R\arm_baseline_n10.log"
Write-Output "n10 exit=$LASTEXITCODE"
& $py 'scripts\check_generation_parity.py' $R
