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
   - Writes the candidate to `<workdir>/<design>.v`, checks it is admissible (see
     [Candidate admissibility](#candidate-admissibility)), copies the testbench (shimmed if
     needed) beside it, compiles with `iverilog -g2012`, then simulates with `vvp`, both
     with `cwd=workdir` and their own timeout and a bounded output capture.
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
     | `missing_golden_data` | The testbench `$readmemh`'d a data file that was not present in the sandbox. The benchmark *does* ship these files inside the design directory and `evaluate_rtl` copies them, so this family firing means the harness is broken, not the candidate — no RTL change can fix it, and the repair loop stops early on it. |
     | `illegal_system_task` | The design file contained a simulation output or control system task (`$display`, `$write`, `$monitor`, `$strobe`, `$fdisplay`, `$finish`, `$stop`, `$dump*`). Refused **before compiling** — see [Candidate admissibility](#candidate-admissibility). |
     | `runaway_output` | The simulation was killed for emitting more than `RUNAWAY_OUTPUT_BYTES` (8 MiB) on one stream. Never a strict pass. |

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

### 1b. Empty baseline — the other end of the same ruler

`--reference` measures the oracle's ceiling. `--empty-baseline` measures its **floor**: it
feeds every design a module with the correct port list and *no logic at all*
(`rtllm_bench.empty_stub_rtl`, derived from the golden file's module header, harness-side
only). Any design it passes is one where the score carries no information, because an agent
that emits an empty module scores it too.

```bash
python3 scripts/run_rtllm_v2.py \
  --benchmark "$RTLLM_ROOT" \
  --out-dir runs/rtllm_empty \
  --empty-baseline \
  --workers 4
```

On this checkout that is **4/50** — `comparator_3bit`, `comparator_4bit`,
`sequence_detector`, `square_wave` — the contents of `VACUOUS_ORACLE_DESIGNS`. Run it once
per machine and compare against that constant; see
[Known Oracle Limitations](#known-oracle-limitations).

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
  skip the already-recorded designs instead of starting over. Every row carries a
  `run_config` fingerprint (benchmark path, samples, repair rounds, evidence policy, shims,
  timeouts), and `--resume` **refuses** to merge rows that disagree on any of them: a sweep
  half-scored at `--samples 1 --evidence-policy none` and half at `--samples 5
  --evidence-policy logs` would produce one report averaging two different measurements and
  stamped with only the last invocation's settings.
- Ctrl-C stops scheduling new designs *and* winds down in-flight ones (the stop event
  reaches `run_design`, which checks it between samples and between repair rounds), then
  writes a partial report and exits 130. A second Ctrl-C aborts without joining the running
  workers.
- A design that names an unknown `--designs` value is a hard error, not a silent narrowing:
  one typo used to run 1 of 50 designs and report `1/1 (100.0%)`.

Scope a debugging run to a couple of designs and turn off the planner and repair to see
the raw generator output:

```bash
python3 scripts/run_rtllm_v2.py --benchmark "$RTLLM_ROOT" \
  --out-dir runs/rtllm_debug \
  --designs adder_8bit fsm --no-plan --max-repair-rounds 0 --verbose
```

Other scoping and tuning flags: `--exclude NAME...`, `--limit N`, `--sim-timeout`,
`--compile-timeout`, `--no-shims`, `--evidence-policy {logs,none}`, `--llm-cli-cmd`,
`--empty-baseline`.

Exit codes: `0` the sweep completed (whatever the score), `2` no LLM backend at startup,
`3` the sweep completed but at least one design scored 0 because the backend errored
mid-run (that is not a model result — rerun those designs with `--resume`), `130`
interrupted.

### 3. Reading the reports

Everything lands under `--out-dir`:

| Path | Contents |
| --- | --- |
| `results.jsonl` | One `DesignResult` per line, appended as each design finishes, each stamped with `mode` and the `run_config` fingerprint. The resume ledger and the raw record for any re-analysis. |
| `report.json` | Aggregate metrics: syntax/func counts, strict counts, round-0 counts, `pass@1_with_repair` and `pass@1_round0`, pass@k, the three adjustment bases (`totals` / `adjusted` / `adjusted_unpassable_only`), `llm_error_designs`, the `oracle` section (both catalogues, shims applied), and the run's configuration. |
| `report.md` | The same run as a human table — a `## Configuration` block, both pass@1 numbers, per-design rows, and a caveats section naming every broken, vacuous, shimmed and backend-failed design. |
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
`classify_output` returns first, and it is what `SimResult.func_pass` records. It is only
*sound* because the candidate is barred from writing to that stream at all — see
[Candidate admissibility](#candidate-admissibility).

**Strict rule** (`SimResult.func_pass_strict`, reported alongside, never instead of):
a pass marker is present **and** no failure marker is present **and** the run did not
time out or flood its output. Several RTLLM testbenches print per-vector `Failed` lines
and then a summary line that still contains the word "pass", and a design that hangs after
printing an early "pass" is not a passing design. The strict count is usually equal to or
one or two below the official count; when it is not, look at those designs before
believing the number.

**pass@k** — the unbiased estimator RTLLM's `auto_run.py` uses, over `n = --samples`
generations with `c` successes per design:

```text
pass@k = mean over designs of  1 - C(n - c, k) / C(n, k)
```

reported for both syntax and function in `report.json`. With `--samples 1` this
degenerates to the plain success rate.

**Two pass@1 numbers, always both.** A sample counts as a success if *any* of its rounds
passed, so with the default `--max-repair-rounds 2` one "sample" is up to three generations
with verifier feedback in between. Reporting that as `pass@1` would put an agent score
under the name of RTLLM's single-shot metric, so the report emits:

| key | meaning |
| --- | --- |
| `pass@1_with_repair` | any round of the sample passed — the **agent's** score. `pass@1` is kept as an alias of this. |
| `pass@1_round0` | only the first generation passed — the **single-shot** score, the one comparable to a published RTLLM `pass@1`. |

`report.md` prints both side by side, plus `designs_func_success_round0` next to
`designs_func_success`, and opens with a `## Configuration` table (samples, repair rounds,
evidence policy, shims, timeouts) so no table from it is quotable without its settings.
`--max-repair-rounds 0 --samples 5` remains the cleanest paper-comparable configuration.

**Adjusted rates correct in both directions.** `report.json` carries three bases:

| section | basis |
| --- | --- |
| `totals` | every selected design. Nothing dropped. |
| `adjusted` | drops the designs no RTL can pass (`KNOWN_ORACLE_ISSUES`), the designs an *empty module* passes (`VACUOUS_ORACLE_DESIGNS`), and designs whose samples all died on an LLM backend error. |
| `adjusted_unpassable_only` | drops only `KNOWN_ORACLE_ISSUES` — the earlier, one-directional basis, kept for continuity. It reads higher. |

Dropping the unpassable designs raises the rate and keeping the vacuous ones raises it too,
so an "adjusted" number that only did the first was biased upward from both ends at once.
On the reference baseline the difference is 47/47 (one-directional) versus 43/43 (both).

## Candidate admissibility

The official oracle greps the simulator's stdout — and under `vvp` the design under test
writes to that same stdout. So a candidate that can print can report its own verdict:

```verilog
module adder_8bit(input [7:0] a, input [7:0] b, input cin, output [7:0] sum, output cout);
  assign sum = 8'b0;
  assign cout = 1'b0;
  initial begin
    $display("===========Your Design Passed===========");
    $finish;                       // ends the run before the testbench can disagree
  end
endmodule
```

Measured on this harness before the gate existed, that scored `syntax_pass=True`,
`func_pass=True` **and** `func_pass_strict=True`. A one-line `initial $display("bypass mode
enabled");` bolted onto a zero-logic stub scored the official oracle on 44 of the 45 designs
whose stub compiled. No downstream check can undo this: once the marker is in the stream,
the verdict is indistinguishable from a real pass.

`find_illegal_system_tasks` therefore refuses any candidate containing an output or
control system task, **before compiling it**, as the `illegal_system_task` family:

- Refused: `$display`, `$write`, `$monitor`, `$strobe`, `$fdisplay`, `$fwrite`,
  `$fmonitor`, `$fstrobe` (`$fdisplay(1, …)` is stdout), `$finish`, `$stop`, `$dump*`.
- Allowed: `$signed`, `$unsigned`, `$clog2`, `$bits`, `$time`, `$random` — legitimate in
  RTL and unable to reach stdout.
- Comments and string literals are excluded by a real scanner, so `// no $display here` and
  `parameter S = "$finish";` are fine.

The generator prompt already forbade these tasks; a prompt is not a gate. The candidate is
**rejected, not silently stripped**: a design that tries to print is a real benchmark
failure and the repair agent is given the offending lines. The benchmark's own
`verified_*.v` files contain none of these tasks, so `--reference` is unaffected (verified:
50/50 syntax, 47/50 functional before and after).

Two related bounds live in the same place:

- `RUNAWAY_OUTPUT_BYTES` (8 MiB per stream) — output is drained by reader threads into a
  fixed-size tail buffer and the process group is killed when a stream blows the budget.
  Before this, a `$display` in a repeating block buffered **683 MB in one Python string**
  for a 10-second `--sim-timeout` (1.5 GB peak RSS, ~16 GB at `--workers 8`), and the
  watchdog itself was defeated: a 10 s timeout took 29.4 s wall because the kill path still
  had to drain and decode the backlog. It is now 0.3 s and 14 MB, bucketed `runaway_output`.
- `MAX_RESPONSE_CHARS` (512k) in `rtllm_agent` — the model response is capped and the
  module scanner is linear. The old `module … .*? endmodule` regex was quadratic in
  response length: 2000 repeated headers took 8.9 s and 20 000 did not finish in 110 s,
  hanging a worker thread with no timeout and no cancellation.

## Benchmark Integrity

What the model is shown:

- `design_description.txt` verbatim — the natural-language spec, which already states the
  required module name and port table.
- The required top module name.
- On repair rounds only: the failure family and, under the default evidence policy, a
  truncated excerpt of the compile and simulation logs (`EVIDENCE_LIMIT = 4000`
  characters).

What the model is **never** shown:

- The benchmark's `verified_*.v` reference RTL. `reference_rtl_text` and `empty_stub_rtl`
  exist solely for `--reference` / `--empty-baseline` runs and are not reachable from any
  prompt-building path.
- The `testbench.v` source. Not in the generator prompt, not in the repair prompt, not
  as "helpful context".
- Any golden data file.

**The prompt is not the only channel.** The default backend is the local Claude Code CLI,
which is an *agent* with filesystem tools, not a completion endpoint. Run unrestricted it
inherits the driver's cwd — and this harness stages a copy of the golden testbench under
`--out-dir/work/<design>/sample<NN>/round<N>/` on every attempt. Verified before the fix:
an unrestricted `claude -p` returned the first three lines of a staged `testbench.v`
verbatim. `ClaudeCLIClient` therefore runs each call with `--disallowedTools` covering every
file/shell/network tool, `--permission-mode plan`, and `cwd` set to a fresh empty temp
directory that is deleted afterwards (`ClaudeCLIClient.sandboxed`).

Two layers, both partial on their own: a deny list can only name tools we know about, and
an empty cwd does not stop an absolute path. **If you add a backend that can call tools,
sandbox it the same way or stop calling the numbers clean.** The API backends
(`anthropic`, `openai`) have no tool surface and need nothing.

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

The oracle fails in two opposite directions, and a report that corrects only one of them is
biased. Both catalogues live in `rtllm_bench.py` and both are surfaced in `report.json`'s
`oracle` section and in `report.md`'s caveats:

| catalogue | meaning | count | measured by |
| --- | --- | --- | --- |
| `KNOWN_ORACLE_ISSUES` | too strict — **no** RTL can pass, not even the benchmark's own | 3 | `--reference` |
| `VACUOUS_ORACLE_DESIGNS` | too loose — an **empty** module passes | 4 | `--empty-baseline` |

### Too strict: designs no RTL can pass

Eight of the 50 designs need care under this setup, for four distinct reasons — but only
**three** of them survive as genuinely unpassable. Those three are recorded in
`KNOWN_ORACLE_ISSUES` (design name → human-readable reason) so the reports can flag them
instead of letting you chase a phantom bug in the generator.

| Design(s) | What breaks | Consequence |
| --- | --- | --- |
| `alu`, `calendar`, `signal_generator`, `asyn_fifo`, `multi_booth_8bit` | The testbench `$readmemh`s a golden data file (`reference.dat` / `reference.txt` / `tri_gen.txt` / `wfull.txt`+`rempty.txt`+`tdata.txt` / `test_data.dat`). | **Recoverable, and not an oracle bug.** Those files ship *inside the design directory*. `evaluate_rtl` copies every non-Verilog support file into the sandbox next to the testbench, so all five pass. A sandbox that copies only `testbench.v` makes them look permanently unpassable — that is a harness bug, not a benchmark defect. The golden `*.v` is still never copied. |
| `clkgenerator`, `radix2_div` | The benchmark's own `verified_*.v` fails its own testbench under iverilog (`clkgenerator`: 20 reported failures; `radix2_div`: 3 `Error: dividend=…` lines then `===========Failed===========`). | Upstream benchmark bugs. No candidate should be expected to pass; a "failure" here says nothing about the generator. **In `KNOWN_ORACLE_ISSUES`.** |
| `ring_counter` | Two problems. (1) `testbench.v:20` uses a SystemVerilog array initializer — `reg [7:0] data [0:9] = {8'b…, …};` — which iverilog rejects (*"sorry: Assignment to an entire array or to an array slice is not yet supported"*). (2) Underneath that, the two `always @(posedge clk)` blocks race: the block doing `i = i + 1` and the block testing `if (i == 9)` are unordered, and iverilog interleaves them so that `i == 9` is **never observed** (the checker sees 8 at t=85 and 10 at t=95). | The shim fixes (1), so the design now compiles and the reference matches all ten expected values with no `Failed at` line — yet the pass banner still never prints and it scores 0. Problem (2) cannot be fixed without rewriting the checker, which would weaken it. **In `KNOWN_ORACLE_ISSUES`.** |
| `asyn_fifo` | `testbench.v:102` uses a SystemVerilog `break` inside `repeat(...)`, which iverilog rejects: *"sorry: break statements not supported."* | Recoverable. The shim names the enclosing `initial` block and rewrites `break` into `disable <block>`; nothing follows the `repeat` inside that block, so this is exactly "leave the loop". Verified: the break path really is taken (at t=500, after 16 writes), the reference passes, and mutated RTL still fails. Un-shimmed this is `simulator_unsupported`. |
| `adder_pipe_64bit`, `multi_pipe_4bit` | The reference file's top module is named `verified_adder_64bit` / `verified_multi_pipe` rather than `verified_<design>`, so a fixed-prefix rename misses it. | **Baseline-only artifact.** `reference_rtl_text` renames the first `verified_*` module in the reference file to the design name, which handles both. LLM-generated RTL declares the right module name to begin with, so agent runs are unaffected. |

Shims are opt-out with `--no-shims`, and every `SimResult` carries `shim_applied` so a
shimmed result is never silently conflated with a clean one. A shim may only rewrite the
copied testbench into semantically equivalent Verilog-2001 that iverilog accepts. It may
never touch the design under test, weaken a check, or change stimulus.

### Too loose: designs an empty module passes

Four designs pass **both** oracles when handed a module with the correct port list and no
body whatsoever — every output undriven, no `assign`, no `always`, nothing:

| Design | Mechanism |
| --- | --- |
| `comparator_3bit`, `comparator_4bit` | `testbench.v:33` checks `if ((A > B && !A_greater) \|\| (A == B && !A_equal) \|\| (A < B && !A_less))`. With X-valued outputs the whole expression is X, the `if` takes the false branch, `error` is never incremented, and `if (error == 0)` prints `=========== Your Design Passed ===========`. |
| `sequence_detector` | `if (!sequence_detected) error = error + 1;` never fires while `sequence_detected` is X. |
| `square_wave` | `if (wave_out_tb == 1)` is X for an undriven output, so the consecutive-ones check never runs. |

Classic X-optimism, and it is 8% of the benchmark that any agent banks for free. Note that
`func_pass_strict` does **not** catch it: the run prints a genuine pass banner and no
failure marker.

These are measured, not guessed. `empty_stub_rtl` derives the port-only stub from the golden
file's module header (parameter block included, non-ANSI port declarations included, all 50
stubs compile), and `--empty-baseline` scores it for every design. Re-run it on your machine
before trusting the constant. The stub builder is conservative by construction: a stub it
cannot derive, or one that fails to compile, simply scores as a failure, so the measurement
can only *under*-report vacuity, never invent it.

`adjusted` drops these four alongside the three unpassable ones (43 designs);
`adjusted_unpassable_only` keeps them (47 designs) and reads higher.

### Measured baseline

Running the benchmark's own reference RTL against the benchmark's own testbenches under
iverilog 12.0, with support-file copying, the two testbench shims, and the top-module
rename all in force (i.e. what `--reference` actually does):

```text
syntax (compiled):        50 / 50
functional (official):    47 / 50
functional (strict):      47 / 50
```

So **47 — not 50 — is the honest denominator** for an agent run on this simulator. The
three shortfalls are `clkgenerator`, `radix2_div` and `ring_counter`, and they are
exactly the contents of `KNOWN_ORACLE_ISSUES`. An agent cannot beat 47/50 here, so quote
agent scores against that ceiling rather than against 50.

The matching floor, from `--empty-baseline` on the same checkout:

```text
syntax (compiled):        50 / 50      (a port-only stub always compiles)
functional (official):     4 / 50
functional (strict):       4 / 50
```

Those four are `comparator_3bit`, `comparator_4bit`, `sequence_detector` and `square_wave`
— exactly `VACUOUS_ORACLE_DESIGNS`. **The informative range is 4/50 to 47/50, 43 designs
wide.** `report.json`'s `adjusted` section reports over those 43 (giving the reference
43/43); `adjusted_unpassable_only` reports over 47 (giving 47/47). Quote both bounds or
neither.

For reference, a sandbox that copies only `testbench.v` and applies no shims measures
46/50 syntax and 41/50 functional instead. That lower number is a harness artifact, not a
property of the benchmark: the five `$readmemh` designs fail only because their golden
data was not copied, and `ring_counter`/`asyn_fifo` fail to compile only because the
shims are missing. If you see 41, check those two mechanisms before believing it.

The first thing you should run on a new machine is still `--reference`: trust the number
your own `report.md` prints, not the one in this file.

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
- **Candidate admissibility.** Upstream runs whatever the model emitted. This harness
  refuses a design file containing output or simulation-control system tasks
  (`illegal_system_task`), because under a substring oracle such a file can score itself —
  see [Candidate admissibility](#candidate-admissibility). This makes the harness *stricter*
  than upstream on exactly one axis, and the axis is "the design must not write the answer".
