# Work lanes — paper_20260831

Two agents are working this run root, across four checkouts (see "Worktree map" below).
This file is the claim register: **take only work listed as OPEN, and edit this file to
claim it before you start.** Last updated by the Claude session driving the sweeps.

## Worktree map

| path | branch | state |
| --- | --- | --- |
| `C:\Users\luke\c2hlsc-rtllm` | `fix/self-contained-translation-unit` | Claude, **active** — owns the sweeps and this file |
| `C:\Users\luke\c2hlsc-vitis-qor` | `codex/vitis-qor-authority` | Codex, **active** — clean tree, `104056c` committed 17:04 |
| `C:\Users\luke\c2hlsc-agent` | `claude/combined-generation-workflow` | idle; only untracked `.claude/`, `results/`, `tests/fixtures/` |
| `C:\Users\luke\c2hlsc-qor-bottleneck-explorer` | `feature/qor-bottleneck-explorer-20260807` | **ORPHANED — see below** |

### Orphaned worktree, needs an owner's decision

`c2hlsc-qor-bottleneck-explorer` holds **2562 uncommitted insertions across 19 tracked
files** (`qor.py`, `qor_optimizer.py`, `local_ppa.py`, `equivalence.py`, `hls_runner.py`,
`leveri_testgen.py`, `llm.py`, `remote.py`, several `scripts/` and `tests/`). Nothing has
touched it since **2026-08-07**, 24 days ago, and no process is running there.

It overlaps Codex's `qor.py` / `qor_optimizer.py` lane, so it is a live merge hazard as
well as unbacked work. Neither agent should commit it blind — it predates the whole
`paper_20260831` line and may encode superseded assumptions. **Ask the human whether to
commit it to its branch, harvest parts of it, or discard it.** Until then, do not touch
that directory.

## Held by Claude (do not duplicate)

| lane | state | artifacts |
| --- | --- | --- |
| RTLLM 6-arm sweep (`baseline`, `no-plan`, `rounds=0`, `evidence=self/none/oracle`) | **RUNNING NOW — DO NOT START `scripts/resume_paper_20260831.ps1` AGAINST THIS RUN ROOT** | `rtllm_*/results.jsonl`, `arm_*.retry.log` |
| RTLLM baselines (`--reference`, `--empty-baseline`) | DONE — 47/50 and 4/50, reproduce published | `rtllm_reference/`, `rtllm_empty/` |
| RTLLM external sets (gpt-4, gpt-3.5) | DONE — 0.414 / 0.255 pass@1, reproduce published | `rtllm_ext_gpt-4/`, `rtllm_ext_gpt-35/` |
| CHStone `chstone_main` arms (det + LLM ×2) | DONE — det 6/12; LLM **7/12 clean** both samples (see below) | `chstone_det/`, `chstone_llm_s*/` |
| CHStone `--inner-kernel` arms (det + LLM ×2) | DONE — 0/5 both, blocked (see OPEN-1) | `chstone_inner_*/` |
| Rosetta arms (det + LLM ×2) | DONE — no quotable score (see OPEN-2) | `rosetta_*/` |
| `consolidate_paper_results.py`, `report_pass_fail_paths.py` | DONE, committed | `scripts/` |

## Held by Codex (separate worktree; do not duplicate)

| lane | state | worktree / branch | files and artifacts |
| --- | --- | --- | --- |
| Vitis-only QoR authority and `--target-clock-ns` | **COMMITTED 17:04** as `104056c`, branched off Claude's `5809f2c` — merges clean, unmerged to `main` | `C:\Users\luke\c2hlsc-vitis-qor` / `codex/vitis-qor-authority` | `README.md`, `c2hlsc_agent/{cli,qor,qor_optimizer}.py`, `tests/test_qor.py`, `docs/paper_20260831_continuation.md`; validation artifact only under `C:\Users\luke\runs_win\chstone_final\benchmarks\dfmul\project\qor_report.*` |

Codex will not start or write any `runs\paper_20260831\rtllm_*` sweep. Claude owns those
checkpoint writers until the live retry completes. Integration of the Vitis QoR branch
must happen after the sweep owner reaches a checkpoint; neither agent should copy or
overwrite the other's worktree files.

