# RTLLM v2.0 — CLI session handoff

Everything here is runnable from a terminal. It assumes you were not present for the session
that built this, and that you want to reproduce or extend the RTLLM v2.0 results.

Companion document: [`rtllm_v2_benchmark.md`](rtllm_v2_benchmark.md) explains the design of the
harness. This file is the operational runbook.

---

## 1. State of the branch

Branch: `claude/c2hlsc-agent-rtllmv2-f7apxo`

| commit | contents |
| --- | --- |
| `3ed76e6` | Harness skeleton: `rtllm_bench.py`, `rtllm_agent.py`, `run_rtllm_v2.py`, tests, docs |
| `9d8759f` | First full 50-design run. **Its headline numbers are superseded** — see §9 |
| `eea70f4` | Correctness fixes: oracle-bypass gate, LLM sandboxing, empty-stub floor, 14 review findings |

**Uncommitted in the working tree**: the `--external-rtl` comparison mode
(`c2hlsc_agent/rtllm_bench.py`, `scripts/run_rtllm_v2.py`). It is implemented and working —
the GPT-3.5/GPT-4 numbers in §8 were produced with it — but it was still being reviewed when the
session ended. Commit it when you are satisfied.

Test suite: `python -m pytest tests -q` → **377 passed, 15 subtests passed**.

---

## 2. One-time setup

```bash
# 1. Simulator. RTLLM's own makefile and auto_run.py assume Synopsys VCS; this harness uses
#    Icarus Verilog instead, which is why it exists. Results under VCS may differ (see §9).
sudo apt-get update && sudo apt-get install -y iverilog
iverilog -V | head -1        # expect 12.0 or newer
command -v vvp               # must be on PATH

# 2. Benchmark checkout (50 designs, ~4 MB)
git clone https://github.com/hkust-zhiyao/RTLLM.git ~/RTLLM
export RTLLM_ROOT=~/RTLLM    # --benchmark defaults to this

# 3. This repo
cd /path/to/c2hlsc-agent
python3 -m pip install -e .
python3 -m pytest tests -q   # expect 377 passed
```

The harness can fetch the benchmark itself if you prefer:
`--clone --benchmark ~/RTLLM` shallow-clones when the path is missing.

### LLM backend

Only the agent run needs a model. `--reference`, `--empty-baseline` and `--external-rtl` never
construct an LLM client at all. Backend resolution lives in `c2hlsc_agent/llm.py`; `auto` picks,
in order: an explicitly configured OpenAI-compatible endpoint, then the Claude CLI, then Anthropic,
then OpenAI.

| backend | requirement | flag |
| --- | --- | --- |
| `claude-cli` *(recommended)* | `claude` on PATH, logged in. **No API key**, subscription auth | `--llm-backend claude-cli --llm-model opus` |
| `anthropic` | `pip install anthropic` + `ANTHROPIC_API_KEY` | `--llm-backend anthropic --llm-model <id>` |
| `openai` | `OPENAI_API_KEY`, or any OpenAI-compatible local server | `--llm-backend openai --llm-base-url http://localhost:11434/v1` |

The `claude-cli` backend is sandboxed by the harness: file/shell/network tools disallowed, plan
permission mode, and a fresh empty working directory that is deleted after each call. This is
load-bearing — see §7.

---

## 3. Verify your setup before trusting any number

Run the two no-LLM baselines first. They take seconds and they are the setup check.

```bash
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT --reference \
  --out-dir /tmp/rtllm_ref --workers 8

python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT --empty-baseline \
  --out-dir /tmp/rtllm_empty --workers 8
```

You must get:

```
reference: 47/50 designs func-pass (94.0% official), 43/43 adjusted (100.0%), syntax 50/50
empty:      4/50 designs func-pass  (8.0% official),  0/43 adjusted   (0.0%), syntax 50/50
```

If `--reference` is not 47/50, stop and investigate before running anything else:

- **Below 47** → most likely your simulator is older than iverilog 12, or the benchmark checkout
  is not v2.0 (`git -C $RTLLM_ROOT log -1` should show the 50-design tree with
  `Arithmetic/ Control/ Memory/ Miscellaneous/`).
