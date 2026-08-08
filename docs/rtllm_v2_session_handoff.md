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
| `e48a7ad` | `--external-rtl` comparison mode + this handoff |
| `+1` | `scripts/triage_rtllm_run.py` and §6 below |

Test suite: `python -m pytest tests -q` → **724 passed, 86 subtests passed**.

`rtllm_v2_results/` is now **generated, not hand-written**: `scripts/make_rtllm_v2_results.py`
rebuilds it from a completed run directory, so a stale figure cannot survive an update. It
currently holds the confirmed full-suite run in `runs/confirm` (§9). See also
[`loop_ablation.md`](loop_ablation.md), which asks what each part of the loop is worth.

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
python3 -m pytest tests -q   # expect 724 passed
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
load-bearing — see §8.

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

## 4. The whole sequence, end to end

If you only read one section, read this one. Copy-paste, in order:

```bash
export RTLLM_ROOT=~/RTLLM

# 1. Calibrate: ceiling and floor. Seconds. MUST give 47/50 and 4/50 (§3).
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT --reference \
  --out-dir runs/reference --workers 8
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT --empty-baseline \
  --out-dir runs/empty --workers 8

# 2. Smoke test three designs before spending 40 minutes. ~2-4 min.
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT \
  --out-dir runs/smoke --designs adder_8bit pulse_detect fsm \
  --workers 3 --max-repair-rounds 1 --llm-backend claude-cli --llm-model opus --verbose

# 3. The real sweep. ~30-45 min at --workers 8.
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT \
  --out-dir runs/agent --workers 8 --max-repair-rounds 2 \
  --llm-backend claude-cli --llm-model opus --verbose

# 4. Triage it. This is what turns a raw score into a defensible one (§6).
python3 scripts/triage_rtllm_run.py \
  --run runs/agent --reference runs/reference --empty runs/empty --markdown

# 5. Optional: compare against the models the benchmark ships.
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT \
  --external-rtl $RTLLM_ROOT/_chatgpt4 --label gpt-4 --out-dir runs/gpt4 --workers 8
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT \
  --external-rtl $RTLLM_ROOT/_chatgpt35 --label gpt-3.5 --out-dir runs/gpt35 --workers 8
```

Steps 1 and 4 are not optional if you intend to quote a number. Step 1 tells you whether your
environment is sane; step 4 tells you which designs your score is actually made of.

---

## 4b. The run modes in detail

### Reference (ceiling) — seconds

Scores the benchmark's own `verified_*.v` RTL. Tells you the maximum any model can achieve under
your simulator.

```bash
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT --reference \
  --out-dir runs/reference --workers 8
```

### Empty baseline (floor) — seconds

Scores a port-only module with **no logic**. Any design it passes has a vacuous oracle, and that
design's score means nothing for any model.

```bash
python3 scripts/run_rtllm_v2.py --benchmark $RTLLM_ROOT --empty-baseline \
  --out-dir runs/empty --workers 8
```

### Agent run (the actual benchmark) — ~30–45 min at `--workers 8`

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

### External RTL (comparison against other models) — seconds

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

## 6. Calibrate and triage — the step that makes your outputs trustworthy

A raw RTLLM score mixes four different things together. Only two of them say anything about your
RTL generator. Run the triage after every sweep:

```bash
python3 scripts/triage_rtllm_run.py \
  --run runs/agent_opus --reference runs/reference --empty runs/empty
```

Add `--markdown` for tables you can paste into a report, or `--json` for the raw buckets.

It sorts every design into one of five buckets, using three facts: does the empty stub pass it, does
the reference RTL pass it, do you pass it.

| bucket | what it means | what to do |
| --- | --- | --- |
| **free** — empty stub passes | The testbench is vacuous; a module with no logic scores | Exclude. A pass here is not evidence |
| **unscorable** — neither reference nor you pass | No RTL is known to satisfy this testbench under iverilog | Exclude. Failing it is not your defect |
| **reference wrong** — you pass, reference fails | The shipped `verified_*.v` is wrong but the testbench *is* satisfiable | **Count it. This is a win over the benchmark** |
| **passed** | Reference passes, empty fails, you pass | The score |
| **REAL FAILURE** | Reference passes, empty fails, you fail | The only designs worth debugging |

The first two buckets are noise; the last three are the **signal basis**, and that is the
denominator to quote.

Measured on the run in §9:

```
free (empty stub passes -- vacuous oracle)                   4  comparator_3bit, comparator_4bit, sequence_detector, square_wave
unscorable (neither reference nor you pass)                  1  ring_counter
reference wrong (you pass a testbench the reference fails)   2  clkgenerator, radix2_div
real signal -- passed                                       40
REAL FAILURE                                                 3  asyn_fifo, pulse_detect, serial2parallel

signal basis: 45/50 designs (5 excluded as uninformative)
functional pass, signal basis: 42/45 (93.3%)
first-round pass (no repair):   31/45 (68.9%)
lifted by the repair loop:      11
```

