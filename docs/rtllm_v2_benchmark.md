# RTLLM-V2 Benchmark Harness

RTLLM v2.0 ([hkust-zhiyao/RTLLM](https://github.com/hkust-zhiyao/RTLLM)) is an
open-source benchmark for natural-language-to-RTL generation: 50 design tasks, each a
directory containing a `design_description.txt` (the prompt), a `testbench.v` (the
oracle), a `makefile`, and a `verified_*.v` reference implementation. The designs are
categorized as `Arithmetic` (19), `Control` (6), `Memory` (5), and `Miscellaneous` (20),
covering adders, multipliers, dividers, FIFO/LIFO/shifters, counters and FSMs, frequency
dividers, RISC-V blocks, and assorted glue logic.

This repository already treats C-to-HLS-C-to-RTL as a verifier-closed loop rather than a
single generation step (see `docs/functional_equivalent_rtl_agent.md`). RTLLM-V2 is the
same loop pointed at a different front door: the input is a natural-language spec instead
of C, and the verifier is Icarus Verilog instead of the Vitis ladder, because the
benchmark ships Verilog testbenches rather than C oracles. Everything else — plan a
contract, generate, verify, classify the failure, patch, rerun the *whole* verifier — is
unchanged.

Two modules and one driver implement it:

- `c2hlsc_agent/rtllm_bench.py` — benchmark model and the iverilog/vvp verifier. No LLM
  code lives here, so the oracle can be run and trusted on its own.
- `c2hlsc_agent/rtllm_agent.py` — the multi-agent loop (planner, generator, repair) on
  top of the repo's existing `c2hlsc_agent/llm.py` backends.
- `scripts/run_rtllm_v2.py` — the sweep driver, reports, and artifacts.

## Agent Roles

The RTLLM mapping of the blueprint in `docs/functional_equivalent_rtl_agent.md`:

1. `rtl_planner` (`plan_contract`, `RTL_PLANNER_SYSTEM_PROMPT`)
   - Inputs: `design_description.txt`, the required top module name.
   - Outputs: an interface contract — module name, port list with directions and widths,
     clock edge, reset polarity and synchronicity, handshake/latency expectations, and
     the behavioural obligations the description states.
   - Failure ownership: ambiguous or under-specified prompts, mis-read port tables.
   - Disabled with `--no-plan` (`RtllmAgentConfig.plan = False`) so the planner's
     contribution can be ablated instead of assumed.

2. `rtl_generator` (`generate_rtl`, `RTL_GENERATOR_SYSTEM_PROMPT`)
   - Inputs: the description, the contract when planning ran.
   - Outputs: one self-contained translation unit whose *top* module is named exactly
     after the design directory (`adder_8bit`, `fsm`, …). The testbench instantiates that
     name, so a correct design with the wrong module name is still a failure.
   - `extract_verilog(text, module_name)` pulls the code out of whatever the model
     wrapped it in (fenced block, prose, multiple candidate blocks) and prefers the unit
     that actually declares `module_name`.
   - Failure ownership: syntax, wrong module/port names, non-synthesizable or
     simulator-hostile constructs.

3. verifier (`evaluate_rtl` in `rtllm_bench.py`)
   - Writes the candidate to `<workdir>/<design>.v`, copies the testbench (shimmed if
     needed) beside it, compiles with `iverilog -g2012`, then simulates with `vvp`, both
     with `cwd=workdir` and their own timeout.
   - Never raises on tool failure and never writes into the benchmark checkout. A
     timeout keeps the partial stdout captured so far, so a design that printed 400 good
     compares and then hung is still diagnosable.
   - This is the `cosim_operator` slot. iverilog stands in for the four-stage Vitis
     ladder; there is only one gate here because the benchmark's contract is
     "testbench says pass", not "RTL is equivalent to a C oracle".

4. `failure_analyst` (`classify_failure`, `build_evidence`)
   - Deterministic, not LLM-driven: the compile log, sim log, syntax flag, and timeout
     flag map to one failure family, and the evidence pack is a truncated log excerpt.
   - Failure families (exact strings in `SimResult.failure_family`; `None` when the
     attempt passed):

     | Family | Meaning |
     | --- | --- |
     | `compile_error` | iverilog rejected the candidate — syntax, undeclared identifier, illegal construct. |
     | `missing_module` | The testbench instantiates a module the candidate never declares; usually a mis-named top. |
     | `port_mismatch` | The module exists but its port list disagrees with the testbench instantiation. |
     | `functional_mismatch` | Compiled and ran, but the testbench reported failures or never printed a pass marker. |
     | `timeout` | `iverilog` exceeded `--compile-timeout` or `vvp` exceeded `--sim-timeout`. |
     | `no_output` | The simulation produced no stdout at all. |
     | `simulator_unsupported` | iverilog emitted a `sorry:` unimplemented-construct diagnostic. |
     | `missing_golden_data` | The testbench tried to `$readmemh` a data file the benchmark does not ship. |

5. `rtl_repair_agent` (`repair_rtl`, `RTL_REPAIR_SYSTEM_PROMPT`)
   - Inputs: the current candidate, the failure family, and the compact evidence pack.
   - Repairs only the current candidate with the latest evidence — no accumulated
     cross-round transcript, matching the repo's existing repair discipline.
   - The full verifier reruns from compile after every patch. There is no "only rerun
     the failing phase" shortcut.

There is no `shift_left_testbench_agent` in this loop: the benchmark owns the testbench,
and generating our own would change what is being measured. There is no
`rtl_optimizer_agent` either — RTLLM scores function, not QoR.

## Main Loop

```text
for each design:
  for sample in 1..samples:
    plan contract                       # rtl_planner, unless --no-plan
    generate RTL                        # rtl_generator
    compile + simulate                  # verifier (iverilog -g2012, vvp)
    while failing and rounds < max_repair_rounds:
      classify failure + pack evidence  # failure_analyst
      patch RTL                         # rtl_repair_agent
      compile + simulate from scratch   # verifier, full rerun
    record sample outcome
  aggregate design -> syntax_success, func_success
write results.jsonl, report.json, report.md, designs/<name>/ artifacts
```

## Setup

Icarus Verilog is the only external tool. On Debian/Ubuntu:

```bash
sudo apt-get update && sudo apt-get install -y iverilog
iverilog -V | head -1        # 12.0 or newer is what this harness was calibrated on
```

Clone the benchmark once and point the driver at it:

```bash
git clone https://github.com/hkust-zhiyao/RTLLM.git ~/benchmarks/RTLLM
export RTLLM_ROOT=~/benchmarks/RTLLM
```

`--benchmark PATH` defaults to `$RTLLM_ROOT`. Alternatively `--clone` fetches
`DEFAULT_BENCHMARK_URL` (the upstream repo) into the `--benchmark` path when it is not
already there; pass `--clone <URL>` to use a fork or mirror.

`discover_designs` walks the two-level category tree (`Arithmetic/Adder/adder_8bit`, …)
and skips the `_chatgpt35/` and `_chatgpt4/` sample-output directories that ship with the
benchmark.

No API key is needed. The default LLM backend is `claude-cli`, the local Claude Code CLI
(`claude -p`) under subscription auth, exactly as in the rest of this repo — see the
backends section of the README for `openai` (including local Ollama/vLLM endpoints) and
`anthropic`.

## Running It

### 1. Reference baseline first, always

Before scoring a model, score the benchmark. `--reference` skips the LLM entirely and
feeds the benchmark's own `verified_*.v` through the identical verifier
(`evaluate_reference` / `reference_rtl_text`). That number is the oracle's ceiling on
your machine, and it is not 50:

```bash
python3 scripts/run_rtllm_v2.py \
  --benchmark "$RTLLM_ROOT" \
  --out-dir runs/rtllm_reference \
  --reference \
  --workers 4
```

Takes about a minute. Read `runs/rtllm_reference/report.md` and treat its `func_success`
count as the denominator you actually care about.

### 2. Agent run

```bash
python3 scripts/run_rtllm_v2.py \
  --benchmark "$RTLLM_ROOT" \
  --out-dir runs/rtllm_opus \
  --samples 5 \
  --max-repair-rounds 2 \
  --workers 4 \
  --llm-backend claude-cli --llm-model opus
```

- `--samples N` is the `n` in pass@k. RTLLM's own script uses 5 independent generations
  per design; use `--samples 1` for a smoke run.
- `--max-repair-rounds N` bounds the repair loop per sample (`0` measures raw
  single-shot generation, which is what the RTLLM paper reports).
- `--workers N` runs N designs concurrently. Each attempt gets its own scratch workdir,
  so parallel runs cannot collide. iverilog is cheap; N = cores is fine, but the LLM
  backend is usually the bottleneck.
- Each finished design is appended to `results.jsonl` immediately, so an interrupted
  sweep keeps everything completed so far — rerun the same command with `--resume` to
  skip the already-recorded designs instead of starting over.

Scope a debugging run to a couple of designs and turn off the planner and repair to see
the raw generator output:

```bash
python3 scripts/run_rtllm_v2.py --benchmark "$RTLLM_ROOT" \
  --out-dir runs/rtllm_debug \
  --designs adder_8bit fsm --no-plan --max-repair-rounds 0 --verbose
```

Other scoping and tuning flags: `--exclude NAME...`, `--limit N`, `--sim-timeout`,
`--compile-timeout`, `--no-shims`, `--evidence-policy {logs,none}`, `--llm-cli-cmd`.

### 3. Reading the reports

Everything lands under `--out-dir`:

| Path | Contents |
| --- | --- |
| `results.jsonl` | One `DesignResult` per line, appended as each design finishes. The resume ledger and the raw record for any re-analysis. |
| `report.json` | Aggregate metrics: syntax/func success counts, the strict counts, pass@k, and the run's configuration (samples, repair rounds, evidence policy, shims, timeouts). |
| `report.md` | The same thing as a human table — per-design rows plus the totals line. |
| `designs/<name>/` | Per-design artifacts: `rtl.v` (the final candidate), `compile.log`, `sim.log`, and `trace.json` (the serialized `DesignResult`: every sample, every round, its role, its `SimResult`, and the RTL at that round). |

`trace.json` is the audit record. It is what lets you answer "did that pass come from the
spec or from the log excerpt we handed back?" months later.

A quick failure-family histogram over a finished sweep:

```bash
python3 - <<'PY'
import collections, json
from pathlib import Path

fam = collections.Counter()
for line in Path("runs/rtllm_opus/results.jsonl").read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        continue  # torn final line from an interrupted sweep
    for sample in row.get("samples", []):
        rounds = sample.get("rounds") or []
        if not rounds:
            fam["no_attempt"] += 1
            continue
        fam[rounds[-1]["sim"].get("failure_family") or "pass"] += 1
print(dict(fam))
PY
```

## Metrics

**Official RTLLM rule** (taken from the benchmark's own `auto_run.py`, so numbers are
comparable to published ones):

- `syntax_success` — the design compiled. Upstream this means `vcs` produced `simv`;
  here it means `iverilog` produced the simulation binary.
- `func_success` — the simulation's stdout contains the substring `Pass` or `pass`.

That is a substring test on testbench-authored output, nothing more. It is what
`classify_output` returns first, and it is what `SimResult.func_pass` records.

**Strict rule** (`SimResult.func_pass_strict`, reported alongside, never instead of):
a pass marker is present **and** no failure marker is present **and** the run did not
time out. Several RTLLM testbenches print per-vector `Failed` lines and then a summary
line that still contains the word "pass", and a design that hangs after printing an early
"pass" is not a passing design. The strict count is usually equal to or one or two below
the official count; when it is not, look at those designs before believing the number.

**pass@k** — the unbiased estimator RTLLM's `auto_run.py` uses, over `n = --samples`
generations with `c` successes per design:

```text
pass@k = mean over designs of  1 - C(n - c, k) / C(n, k)
```

reported for both syntax and function in `report.json`. With `--samples 1` this
degenerates to the plain success rate.

Note that a repair loop and pass@k measure different things. `--max-repair-rounds 0
--samples 5` is the paper-comparable configuration; anything above zero measures the
agent, not the base model, and the report records both settings so the two are never
confused.

## Benchmark Integrity

What the model is shown:

- `design_description.txt` verbatim — the natural-language spec, which already states the
  required module name and port table.
- The required top module name.
- On repair rounds only: the failure family and, under the default evidence policy, a
  truncated excerpt of the compile and simulation logs (`EVIDENCE_LIMIT = 4000`
  characters).

What the model is **never** shown:

- The benchmark's `verified_*.v` reference RTL. `reference_rtl_text` exists solely for
  `--reference` runs and is not reachable from any prompt-building path.
- The `testbench.v` source. Not in the generator prompt, not in the repair prompt, not
  as "helpful context".
- Any golden data file.

Simulation stdout *is* testbench-authored and can quote expected values. That is
legitimate black-box feedback — it is what a human designer sees at a terminal — but it
is a strictly stronger signal than the spec alone, so it is treated as part of the metric
rather than as a hidden knob. `--evidence-policy` controls it:

- `logs` (default) — compile/sim excerpts up to `EVIDENCE_LIMIT`.
- `none` — the failure family and the syntax/function booleans only, no log text.

Run both and the difference tells you how much of the repair gain is log leakage. The
policy in force is written into `report.json` and into every `trace.json`, so no number
from this harness is ambiguous about what the model got to read.

## Known Oracle Limitations

Nine of the 50 designs do not behave under this setup, for four distinct reasons. All of
them are recorded in `KNOWN_ORACLE_ISSUES` (design name → human-readable reason) so the
reports can flag them instead of letting you chase a phantom bug in the generator.

| Design(s) | What breaks | Consequence |
| --- | --- | --- |
| `alu`, `calendar`, `signal_generator` | The testbench does `$readmemh` on `reference.dat` / `reference.txt` / `tri_gen.txt`; **those files do not exist anywhere in the benchmark repo**. | The testbench prints `===========Error===========` for *any* design, the benchmark's own reference included. Unpassable as shipped. Classified `missing_golden_data`. |
| `clkgenerator`, `radix2_div` | The benchmark's own `verified_*.v` fails its own testbench under iverilog (`clkgenerator`: 20 reported failures; `radix2_div`: 3). | Upstream benchmark bugs. No candidate should be expected to pass; a "failure" here says nothing about the generator. |
| `ring_counter` | `testbench.v:20` uses a SystemVerilog array initializer — `reg [7:0] data [0:9] = {8'b…, …};` — which iverilog rejects: *"sorry: Assignment to an entire array or to an array slice is not yet supported."* | Recoverable. The harness applies a documented, semantics-preserving shim to the **testbench copy** (the array literal becomes element-wise assignments in an `initial` block); the design under test is never touched. Un-shimmed this is `simulator_unsupported`. |
| `asyn_fifo` | `testbench.v:102` uses a SystemVerilog `break` inside `repeat(...)`, which iverilog rejects: *"sorry: break statements not supported."* | Recoverable, same treatment: the shim rewrites the early exit into an equivalent guarded loop. Un-shimmed this is `simulator_unsupported`. |
| `adder_pipe_64bit`, `multi_pipe_4bit` | The reference file's top module is named `verified_adder_64bit` / `verified_multi_pipe` rather than `verified_<design>`, so a fixed-prefix rename misses it. | **Baseline-only artifact.** `reference_rtl_text` renames the *first/top* module in the reference file to the design name, which handles both. LLM-generated RTL declares the right module name to begin with, so agent runs are unaffected. |

Shims are opt-out with `--no-shims`, and every `SimResult` carries `shim_applied` so a
shimmed result is never silently conflated with a clean one. A shim may only rewrite the
copied testbench into semantically equivalent Verilog-2001 that iverilog accepts. It may
never touch the design under test, weaken a check, or change stimulus.

### Measured baseline

Running the benchmark's own reference RTL against the benchmark's own testbenches under
iverilog, **as shipped** (no shims, fixed-prefix rename):

```text
syntax (compiled):        46 / 50
functional (official):    41 / 50
```

So 41 — not 50 — is the honest denominator for an agent run on this simulator. Of the
nine gaps, four are recoverable by the harness itself (the two testbench shims lift
`ring_counter` and `asyn_fifo` for *every* run; the top-module rename lifts
`adder_pipe_64bit` and `multi_pipe_4bit` for the reference baseline only), leaving five
designs — `alu`, `calendar`, `signal_generator`, `clkgenerator`, `radix2_div` — that no
implementation can pass here. That puts the practical ceiling at 45/50 with shims
working, and it is why the first thing you should run on a new machine is `--reference`:
trust the number your own `report.md` prints, not the one in this file.

## Differences from the Paper's VCS Setup

RTLLM's `auto_run.py` and per-design `makefile` assume Synopsys VCS. This harness assumes
Icarus Verilog, which is free and installable in one apt line. The differences that
matter when comparing numbers:

- **Simulator and flags.** Upstream: `vcs -sverilog +v2k -timescale=1ns/1ns -debug_all`,
  then `./simv`. Here: `iverilog -g2012 -o sim <design>.v testbench.v`, then `vvp sim`.
  iverilog's SystemVerilog coverage is partial, which is the entire reason the
  `simulator_unsupported` family and the two testbench shims exist. A design that VCS
  accepts may be rejected here; that is a simulator difference, not a design defect, and
  the failure family says so.
- **Timeouts.** `auto_run.py` gives `make sim` a hard 8-second wall clock via a daemon
  thread and no compile timeout at all, so a slow-but-correct design is scored as a
  failure. Here compile and simulation have separate configurable budgets
  (`--compile-timeout`, default 120s; `--sim-timeout`, default 30s), a timeout is
  classified `timeout` rather than silently folded into "not a pass", and partial stdout
  is retained. A run that times out never counts as a strict pass.
- **Isolation.** `auto_run.py` rewrites each design's `makefile` in place, `chdir`s into
  the benchmark tree, and shells out through `os.system`. This harness copies the
  design and testbench into a per-attempt scratch workdir and never writes into the
  benchmark checkout, so a sweep is re-runnable and safe to parallelize.
- **pass@k arithmetic.** `auto_run.py` appends an extra `0` to the per-design list before
  averaging, which scales its printed pass@k by `n_designs / (n_designs + 1)` — about
  0.98 at 50 designs. Keep that factor in mind when comparing this harness's `pass_at_k`
  against a number produced by the upstream script.
- **Oracle strictness.** Upstream keeps only the `"Pass" in output or "pass" in output`
  test. That test is reproduced exactly as `func_pass`; the additional `func_pass_strict`
  column has no upstream counterpart and should not be quoted as an RTLLM score.