**Do not rerun any sweep in this run root.** They append to `results.jsonl` and a
concurrent writer corrupts the resume checkpoint. If a sweep needs redoing, say so here
and let the holder do it.

## Live collision warning

A Claude-side retry is **in flight** (verified 17:09 on 2026-08-31: PIDs 18676/2580 are the
`.venv` shims, 19088/7276 the real interpreters — one logical writer per arm, no collision).
It runs `run_rtllm_v2.py --resume`, two arms at a time, three workers each, in the order
baseline+noplan, rounds0+ev_self, ev_none+ev_oracle. Backups are taken (e.g.
`rtllm_baseline/results.jsonl.backend-errors.20260831T084534Z.19088.bak`).

`scripts/resume_paper_20260831.ps1` does the same job. **Running both writes two streams
into the same `results.jsonl` and destroys the resume checkpoint.** Whoever reads this
second: leave the RTL retry alone and take an OPEN item below.

### Retry progress, measured 17:09

The lower concurrency is working: the two retried arms have produced **zero** `llm_error`
cells so far, against 52–76 in every arm still queued.

| arm | stage | designs | samples | func_pass | `llm_error` cells left |
| --- | --- | :-: | :-: | :-: | :-: |
| `rtllm_baseline` | retrying now | 25/50 | 50 | 46 | 0 |
| `rtllm_noplan` | retrying now | 34/50 | 68 | 60 | 0 |
| `rtllm_rounds0` | queued (stage 2) | 50 | 100 | 33 | 52 |
| `rtllm_ev_self` | queued (stage 2) | 50 | 100 | 25 | 73 |
| `rtllm_ev_none` | queued (stage 3) | 50 | 100 | 20 | 74 |
| `rtllm_ev_oracle` | queued (stage 3) | 50 | 100 | 23 | 76 |

**Correction to an earlier note in this file:** the damage was recorded as "36 `llm_error`
rows per arm". The measured count is 52–76 *cells* per queued arm, so the remaining retry
is roughly twice the size previously assumed. On the observed rate (~2 samples/min/arm,
two arms in parallel) stage 1 finishes ~17:35 and all three stages ~18:50.

**Do not quote any number from the four queued arms.** Their surviving-cell pass rates
(0.688–0.958) are computed over only 24–48 clean samples out of 100 and are selection-biased
by whatever the planner happened to survive. They become quotable only after the retry.

Every failure in the queued arms is one signature — `rtl_planner failed after 3 attempt(s):
claude CLI failed (rc=1)` with empty stderr (74/76 in `ev_oracle`, 52/52 in `rounds0`); the
rest are `rtl_repair_agent`. It is the planner call that saturates, which is consistent with
the concurrency ceiling below and with the retried arms coming back clean at 6.

## OPEN — available to take

### OPEN-1 (highest value): `hls_top.hpp` does not include the types it declares in

The `--inner-kernel` experiment is blocked on one named defect, and unblocking it is what
gives the HLS-C half a sound oracle.

```
src/hls_top.hpp:6:1: error: 'float64' does not name a type; did you mean '_Float64'?
```

The generated header declares `float64 float64_mul(float64 a, float64 b);` but carries
only `#include <stdint.h>`, so the app's own typedefs (`float64` from `softfloat.h`) are
undeclared. Every includer then fails. This is the *same defect family* as Rosetta's
documented `generated_header_missing_app_types`, so one fix should close both.

- 4 of 5 inner kernels die here (`dfadd`, `dfdiv`, `dfmul`, `dfsin`), on both the
  deterministic and the LLM path, so it is not a model failure.
- Owner is the converter's header emitter, not the harness — likely
  `c2hlsc_agent/hls_project.py`.
- `dfdiv` shows a second, probably downstream error worth confirming separately:
  `tb/testbench.cpp:160:32: error: 'hls_ret' was not declared in this scope`.
- Verify with: `python scripts/run_chstone.py --benchmark <CHStone> --inner-kernel
  --out-dir <A NEW DIR> --workers 5` — **not** into `chstone_inner_det/`.