- **`missing_golden_data` failures** → the support files (`reference.dat`, `reference.txt`,
  `tri_gen.txt`, `test_data.dat`, `wfull.txt`, `rempty.txt`, `tdata.txt`) are not reaching the
  simulator's working directory. They ship inside each design directory; the harness copies them.
  A local edit to `_prepare_workdir` is the usual cause.
- **`simulator_unsupported` on `ring_counter`/`asyn_fifo`** → the testbench shims are off. They are
  on by default; check you did not pass `--no-shims`.

If `--empty-baseline` is not 4/50, your oracle is not behaving as measured — do not quote scores.

---

## 4. The four run modes

### 4a. Reference (ceiling) — seconds

Scores the benchmark's own `verified_*.v` RTL. Tells you the maximum any model can achieve under
your simulator.

```bash
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT --reference \
  --out-dir runs/reference --workers 8
```

### 4b. Empty baseline (floor) — seconds

Scores a port-only module with **no logic**. Any design it passes has a vacuous oracle, and that
design's score means nothing for any model.

```bash
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT --empty-baseline \
  --out-dir runs/empty --workers 8
```

### 4c. Agent run (the actual benchmark) — ~30–45 min at `--workers 8`

Natural-language spec → `rtl_planner` → `rtl_generator` → verifier → `failure_analyst` →
`rtl_repair_agent`, looping until pass or `--max-repair-rounds` is spent.

```bash
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT \
  --out-dir runs/agent_opus \
  --workers 8 \
  --max-repair-rounds 2 \
  --llm-backend claude-cli --llm-model opus \
  --verbose
```

Useful variants:

```bash
# Smoke test on three designs first (~2-4 min) before committing to a full sweep
--designs adder_8bit pulse_detect fsm --workers 3 --max-repair-rounds 1

# pass@k with independent samples (cost scales linearly: --samples 5 is ~5x the wall clock)
--samples 5

# One-shot generation only, no repair loop — the number comparable to published one-shot results
--max-repair-rounds 0

# Measure what the repair evidence is worth: blind retry, no tool output shown to the model
--evidence-policy none

# Resume an interrupted sweep (refuses to resume if the run configuration disagrees)
--resume

# Skip the planner agent
--no-plan
```

`--workers` is network-bound, not CPU-bound; 8 parallel CLI agents ran clean here with no rate
limiting. Ctrl-C once stops scheduling new designs and still writes the reports; twice aborts.

### 4d. External RTL (comparison against other models) — seconds

Scores pre-generated RTL through the identical pipeline. The benchmark ships GPT-3.5 and GPT-4
generations (5 trials × 29 designs each), which is the only true apples-to-apples comparison
available offline.

```bash
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT \
  --external-rtl $RTLLM_ROOT/_chatgpt4 --label gpt-4 \
  --out-dir runs/gpt4 --workers 8
```

Layout: `DIR/<design>.v` for a single trial, or `DIR/<trial>/<design>.v` where each trial
subdirectory is one sample (so `t1..t5` gives you pass@5). Designs absent from a trial are recorded
as `missing_candidate`, never silently dropped.

---

## 5. Reading the output

Every run writes into `--out-dir`:

| file | what it is |
| --- | --- |
| `results.jsonl` | One `DesignResult` per line, appended as each design finishes. Crash-safe; `--resume` reads it |
| `report.json` | Aggregate metrics: totals, `adjusted`, `adjusted_unpassable_only`, pass@k, failure-family counts, oracle section |
| `report.md` | Human-readable table + Configuration block + caveats |
| `designs/<name>/` | `rtl.v`, `compile.log`, `sim.log`, `trace.json` (per-round history) |

### Which number to quote

**Quote the adjusted rate over the 43-design basis.** `report.json → adjusted.designs_func_success`
over `adjusted.designs`.

The 50-design denominator is not honest, because:

- **3 designs are unpassable** by any RTL (`clkgenerator`, `radix2_div`, `ring_counter`)
- **4 designs pass with no logic at all** (`comparator_3bit`, `comparator_4bit`,
  `sequence_detector`, `square_wave`)

`adjusted` excludes both directions. `adjusted_unpassable_only` excludes only the first and is kept
for continuity with earlier reports — it reads higher and is the weaker number.

