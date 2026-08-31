# Paper run continuation: 2026-08-31

This is the durable handoff for `runs\paper_20260831`. It records the state at the
Claude CLI quota cutoff so the experiment can be resumed without relying on chat history.

## Identity and fixed configuration

- Repository: `C:\Users\luke\c2hlsc-rtllm`
- Branch: `fix/self-contained-translation-unit`
- Last pushed commit at handoff: `32a82e6`
- RTLLM checkout: `C:\Users\luke\RTLLM`
- CHStone checkout: `C:\Users\luke\bench\CHStone`
- Rosetta checkout: `C:\Users\luke\bench\rosetta`
- Python: `C:\Users\luke\c2hlsc-agent\.venv\Scripts\python.exe`
- LLM backend/model: `claude-cli` / `opus`
- Samples now: 2 per model-backed arm. The requested 5-sample extension is later work and
  must use separate output directories or an explicitly compatible sampling workflow.
- HLS-C stopping point: host equivalence only. Vitis CSim, CSynth, and CoSim were not run
  for these sweeps, by design.
- RTLLM: all 50 designs, two samples, with per-design checkpoint rows in `results.jsonl`.

## Completed and trustworthy evidence

- Deterministic CHStone: 6/12 pass; every pass has a red mutation check.
- CHStone LLM top-level raw summaries said 8/12 and 9/12, but those aggregates include
  deterministic fallbacks after failed model calls. Before retry, each sample has seven
  clean, mutation-red passes; neither arm is complete or quotable yet.
- Rosetta deterministic: 0/5. LLM sample 1 reports 4/5 host-equivalence passes; sample 2
  has one backend-error cell and only three clean passes. Even clean Rosetta passes are not
  paper-ready: the runner has no mutation check, and `face-detection` has an unsound missing
  pointer bound.
- RTLLM reference ceiling: 47/50. Empty-module floor: 4/50. The four vacuous designs are
  `comparator_3bit`, `comparator_4bit`, `sequence_detector`, and `square_wave`.
- Shipped GPT-4 and GPT-3.5 sets reproduce their published scores under this harness:
  18/29 and 11/29 pass@5 respectively.

## Quota cutoff: do not quote the partial RTL-arm rates

All six model-backed RTLLM arms wrote 50 rows, but many rows contain Claude backend
errors. Those cells are infrastructure failures, not wrong RTL, and must be retried before
the arm is complete.

| arm | complete designs before cutoff | designs to retry | configuration |
| --- | ---: | ---: | --- |
| `rtllm_baseline` | 13 | 37 | plan, logs evidence, 2 repair rounds |
| `rtllm_noplan` | 14 | 36 | no plan, logs evidence, 2 repair rounds |
| `rtllm_rounds0` | 23 | 27 | plan, generation only |
| `rtllm_ev_self` | 13 | 37 | plan, self evidence, 2 repair rounds |
| `rtllm_ev_none` | 12 | 38 | plan, no evidence, 2 repair rounds |
| `rtllm_ev_oracle` | 11 | 39 | plan, oracle evidence, 2 repair rounds |

The retry count includes six mixed rows where one sample produced a result and the other
lost its model call. Keeping those rows would bias pass@1, so the whole two-sample design
cell is regenerated. The raw log summaries score backend failures as zero and are therefore
partial. The cross-suite consolidator must show every quota-contaminated row as unknown;
regenerate the final version only after the retries finish.

## Inner-kernel state

The argument-driven CHStone scope is exactly `dfadd`, `dfdiv`, `dfmul`, `dfsin`, and
`gsm`. Both two-sample sweeps currently read 0/5. Six of the ten cells produced genuine
LLM candidates that failed to compile. Four cells were interrupted by the backend and
fell back to the deterministic copy:

- `chstone_inner_llm_s1`: `dfsin`, `gsm`
- `chstone_inner_llm_s2`: `dfsin`, `gsm`

Those four cells must be retried. Their `conversion_report.md` files contain
`LLM generation attempt 1 failed [RuntimeError: claude CLI failed (rc=1)]`, so their
current candidate verdicts are not model results.

The artifact audit also found hidden deterministic fallbacks in earlier top-level sweeps.
They are part of the same resume, not completed model results:

- `chstone_llm_s1`: `blowfish`, `dfadd`, `jpeg`
- `chstone_llm_s2`: `dfadd`, `dfmul`, `jpeg`, `motion`
- `rosetta_llm_s2`: `3d-rendering`