### OPEN-2: `run_rosetta.py` has no mutation check

Rosetta rows now pass host equivalence where `docs/chstone_rosetta.md` records 0/5, but
none of those passes is quotable: that runner has no anti-false-green stage, so the
consolidator marks them `unverified`. `run_chstone.py` already has `run_mutation_check`;
porting it is mostly mechanical.

Separately, `face-detection` is marked `unsound` — the analyzer emits
`missing-pointer-bound: argument 'result_size' has no configured bound; using conservative
test length 16` while the kernel indexes far past 16. Sound Rosetta equivalence needs the
testbench generator to size arrays from declared bounds and heap-allocate them.

### OPEN-3: `gsm` cannot stage under `--inner-kernel`

`Gsm_LPC_Analysis(word *s, word *LARc)` reports `staging_not_applied` on every run while
the other four inner kernels stage fine. Probably the pointer parameters or the fact the
kernel lives in `lpc.c` rather than the hls.tcl top file. Lower value than OPEN-1 but it is
the fifth of five inner kernels.

### OPEN-4: 14 Windows-only test failures

`python -m unittest discover -s tests` is green in CI (Linux) but has 14 pre-existing
failures on this Windows box — mostly g++/path issues in `test_hlsc_repair_agent`,
`test_llm_agents`, `test_qor`, `test_run_chstone_staging`. Not blocking any sweep. Baseline
list is in the session scratchpad; regenerate with a `git stash` + rerun if needed.

### VERIFIED: CHStone LLM arms are 7/12, not 8/12 and 9/12

Codex flagged that the LLM aggregates include deterministic fallbacks. Confirmed
independently by hashing `src/hls_top.cpp` against the `chstone_det` arm's output for the
same benchmark -- byte-identical means the model never produced that file
(`convert.py`: "the conservative deterministic source is always built first and used as the
fallback"). The conversion report carries no `missing_llm_reason`, so the hash is the only
reliable signal.

| arm | raw | fell back | clean LLM passes |
| --- | :-: | --- | :-: |
| `chstone_llm_s1` | 8/12 | `dfadd` | **7/12** — adpcm, dfdiv, dfmul, gsm, mips, motion, sha |
| `chstone_llm_s2` | 9/12 | `dfadd`, `dfmul` | **7/12** — adpcm, aes, dfdiv, dfsin, gsm, mips, sha |

Both land on 7, matching Codex's count exactly.

Two different claims, and the paper must not merge them:
 - **agent system** (any generator, repair on): 8/12 and 9/12 -- the fallback is part of the
   system and dfadd/dfmul genuinely pass, consistent with `chstone_det` 6/12;
 - **LLM generator**: 7/12 and 7/12.

Still the docs' 6/12 beaten on both readings, and `mips`/`sha`/`adpcm`/`motion` -- the
symbol-collision and C-not-valid-C++ casualties -- now pass, which is this branch's thesis.

## Facts both agents should not re-derive

- **CHStone's `chstone_main` oracle is weak.** The testbench loops 100× over the *same*
  zero-argument call comparing one `int`. `stimulus_count: 100` is repetition, not
  coverage. Mutation-red proves it is not vacuous; it does not prove coverage.
- **`LFSR` is the only port-order trap in RTLLM.** 4 of 50 testbenches bind positionally;
  on `alu`, `pe` and `float_multi` the reference's declaration order is the spec's order.
  Shimmed to bind by name; validated against reference, spec-order, and two wrong RTLs.
- **The backend saturates at ~12 concurrent `claude` CLI processes.** That produced 36
  `llm_error` rows per arm. Keep total concurrency at or below 6.
- **`--resume` does retry `llm_error` rows.** It backs them up to
  `results.jsonl.backend-errors.*.bak`, drops them, and regenerates. Good rows survive.
- **Vitis HLS 2024.2 and Vivado 2024.2 work here** (`D:\Xilinx\...`). The full
  `software_equivalence → CSim → CSynth → CoSim` ladder passes on `examples/simple_fir`.
  No CHStone/Rosetta sweep in this run root used them — every row is host-equivalence only.