Also distinguish, and never conflate:

- `pass@1_round0` — one-shot, no repair. **This is what you compare to published one-shot results.**
- `pass@1_with_repair` — after the closed verifier loop.

---

## 6. Failure taxonomy

`failure_family` on every failing round:

| family | meaning | what to do |
| --- | --- | --- |
| `compile_error` | iverilog rejected the candidate | Real model failure |
| `missing_module` | Top module name does not match what the testbench instantiates | Real model failure |
| `port_mismatch` | Port names/widths disagree with the testbench | Real model failure |
| `functional_mismatch` | Compiled and ran, testbench reported failures | Real model failure |
| `timeout` | Simulation exceeded `--sim-timeout` (default 30 s) | Usually a hang in the candidate; check `sim.log` is empty |
| `no_output` | Compiled and ran but printed nothing | Inspect the testbench |
| `runaway_output` | Candidate flooded stdout past the kill threshold (8 MiB) | Real model failure |
| `illegal_system_task` | Candidate contains `$display`/`$finish`/etc. — refused before compiling | See §7 |
| `simulator_unsupported` | iverilog rejects a testbench construct | Harness/simulator issue, not the model |
| `missing_golden_data` | Testbench `$readmemh` could not open its support file | **Setup bug** — see §3 |

---

## 7. Benchmark integrity — what the harness enforces and why

1. **The model never sees the golden RTL or the testbench source.** It gets the natural-language
   `design_description.txt`, its own previous RTL, and tool output from its own failing run.
   `--evidence-policy none` removes even the tool output.
2. **The LLM backend is sandboxed.** Before this was fixed, `claude -p` ran with full tool access
   and returned a staged `testbench.v` verbatim when one sat in its working directory. It now runs
   with file/shell/network tools disallowed, in plan permission mode, in a scrubbed temp dir.
   **If you add a new tool-capable backend, you must sandbox it the same way.**
3. **Candidate RTL may not print to stdout.** The oracle greps the combined stdout of testbench and
   design, so a candidate that prints "pass" scores. Measured before the gate existed: a zero-logic
   stub plus one `$display` scored the official oracle on **44 of 45** designs. Candidates
   containing `$display`, `$write`, `$monitor`, `$strobe`, `$f*`, `$dump*`, `$finish` or `$stop` are
   now refused before compiling, as `illegal_system_task`, with the offending lines handed to the
   repair agent. `$signed`, `$unsigned`, `$clog2`, `$bits`, `$time` and `$random` remain legal.
   No `verified_*.v` uses a banned task, so the reference baseline is unaffected.
   For external RTL the harness also reports what the gate cost (`--no-gate-impact` disables that
   second measurement).

---

## 8. Measured results

All numbers below are from this harness: iverilog 12.0 `-g2012`, identical oracle, identical shims.

### All 50 designs

| | syntax | func (official) | func (strict) | round 0 | adjusted (43) |
| --- | :-: | :-: | :-: | :-: | :-: |
| **this agent** (Claude Opus, 1 sample, ≤2 repairs) | 50/50 | **48/50** | 48/50 | 33/50 | **42/43 (97.7%)** |
| reference `verified_*.v` | 50/50 | 47/50 | 47/50 | — | 43/43 |
| empty stub (floor) | 50/50 | 4/50 | 4/50 | — | 0/43 |

The agent's two failures: `ring_counter` (unpassable oracle) and `serial2parallel` (**genuine** —
hangs vvp on all rounds). It passes `clkgenerator` and `radix2_div`, both of which the benchmark's
own reference RTL fails.

The repair loop rescued 15 designs (33/50 → 48/50): `LFSR`, `LIFObuffer`, `adder_pipe_64bit`,
`alu`, `asyn_fifo`, `barrel_shifter`, `clkgenerator`, `fixed_point_substractor`, `freq_divbyeven`,
`freq_divbyfrac`, `freq_divbyodd`, `pulse_detect`, `radix2_div`, `sequence_detector`,
`signal_generator`.

### The 29 designs the shipped GPT sets cover

None of the 4 vacuous designs are in this subset, so the floor here is a true 0/29.

