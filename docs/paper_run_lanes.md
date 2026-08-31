# Work lanes — paper_20260831

Two agents worked this run root across isolated checkouts (see "Worktree map" below).
This file is the claim register: **take only work listed as OPEN, and edit this file to
claim it before you start.** Final state recorded by Codex after the bounded handoff.

> **Final handoff, 19:35 on 2026-08-31:** no RTLLM writer or deep-sampling queue remains.
> Codex stopped the bounded retry after Claude CLI saturation reappeared. Baseline,
> no-plan, and no-repair are complete and clean; the three evidence-policy arms are
> incomplete/contaminated and must not be scored. Codex owns all further work.

## Worktree map

| path | branch | state |
| --- | --- | --- |
| `C:\Users\luke\c2hlsc-rtllm` | `fix/self-contained-translation-unit` | Claude, **stopped** — frozen source artifacts; no writer remains |
| `C:\Users\luke\c2hlsc-vitis-qor` | `codex/vitis-qor-authority` | Codex, **active** — clean tree, `104056c` committed 17:04 |
| `C:\Users\luke\c2hlsc-final` | `codex/final-integration` | Codex, **lead integration** — Vitis and Claude branches merged; owns final validation and report |
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
| RTLLM 6-arm sweep (`baseline`, `no-plan`, `rounds=0`, `evidence=self/none/oracle`) | **STOPPED AT QUOTA/TRANSPORT CEILING — do not resume this run root** | Three complete clean arms; three evidence arms frozen unscored in `rtllm_*/results.jsonl` |
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
| Lead integration, independent validation, final merged report, and OPEN-1 | **FINALISING under Codex** — Vitis work merged at `68ca828`; Claude sweep is stopped | `C:\Users\luke\c2hlsc-final` / `codex/final-integration` | Codex owns code integration, consolidation, report QA, and the account-accessible Markdown handoff |

Codex did not start or write any `runs\paper_20260831\rtllm_*` sweep. Claude owned those
checkpoint writers until the bounded retry stopped. Codex is the lead agent and owns all
integration, validation, and reporting; do not assign Claude new work without a new,
explicitly bounded lane.

**Do not rerun any sweep in this run root.** They append to `results.jsonl` and a
concurrent writer corrupts the resume checkpoint. If a sweep needs redoing, say so here
and let the holder do it.

## Historical collision log — superseded by final handoff

A Claude-side retry was **in flight** (verified 17:09 on 2026-08-31: PIDs 18676/2580 were the
`.venv` shims, 19088/7276 the real interpreters — one logical writer per arm, no collision).
It ran `run_rtllm_v2.py --resume`, two arms at a time, three workers each, in the order
baseline+noplan, rounds0+ev_self, ev_none+ev_oracle. Backups are taken (e.g.
`rtllm_baseline/results.jsonl.backend-errors.20260831T084534Z.19088.bak`).

`scripts/resume_paper_20260831.ps1` does the same job. **Running both writes two streams
into the same `results.jsonl` and destroys the resume checkpoint.** Whoever reads this
second: leave the RTL retry alone and take an OPEN item below.

### Historical retry progress, measured 17:09

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

## Historical detached relaunch (Claude, 19:31) — superseded by final handoff

Claude correctly detected that the first background job had ended and committed
`scripts/finish_paper_run.ps1` as a detached continuation. That launcher briefly resumed
`ev_self` and `ev_none`, clearing their contaminated rows before producing two additional
clean rows in each arm. It also contained an automatic `baseline_n10` continuation that
conflicted with the Codex-priority quota boundary.

Codex stopped the detached process at 19:35, merged the script for provenance, and changed
it to require `-AllowResume`; deep sampling additionally requires `-AllowDeepSampling`.
The frozen counts in the final measured-state table supersede this historical relaunch.

## CHStone LLM: 8/12 and 9/12 in consolidated.md still include fallbacks

Re-verified at 19:31 after the CHStone cells were retried. Hashing `src/hls_top.cpp`
against the `chstone_det` arm's output for the same benchmark:

| arm | raw passes | byte-identical to deterministic | clean LLM |
| --- | :-: | --- | :-: |
| `chstone_llm_s1` | 8 | `dfadd` | **7** |
| `chstone_llm_s2` | 9 | `dfadd`, `dfmul` | **7** |

(`motion` is also byte-identical in s2 but is not a pass there, so it does not change the
count.)

`consolidated.md` reports 8 and 9 because the fallback is invisible to it: a cell where the
model call failed and `convert.py` used the deterministic source is **not** flagged
`llm_error`, so nothing distinguishes it from a genuine model pass. The hash is the only
signal, and no tool computes it automatically yet.

Both numbers are legitimate, for different claims: **agent system** 8/12 and 9/12, **LLM
generator** 7/12 and 7/12. They must not be merged, and the LLM figure must not be taken
from `consolidated.md` as it stands.

## GENERATION CONTRACT — run this before and after any sweep

Both agents generate into one run root, so "we agreed on the settings" is a hope, not a
guarantee. It is now checkable:

```
python scripts/check_generation_parity.py runs/paper_20260831
```

Exit 0 = every model-backed arm shares the invariant configuration. Exit 1 = two arms are
not comparable, and no downstream analysis recovers the comparison.

**Invariant — identical in every arm.** `model` (opus), `backend` (claude-cli),
`benchmark` (`C:\Users\luke\RTLLM`), `apply_shims` (true), `sim_timeout` (30), `compile_timeout` (120),
`llm_retries` (2). Changing any of these mid-matrix silently invalidates every cross-arm
delta and nothing in the output would reveal it.