## Safe continuation rules

1. Preserve every pre-cutoff `results.jsonl` before removing retryable backend-error rows.
2. Never change samples, repair rounds, plan, evidence policy, benchmark root, model, or
   shim policy when resuming an existing RTLLM output directory.
3. Keep one writer per output directory. Different arms may run concurrently; two
   processes must never share an arm directory.
4. Retry only backend-error cells. Completed candidates, genuine failures, baselines, and
   external GPT sets are checkpoints and must not be regenerated.
5. A run is complete only when its report lists no `llm_error_designs` and has the intended
   number of samples for every selected design.
6. Regenerate `consolidated.json`, `consolidated.md`, and `report_pass_fail.md` after all
   retries, then check that unknown/backend-error counts are zero before quoting rates.

## Resume commands

Run these from `C:\Users\luke\c2hlsc-rtllm` after the backend is available. The runner's
resume implementation must first be verified to retry and atomically replace backend-error
rows; the pre-cutoff implementation incorrectly skipped every recorded design.

The preferred entry point is the checked-in controller. It probes the same sandboxed
backend client before changing a checkpoint, runs HLS-C before RTL, limits model-call
concurrency, stops later groups after any nonzero runner exit, and validates that no target
cell remains unknown before declaring completion:

```powershell
powershell.exe -NoProfile -File scripts\resume_paper_20260831.ps1
```

Use `-DryRun` to print every command without calling the backend or writing a checkpoint.
The individual commands below are the controller's underlying configuration and remain
useful for a manual single-arm resume.

```powershell
$python = 'C:\Users\luke\c2hlsc-agent\.venv\Scripts\python.exe'
$runRoot = 'runs\paper_20260831'
$common = @('--benchmark', 'C:\Users\luke\RTLLM', '--samples', '2', '--workers', '2',
            '--resume', '--llm-backend', 'claude-cli', '--llm-model', 'opus')

& $python scripts\run_rtllm_v2.py @common --out-dir "$runRoot\rtllm_baseline"
& $python scripts\run_rtllm_v2.py @common --out-dir "$runRoot\rtllm_noplan" --no-plan
& $python scripts\run_rtllm_v2.py @common --out-dir "$runRoot\rtllm_rounds0" --max-repair-rounds 0
& $python scripts\run_rtllm_v2.py @common --out-dir "$runRoot\rtllm_ev_self" --evidence-policy self
& $python scripts\run_rtllm_v2.py @common --out-dir "$runRoot\rtllm_ev_none" --evidence-policy none
& $python scripts\run_rtllm_v2.py @common --out-dir "$runRoot\rtllm_ev_oracle" --evidence-policy oracle
```

HLS-C retries use the same original configurations. Resume infers backend fallback rows
from their saved `conversion_report.md`, backs up the original JSONL, and regenerates only
those cells:

```powershell
$hlsCommon = @('--use-llm', '--llm-backend', 'claude-cli', '--llm-model', 'opus',
               '--auto-repair', '--max-iterations', '3', '--timeout', '2400', '--resume')

& $python scripts\run_chstone.py --benchmark 'C:\Users\luke\bench\CHStone' @hlsCommon `
    --out-dir "$runRoot\chstone_llm_s1" --workers 4 --label llm_s1
& $python scripts\run_chstone.py --benchmark 'C:\Users\luke\bench\CHStone' @hlsCommon `
    --out-dir "$runRoot\chstone_llm_s2" --workers 4 --label llm_s2
& $python scripts\run_chstone.py --benchmark 'C:\Users\luke\bench\CHStone' --inner-kernel `
    @hlsCommon --out-dir "$runRoot\chstone_inner_llm_s1" --workers 3 --label inner_llm_s1
& $python scripts\run_chstone.py --benchmark 'C:\Users\luke\bench\CHStone' --inner-kernel `
    @hlsCommon --out-dir "$runRoot\chstone_inner_llm_s2" --workers 3 --label inner_llm_s2
& $python scripts\run_rosetta.py --benchmark 'C:\Users\luke\bench\rosetta' --agent `
    @hlsCommon --out-dir "$runRoot\rosetta_llm_s2" --workers 2
```

Final report regeneration:

```powershell
& $python scripts\consolidate_paper_results.py $runRoot
& $python scripts\report_pass_fail_paths.py $runRoot
```