### Why this differs from `report.json → adjusted`

`adjusted` (40/43) excludes all three `KNOWN_ORACLE_ISSUES`, which drops `clkgenerator` and
`radix2_div` — two designs you *passed* and the reference failed. The triage keeps them, because a
satisfiable testbench is a real test even when the shipped reference RTL is wrong. Both are
defensible; **42/45 is the one that does not throw away wins over the benchmark**, and 40/43 is the
more conservative. Quote whichever you like, but say which, and never quote the bare 50-design
number.

### The four kinds of issue, kept separate

1. **Benchmark defects** (the *free*, *unscorable* and *reference wrong* buckets) — properties of
   RTLLM as shipped. They are stable across runs: calibrate once with step 1 of §4, subtract forever.
2. **Simulator differences** — iverilog vs the paper's VCS, plus the two testbench shims (§10).
   These matter when comparing your numbers to *published* ones, not when comparing two models on
   your own machine.
3. **Harness bugs** — these make measurements wrong rather than models wrong. Three were found and
   fixed here (uncopied support files producing false `missing_golden_data`, the `$display` oracle
   bypass, the backend reading the testbench off disk). §3 exists to catch a recurrence.
4. **Model failures** — the *REAL FAILURE* bucket. The only category that says anything about the
   RTL generator, and the only one worth your debugging time.

---

## 7. Failure taxonomy

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

## 8. Benchmark integrity — what the harness enforces and why

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

## 9. Measured results

All numbers below are from this harness: iverilog 12.0 `-g2012`, identical oracle, identical shims.

### All 50 designs

The confirmed run is `runs/confirm`, published to `rtllm_v2_results/`. Its configuration is the
baseline confirmed by the ablation study (`plan=on`, `evidence=logs`, `max_repair_rounds=2`,
`samples=1`) — see [`loop_ablation.md`](loop_ablation.md) for why no other configuration was
adopted.

| | syntax | func (official) | func (strict) | round 0 | adjusted (43) |
| --- | :-: | :-: | :-: | :-: | :-: |
| **this agent** (Claude Opus, 1 sample, ≤2 repairs) | 50/50 | **46/50** | 46/50 | 34/50 | **40/43 (93.0%)** |
| reference `verified_*.v` | 50/50 | 47/50 | 47/50 | — | 43/43 |
| empty stub (floor) | 50/50 | 4/50 | 4/50 | — | 0/43 |

The agent's four failures: `ring_counter` (unpassable oracle — the banner is unreachable under
iverilog's scheduling, so nothing can pass it), `serial2parallel` (**genuine** — hangs vvp on all
rounds), and `asyn_fifo` and `pulse_detect` (**genuine** functional mismatches). It passes
`clkgenerator` and `radix2_div`, both of which the benchmark's own reference RTL fails.

The repair loop rescued 12 designs (34/50 → 46/50): `LFSR`, `adder_pipe_64bit`, `alu`,
`barrel_shifter`, `clkgenerator`, `fixed_point_substractor`, `freq_divbyeven`, `freq_divbyfrac`,
`freq_divbyodd`, `radix2_div`, `sequence_detector`, `signal_generator`.

**On run-to-run variance.** The same configuration scored 45/50 in `runs/agent` and 46/50 here.
The generator is sampled at `--samples 1`, so a one- or two-design swing between runs of an
identical configuration is normal and is the reason the ablation study treats a one-design
difference as noise. Do not read the difference between these two runs as a change in anything.

### The 29 designs the shipped GPT sets cover

None of the 4 vacuous designs are in this subset, so the floor here is a true 0/29.

| | samples | syntax | pass@1 | pass@5 | note |
| --- | :-: | :-: | :-: | :-: | --- |
| this agent — **round 0, one-shot** | 1 | 29/29 | **0.759** (22/29) | — | the one-shot comparison |
| this agent — after ≤2 repairs | 1 | 29/29 | 0.897 (26/29) | — | closed verifier loop |
| gpt-4 (shipped) | 5 | 27/29 | 0.414 | 0.621 (18/29) | one-shot |
| gpt-3.5 (shipped) | 5 | 25/29 | 0.255 | 0.379 (11/29) | one-shot |
| reference `verified_*.v` | 1 | 29/29 | 0.966 (28/29) | — | ceiling |
| empty stub | 1 | 29/29 | 0.000 (0/29) | — | floor |

Like-for-like, single sample, no repair: **0.759 vs 0.414 (gpt-4) vs 0.255 (gpt-3.5)**. Even against
gpt-4's 5-sample pass@5 of 0.621, the agent's one-shot pass@1 is higher. After repair it reaches
26/29, two short of the benchmark's own reference RTL on this subset.