**Arm factor — vary exactly ONE per arm.** `plan`, `evidence_policy`, `max_repair_rounds`.
An arm differing from the baseline in two at once has an unattributable delta; the checker
names any such arm.

**Sampling.** `samples` may differ between the headline arm and a deep-sampling arm, but a
cross-arm pass@k is then valid only at `k <= min(n)`. Reported, not refused.

Measured now (exit 0): baseline / `plan=False` / `max_repair_rounds=0` /
`evidence_policy=none|self|oracle`, all n=2, all sharing the invariant set — a clean
single-factor ablation. `rtllm_empty`, `rtllm_reference` and the two `rtllm_ext_*` sweeps
construct no model and are excluded rather than compared.

**Run it before adding any sweep to this run root, and again after.** If it exits 1, do not
analyse the result — regenerate the odd arm into a fresh `--out-dir` under the settings
above. This applies to any future deep-sampling arm as much as to anything Codex adds.

## FINAL MEASURED STATE — bounded retry frozen at 19:35

| arm | rows | contaminated | retried | quotable? |
| --- | --: | --: | :-: | --- |
| `noplan` | 50 | 0 | yes | **yes** — complete and clean |
| `baseline` | 50 | 0 | yes | **yes** — complete and clean |
| `rounds0` | 50 | 0 | yes | **yes** — complete and clean |
| `ev_self` | 34 | 0 | partial | no — 16 designs unrun; surviving subset is selection-biased |
| `ev_none` | 13 | 0 | partial | no — 37 designs unrun; surviving subset is selection-biased |
| `ev_oracle` | 50 | 39 | **no** | no |

`50 rows` does not by itself mean done: `ev_oracle` still holds pre-outage rows and the
consolidator marks 39 of them `unknown`. **Do not quote ev_self / ev_none / ev_oracle.**
The completed baseline, no-plan, and no-repair arms each contain 50 unique designs,
100 samples, and zero backend-error cells. A detached Claude launcher briefly resumed
`ev_self` and `ev_none` after the first stop; Codex stopped it, froze the counts above,
regenerated all consolidated outputs, and changed the launcher to require explicit opt-in.

## pass@k — settled, do not re-derive

`scripts/report_passk_and_errors.py` (committed) emits `passk.md` and
`errors_and_inputs.jsonl`. Unbiased estimator, Chen et al. 2021.

**pass@k is defined only for k <= n.** The arms are `--samples 2`, so pass@1 and pass@2 are
real and pass@5 / pass@10 are *undefined*, not estimable. The script prints `n/a`. Only the
shipped GPT sets (n=5) can report pass@5. Final quotable `adjusted` figures: `baseline`
pass@1/pass@2 0.870/0.935, `noplan` 0.848/0.913, `rounds0` 0.641/0.674,
`reference` pass@1 0.935, `empty` 0.000, `gpt-4` 0.414 / pass@5 0.621, and
`gpt-3.5` 0.255 / pass@5 0.379.

The failure corpus carries the specification, the produced artifact, the rejecting stage
and the tool's own words, 602 cells. Note for whoever builds the memory base: the 275
`llm_error` rows are backend saturation, not model results — label them, do not train on
them. Also note RTLLM's verdict lives on the **last round's `sim`**, not on the sample;
reading it off the sample silently yields 292 records with no failure family.

## CANCELLED BY CODEX: `baseline` at `--samples 10`

The waiting queue process was stopped before generation began. The bounded six-arm retry
is the Claude handoff; Codex remains the lead agent for integration, validation, and the
report. The deep run would require roughly 500 additional Claude calls and is not needed
for pass@1/pass@2. If pass@5 or pass@10 becomes a decision requirement, start a separately
approved arm in a new output directory after confirming quota and concurrency.

## OPEN — available to take

### FIXED BY CODEX — OPEN-1: `hls_top.hpp` now carries application-defined types

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

Codex fixed this on `codex/main-integration` in `ec7a75d`. With closure extraction
disabled, the fallback now finds and stages local headers that declare signature types;
the testbench includes that generated header before its linked-golden declaration. Fresh
isolated runs reached 100-stimulus host equivalence for `dfadd`, `dfdiv`, and `dfmul`.
`dfsin` also passed the type and link stages but then ran indefinitely on unrestricted
random bit-pattern stimulus, so it was stopped and remains unscored. These new runs do not
replace the paper run root and must not be merged into its denominators.

### OPEN-5: mutation wrapper is nullary-only for inner kernels

The existing CHStone mutation wrapper emits `int top()` even when the tested inner kernel
takes arguments and returns an application type. Consequently the three newly reachable
inner-kernel equivalence passes are `mutation_check: inconclusive`, not quotable results.
Generalize the wrapper from the generated signature before reporting an inner-kernel pass.

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
- **The backend saturated at ~12 concurrent `claude` CLI processes and later returned
  transport failures at lower concurrency as quota pressure accumulated.** Keep future
  Claude work bounded and separate from Codex integration.
- **`--resume` does retry `llm_error` rows.** It backs them up to
  `results.jsonl.backend-errors.*.bak`, drops them, and regenerates. Good rows survive.
- **Vitis HLS 2024.2 and Vivado 2024.2 work here** (`D:\Xilinx\...`). The full
  `software_equivalence → CSim → CSynth → CoSim` ladder passes on `examples/simple_fir`.
  No CHStone/Rosetta sweep in this run root used them — every row is host-equivalence only.
