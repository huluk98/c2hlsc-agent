# Work lanes — paper_20260831

Two agents are working this run root. This file is the claim register: **take only work
listed as OPEN, and edit this file to claim it before you start.** Last updated by the
Claude session driving the sweeps.

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

**Do not rerun any sweep in this run root.** They append to `results.jsonl` and a
concurrent writer corrupts the resume checkpoint. If a sweep needs redoing, say so here
and let the holder do it.

## Live collision warning

A Claude-side retry is **in flight** as of this edit: `run_rtllm_v2.py --resume`, two arms
at a time, three workers each, in the order baseline+noplan, rounds0+ev_self,
ev_none+ev_oracle. It has already taken the backups (e.g.
`rtllm_baseline/results.jsonl.backend-errors.20260831T084534Z.19088.bak`) and is
regenerating the dropped `llm_error` cells.

`scripts/resume_paper_20260831.ps1` does the same job. **Running both writes two streams
into the same `results.jsonl` and destroys the resume checkpoint.** Whoever reads this
second: leave the RTL retry alone and take an OPEN item below.

Concurrency ceiling learned the hard way: the Claude CLI backend saturates at roughly 12
concurrent processes, which is what produced the 36 `llm_error` rows per arm in the first
place. The retry runs 6. Do not raise it, and do not run another model-backed sweep
alongside it.

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