This table is generated into [`../rtllm_v2_results/comparison.md`](../rtllm_v2_results/comparison.md)
by `scripts/make_rtllm_v2_results.py`, with the caveats stated before the table rather than after
it. Regenerate rather than hand-editing either copy.

The illegal-system-task gate rejected **0 samples** in both GPT sets, so it costs them nothing and
the comparison is not distorted by it.

Reproduce the GPT rows with the §4b External RTL commands.

**Fairness caveats — state these whenever you quote the comparison:**

- The GPT sets are **one-shot with no repair loop and no tool feedback**. The honest head-to-head is
  against the agent's **round-0 row (22/29)**; the 26/29 row shows what the closed loop adds.
- The GPT sets are **5 samples**, this agent's run is **1**. pass@5 flatters a 5-sample method
  relative to a single-sample run. Run `--samples 5` if you want a symmetric comparison.
- The GPT generations were produced years earlier against RTLLM v1.1 prompts.

### Which configuration this is, and why no other

The configuration above is the one the ablation study confirmed. Eight arms were run over the
13 designs that failed at round 0 in `runs/agent`, varying one factor each
([`loop_ablation.md`](loop_ablation.md)):

| arm | func | verdict |
| --- | :-: | --- |
| `baseline` (`plan=on, evidence=logs, rounds=2`) | 10/13 | reference |
| `evidence=self` | 11/13 | not significant (1 discordant) |
| `evidence=oracle` | 10/13 | **identical outcomes on all 13 designs** |
| `rounds=3` | 10/13 | not significant (2 discordant) |
| `no-plan` | 9/13 | not significant (1 discordant) |
| `rounds=1` | 8/13 | not significant (2 discordant) |
| `evidence=none` | 5/13 | not significant (7 discordant, Holm p=0.750) |
| `rounds=0` | 1/13 | **significant, below baseline** (9 discordant, Holm p=0.027) |

**No arm scored significantly above the baseline**, so the baseline was confirmed rather than
replaced. `evidence=self` is nominally highest at 11/13, but that is a one-design difference on a
13-design subset — 7.7 pp, well inside noise, against a corrected significance floor of 9
discordant designs. Promoting it would be exactly the one-design over-read the study exists to
avoid.

Two results are worth carrying forward anyway:

- **The repair loop is the one demonstrated ingredient.** `rounds=0` is significantly below
  baseline. Whatever the loop scores, the generator alone does not.
- **Richer failure evidence has an upper bound of zero.** `evidence=oracle` may see where the
  candidate diverges from the *reference RTL* — an advantage no deployable system has — and it
  produced the identical result on every design. Do not invest in richer repair evidence on the
  strength of this benchmark.

---

## 10. Open items and known limits

1. ~~**`rtllm_v2_results/` in the repo is stale.**~~ **Closed.** The directory is regenerated by
   `scripts/make_rtllm_v2_results.py --run runs/confirm` and now carries the confirmed run plus a
   `comparison.md` against the shipped GPT archives. The old text also claimed the benchmark's
   golden data files were missing; **that claim was false** — the files ship inside each design
   directory, and the original run simply failed to copy them into the simulation working
   directory. Both the stale numbers and the false claim are gone. Never edit that directory by
   hand; the publisher refuses to publish an interrupted or partial run.
2. **`serial2parallel` is a real agent failure.** All rounds hang vvp with empty output. Worth a
   look: the repair agent gets no evidence from an empty log, so it cannot converge.
3. **Repair evidence is thin — and the ablation says enriching it is not the fix.** Many RTLLM
   testbenches print only `===========Error===========` with no per-vector detail, so
   `functional_mismatch` repair is under-informed. That was the obvious next step until it was
   measured: the `evidence=oracle` arm, which is shown *where the candidate's output first diverges
   from the reference RTL* — strictly more than any PMLC-style localisation could recover without
   the answer key — produced **outcomes identical to the plain log tail on all 13 designs**
   ([`loop_ablation.md`](loop_ablation.md) §3). On this benchmark the ceiling for richer failure
   evidence is zero. What the same study *does* show paying is the existence of the repair loop at
   all. Treat richer evidence as a hypothesis that has been tested and failed here, not as pending
   work — and note the scope: 13 designs, one sample each, so this bounds the effect on RTLLM v2
   rather than on repair evidence in general.
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

## 11. Exit codes

| code | meaning |
| --- | --- |
| `0` | Run completed |
| `2` | No LLM backend resolved — the reason is printed. Never a silent 0% run |
| `3` | Run completed but one or more designs failed from a **backend outage**, not a model error. Those designs are excluded from the adjusted basis and listed as `llm_error_designs` |
| `130` | Interrupted with Ctrl-C; a partial `report.json` was still written |