| | samples | syntax | pass@1 | pass@5 | note |
| --- | :-: | :-: | :-: | :-: | --- |
| this agent — **round 0, one-shot** | 1 | 29/29 | **0.759** (22/29) | — | the one-shot comparison |
| this agent — after ≤2 repairs | 1 | 29/29 | **0.966** (28/29) | — | closed verifier loop |
| gpt-4 (shipped) | 5 | 27/29 | 0.414 | 0.621 (18/29) | one-shot |
| gpt-3.5 (shipped) | 5 | 25/29 | 0.255 | 0.379 (11/29) | one-shot |
| reference `verified_*.v` | 1 | 29/29 | 0.966 (28/29) | — | ceiling |
| empty stub | 1 | 29/29 | 0.000 (0/29) | — | floor |

Like-for-like, single sample, no repair: **0.759 vs 0.414 (gpt-4) vs 0.255 (gpt-3.5)**. Even against
gpt-4's 5-sample pass@5 of 0.621, the agent's one-shot pass@1 is higher — and its after-repair 28/29
matches the benchmark's own reference RTL on this subset.

The illegal-system-task gate rejected **0 samples** in both GPT sets, so it costs them nothing and
the comparison is not distorted by it.

Reproduce the GPT rows with the §4d commands.

**Fairness caveats — state these whenever you quote the comparison:**

- The GPT sets are **one-shot with no repair loop and no tool feedback**. The honest head-to-head is
  against the agent's **round-0 row (22/29)**; the 28/29 row shows what the closed loop adds.
- The GPT sets are **5 samples**, this agent's run is **1**. pass@5 flatters a 5-sample method
  relative to a single-sample run. Run `--samples 5` if you want a symmetric comparison.
- The GPT generations were produced years earlier against RTLLM v1.1 prompts.

---

## 9. Open items and known limits

1. **`rtllm_v2_results/` in the repo is stale.** It holds the superseded first-run numbers (43/50)
   and, in the older `report.md` text, a claim that the benchmark's golden data files are missing.
   **That claim is false** — the files ship inside each design directory; the original run failed to
   copy them into the simulation working directory. Regenerate the directory from a current run.
2. **`serial2parallel` is a real agent failure.** All rounds hang vvp with empty output. Worth a
   look: the repair agent gets no evidence from an empty log, so it cannot converge.
3. **Repair evidence is thin.** Many RTLLM testbenches print only `===========Error===========` with
   no per-vector detail, so `functional_mismatch` repair is under-informed. Richer evidence
   (PMLC-style mismatch localisation, per the repo's existing blueprint) is the obvious next step.
4. **`--samples > 1` has not been run for the agent.** All agent pass@k figures are from n=1.
5. **iverilog is not VCS.** The paper's numbers use Synopsys VCS. Two designs
   (`ring_counter`, `asyn_fifo`) needed documented, semantics-preserving testbench shims to run at
   all, and `ring_counter`'s pass banner is unreachable under iverilog's scheduling. Results on VCS
   would likely differ.
6. **The official oracle is a bare `Pass`/`pass` substring test**, so a testbench that prints both a
   failure line and a pass banner scores. `func_pass_strict` is the guard; the two agreed on every
   run measured so far but can diverge.

### Testbench shims

Two, both applied to a copy of the testbench, both validated against reference RTL *and* against
deliberately wrong RTL so a shim cannot manufacture a pass. Disable with `--no-shims`.

| design | shim | why |
| --- | --- | --- |
| `ring_counter` | SystemVerilog array initializer → declaration + `initial` block | iverilog: "Assignment to an entire array … is not yet supported" |
| `asyn_fifo` | `break` inside `repeat` → named block + `disable` | iverilog: "break statements not supported" |

---

## 10. Exit codes

| code | meaning |
| --- | --- |
| `0` | Run completed |
| `2` | No LLM backend resolved — the reason is printed. Never a silent 0% run |
| `3` | Run completed but one or more designs failed from a **backend outage**, not a model error. Those designs are excluded from the adjusted basis and listed as `llm_error_designs` |
| `130` | Interrupted with Ctrl-C; a partial `report.json` was still written |
