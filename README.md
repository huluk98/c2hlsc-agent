# c2hlsc_agent

## Ubuntu Server Vitis Quick Triage

Use this workflow on the Ubuntu machine where Vitis HLS is installed. Keep it
verification-only: let the server run CSim/CSynth/CoSim and produce logs, then bring
the earliest failing evidence back to the Mac-side Codex/Claude repair loop.

From the `c2hlsc_agent` repo root on Ubuntu:

```bash
python3 -m pip install -e .
source /opt/Xilinx/Vitis_HLS/2023.2/settings64.sh  # adjust for your install
command -v vitis_hls
```

One-command triage when you already know the Vitis binary path:

```bash
VITIS_HLS_BIN=/opt/Xilinx/Vitis_HLS/2023.2/bin/vitis_hls bash scripts/run_hls_nl_vitis_triage.sh
```

That wrapper runs the fast CSim+CSynth sweep, filters passing rows, runs split CoSim on
the passing subset, writes a summary, and writes a `cosim_attention.jsonl` list for
timeout/failing cases to inspect.

The default timeout is 300 seconds for each Vitis invocation. In full-CoSim mode that
means up to 300 seconds for CSim, 300 seconds for CSynth, and 300 seconds for CoSim per
row. Override with `HLS_NL_CSYNTH_TIMEOUT=600` or `HLS_NL_COSIM_TIMEOUT=600` when you
want to give slower rows more room.

If you already ran some rows, do not rerun them. Point the wrapper at the previous
result file:

```bash
VITIS_HLS_BIN=/opt/Xilinx/Vitis_HLS/2023.2/bin/vitis_hls \
HLS_NL_COSIM_RESULTS=/path/to/previous/vitis_batch_results.jsonl \
bash scripts/run_hls_nl_vitis_triage.sh
```

Use `HLS_NL_CSYNTH_RESULTS=/path/to/vitis_batch_results.jsonl` instead if the previous
file came from the fast CSim+CSynth pass. A supplied CoSim results file also acts as a
skip list for the fast pass, so full-CoSim rows that already ran are not sent through
CSim/CSynth again.

Fast first pass over the repaired HLS_NL rows. This runs CSim+CSynth only, with one
timeout per row, so bad cases cannot stall the whole sweep:

```bash
python3 scripts/run_hls_nl_vitis_batch.py \
  --input data/hls_nl/hls_nl_repaired.accepted.jsonl \
  --out-dir runs/hls_nl_csynth_triage \
  --limit 9900 \
  --timeout-seconds 300 \
  --log-tail-lines 120
```

Summarize outcomes:

```bash
python3 - <<'PY'
import collections
import json
from pathlib import Path

path = Path("runs/hls_nl_csynth_triage/vitis_batch_results.jsonl")
status = collections.Counter()
failed_phase = collections.Counter()
for line in path.read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    status[row.get("status", "unknown")] += 1
    failed_phase[row.get("failed_phase", "pass_or_unset")] += 1
print("status_counts:", dict(status))
print("failed_phase_counts:", dict(failed_phase))
PY
```

Create a CoSim input containing only rows that passed the fast CSim+CSynth triage:

```bash
python3 - <<'PY'
import json
from pathlib import Path

source = Path("data/hls_nl/hls_nl_repaired.accepted.jsonl")
results = Path("runs/hls_nl_csynth_triage/vitis_batch_results.jsonl")
out = Path("runs/hls_nl_csynth_pass.jsonl")

passed_ids = set()
for line in results.read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    if row.get("status") == "pass":
        passed_ids.add(int(row["record_id"]))

out.parent.mkdir(parents=True, exist_ok=True)
with source.open(encoding="utf-8") as src, out.open("w", encoding="utf-8") as dst:
    for fallback, line in enumerate(src):
        record = json.loads(line)
        try:
            record_id = int(record.get("record_id", fallback))
        except (TypeError, ValueError):
            record_id = fallback
        if record_id in passed_ids:
            dst.write(json.dumps(record, sort_keys=True) + "\n")

print(f"wrote {out} with {len(passed_ids)} records")
PY
```

Run split CSim/CSynth/CoSim on that smaller set. The timeout applies to each phase, so
a CoSim hang is recorded as `status=timeout` and `failed_phase=cosim` instead of
blocking the run indefinitely:

```bash
python3 scripts/run_hls_nl_vitis_batch.py \
  --input runs/hls_nl_csynth_pass.jsonl \
  --out-dir runs/hls_nl_cosim_triage \
  --run-full-cosim \
  --timeout-seconds 300 \
  --log-tail-lines 160
```

Rerun the summary command against
`runs/hls_nl_cosim_triage/vitis_batch_results.jsonl` to separate CoSim passes,
failures, and timeouts.

For very large runs, split by chunk in separate terminals or jobs, using a different
`--out-dir` per chunk:

```bash
python3 scripts/run_hls_nl_vitis_batch.py \
  --input runs/hls_nl_csynth_pass.jsonl \
  --out-dir runs/hls_nl_cosim_triage_0000 \
  --run-full-cosim \
  --offset 0 \
  --limit 500 \
  --timeout-seconds 300
```

Inspect timeout/fail cases by opening the phase log named in
`runs/.../vitis_batch_results.jsonl`, usually one of `vitis_csim.log`,
`vitis_csynth.log`, or `vitis_cosim.log`. For generated `c2hlsc_agent` projects, copy
the earliest failing log back to the Mac and repair from external evidence:

```bash
python3 -m c2hlsc_agent.cli repair \
  --project build/my_design \
  --stage cosim \
  --evidence /path/to/vitis_cosim.log
```

After a repair, copy the repaired project back to Ubuntu and rerun from CSim. Avoid
`--auto-repair` on the server unless generation, Vitis, and repair are intentionally
running in one local experiment.

`c2hlsc_agent` is a conservative local engineering agent that turns an ordinary C top
function into a Vitis HLS-oriented C/C++ project and verifies the generated code against
the original C implementation.

The highest priority is functional equivalence. The initial converter preserves the
original top-function structure where possible, emits diagnostics for unsafe constructs,
and generates a deterministic C++ testbench that compares the generated HLS top against
a macro-renamed golden reference copy of the original C file.

## Multi-Agent Functional RTL Loop

This repository now treats C-to-HLS-C-to-RTL as a verifier-closed loop, not a
single generation step. The intended agents are:

1. `contract_planner`: extracts the top-function contract, argument bounds, legal input
   domain, and unsupported C constructs.
2. `shift_left_testbench_agent`: builds the golden-C oracle testbench, directed/random
   stimuli, and future coverage/KLEE/gcov augmentation.
3. `hlsc_generator_agent`: emits synthesizable HLS-C and records every transformation.
4. `cosim_operator`: runs host equivalence, Vitis CSim, synthesis, and C/RTL CoSim in
   short-circuit order.
5. `failure_analyst`: classifies the earliest failing stage and packages compact repair
   evidence; CoSim mismatches should use PMLC-style log normalization, slicing, and
   selective instrumentation.
6. `hlsc_repair_agent`: applies minimal patches, then reruns the full verifier.
7. `rtl_optimizer_agent`: runs only after equivalence is locked, and accepts PPA changes
   only after host equivalence, CSim, synthesis, and CoSim pass again.
8. `audit_memory_agent`: stores reproducible artifacts and promotes only audited repair
   successes into retrieval memory.

Important correction: Vitis C/RTL CoSim checks generated RTL against the HLS-C design
under the supplied testbench. It does not, by itself, prove that RTL is equivalent to the
original C. For a defensible "functional equivalent RTL" claim, the loop must keep the
original C in the oracle path, maintain synchronized stimuli, and rerun the complete
verification stack after every repair or optimization.

## HLS-C Generator Agent

The HLS-C generator contract lives in `c2hlsc_agent/hlsc_generator.py` as
`hlsc_generator_vitis_beginner_v1`. It is intentionally separate from the sidecar
testbench generator in `c2hlsc_agent/testgen.py`.

That generator policy instructs `hlsc_generator_agent` to:

- default to AMD/Xilinx Vitis HLS syntax when the target is unspecified
- analyze hotspot loops, loop-carried dependencies, memory-port bottlenecks, helper
  function boundaries, and top-level interfaces before editing
- preserve functional correctness first and only add justified pragmas
- copy the original function exactly in the user-facing HLS report
- emit beginner-readable Vitis HLS annotated code with comments explaining every pragma
- include expected hardware impact, trade-offs, Intel HLS notes, and a synthesis report
  checklist

The expected user-facing HLS-C generator response sections are:

1. Assumptions
2. Hotspot analysis
3. Original code
4. Vitis HLS annotated code
5. Expected hardware impact
6. Trade-offs / risks
7. Intel HLS notes
8. Report checklist

### LLM-backed generation and repair (opt-in)

By default the generator copies the original top-function body verbatim and the
`hlsc_repair_agent` applies only mechanical regex fixes, so the whole pipeline is
deterministic and offline. Passing `--use-llm` activates a real model behind the two
agents:

- `hlsc_generator_agent` sends the `hlsc_generator_vitis_beginner_v1` policy plus the
  source and argument contract to Claude and uses the returned synthesizable
  translation unit as `src/hls_top.cpp`.
- `hlsc_repair_agent` escalates to Claude for a minimal patch when no mechanical repair
  matches the earliest failing stage, using the classified failure evidence.

The LLM only *proposes* candidate HLS-C — the existing verifier ladder (host equivalence
→ CSim → CSynth → CoSim) is still the equivalence gate, the golden `input.c` is never
handed to the model, the LLM repair only ever rewrites `src/hls_top.cpp` (never the
golden-oracle testbench), and any unavailable/unparsable response falls back to the
conservative path. When `--use-llm` is requested but no backend resolves, the run prints a
warning and continues deterministically (exit code unaffected).

#### Backends

The model is a pluggable backend (`--llm-backend`), so the agent does not depend on any
one cloud API:

- **`openai`** — any OpenAI Chat Completions-compatible endpoint, using only the standard
  library (no extra dependency). This is how it runs on a **local model with no cloud
  key**: point `--llm-base-url` at Ollama / LM Studio / llama.cpp / vLLM. The same backend
  also reaches OpenAI-compatible cloud providers.
- **`anthropic`** — the Anthropic Claude API (needs `pip install '.[llm]'` and
  `ANTHROPIC_API_KEY`).
- **`auto`** (default) — prefers a configured OpenAI-compatible endpoint (incl. local),
  then Anthropic, then OpenAI cloud; falls back to deterministic if nothing is configured.

Local model, no API key (e.g. `ollama pull qwen2.5-coder` then `ollama serve`):

```bash
python -m c2hlsc_agent.cli convert \
  --config examples/vector_add/config.yaml \
  --out build/vector_add \
  --use-llm \
  --llm-backend openai \
  --llm-base-url http://localhost:11434/v1 \
  --llm-model qwen2.5-coder \
  --no-run-vitis            # add --run-vitis on a machine with Vitis HLS
```

Anthropic Claude API:

```bash
python3 -m pip install -e '.[llm]'
export ANTHROPIC_API_KEY=sk-ant-...
python -m c2hlsc_agent.cli convert --config examples/vector_add/config.yaml \
  --out build/vector_add --use-llm --llm-backend anthropic --no-run-vitis
```

OpenAI-compatible cloud (OpenAI, Groq, Together, OpenRouter, …):

```bash
export OPENAI_API_KEY=...                 # and OPENAI_BASE_URL for non-OpenAI providers
python -m c2hlsc_agent.cli convert --config examples/vector_add/config.yaml \
  --out build/vector_add --use-llm --llm-backend openai --llm-model gpt-4o-mini --no-run-vitis
```

Backend selection also works from a config file (`use_llm: true`, `llm_backend: openai`,
`llm_base_url: ...`, `llm_model: ...`) and from environment variables
(`C2HLSC_LLM_BASE_URL` / `OPENAI_BASE_URL`, `C2HLSC_LLM_API_KEY` / `OPENAI_API_KEY`,
`C2HLSC_LLM_MODEL`).

## HLS-LeVeri-Style Testbench Generator

AUTO RTL also emits an HLS-LeVeri-inspired paired trace testbench bundle. The reference
framework is [`cz-5f/HLS-LeVeri`](https://github.com/cz-5f/HLS-LeVeri), whose preview
dataset is organized around paired artifacts:

```text
(golden C, HLS-C, golden-C testbench, HLS-C testbench)
```

The local policy lives in `c2hlsc_agent/leveri_testgen.py` as
`hls_leveri_shift_left_v1`. It is owned by `shift_left_testbench_agent`, not by
`hlsc_generator_agent`.

For every generated project, AUTO RTL now writes:

- `tb/leveri_golden_tb.cpp`: runs the macro-renamed original C and writes
  `leveri_golden_trace.csv`
- `tb/leveri_hls_tb.cpp`: runs the generated HLS-C top and writes
  `leveri_hls_trace.csv`
- `tb/leveri_compare.py`: checks static trace alignment and dynamic output consistency
- `tb/run_gcov.py`: compiles/runs the paired traces with gcov coverage flags
- `tb/klee_driver.cpp`: symbolic KLEE driver for the golden C top function
- `tb/run_klee.py`: optional KLEE runner that writes a skip report if KLEE is absent
- `tb/leveri_manifest.json`: records KG-ready metadata for the testbench bundle

Run the paired trace check with:

```bash
make leveri-test
```

Run coverage hooks with:

```bash
make gcov-coverage
make klee-coverage
make coverage
```

`gcov` reports are written to `coverage/gcov_report.json`. KLEE reports are written to
`coverage/klee_report.json`; when KLEE is not installed, the script exits successfully
with a `skipped` report so the generated project remains portable.

## Install

From this repository:

```bash
python3 -m pip install -e c2hlsc_agent
```

Or, from the standalone GitHub repo root:

```bash
python3.11 -m pip install -r requirements.txt
python3.11 -m pip install -e .
```

No commercial parser is required. The analyzer uses a robust regex fallback. Optional
`PyYAML` is used when available, but a small built-in YAML subset parser supports the
example configs.

## Linux Conda + Vitis

For a separate Linux machine, Conda should install only the Python/build environment.
Xilinx Vitis must already be installed and licensed on that machine.

Create/update the environment:

```bash
cd c2hlsc_agent
bash scripts/setup_linux_conda.sh
```

Run Vitis locally on that Linux machine:

```bash
cd c2hlsc_agent
VITIS_HLS_ROOT="/opt/Xilinx/Vitis_HLS/2024.2" \
  bash scripts/run_vitis_linux.sh \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

If your Vitis path contains spaces, keep the quotes:

```bash
VITIS_HLS_ROOT="/path/to/Vitis_HLS/2024.2" \
  bash scripts/run_vitis_linux.sh \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

If you already activated your own Conda environment, for example `hlsc`, use
the active environment instead of the script's default `c2hlsc-linux` env:

```bash
conda activate hlsc
python -m pip install -r requirements.txt
python -m pip install -e .
C2HLSC_USE_ACTIVE_ENV=1 \
VITIS_HLS_ROOT="/path/to/Vitis_HLS/2024.2" \
  bash scripts/run_vitis_linux.sh \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

Alternatively, let the script call that named Conda env directly:

```bash
C2HLSC_CONDA_ENV=hlsc \
VITIS_HLS_ROOT="/path/to/Vitis_HLS/2024.2" \
  bash scripts/run_vitis_linux.sh \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

If the settings script does not put `vitis_hls` on `PATH`, pass the binary directly:

```bash
VITIS_HLS_BIN="/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
  bash scripts/run_vitis_linux.sh \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

There is also a Python wrapper that can read the binary path from a local text
file. Put your path in `vitis_hls_bin_path.txt`, then run the wrapper:

```bash
conda activate hlsc
echo "/path/to/Vitis_HLS/2024.2/bin/vitis_hls" > vitis_hls_bin_path.txt
python scripts/run_vitis_with_bin.py \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

Or pass the binary path without editing:

```bash
python scripts/run_vitis_with_bin.py \
  --vitis-hls-bin "/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

Run the single embedded JSON smoke bundle with only a Vitis path:

```bash
python scripts/run_vitis_bundle.py \
  --vitis "/path/to/Vitis_HLS/2024.2/bin/vitis_hls"
```

This unpacks `configs/simple_calculator_vitis_cosim_bundle.json` into
`build/vitis_bundle_run`, then runs CSim, CSynth, and C/RTL CoSim. If Vitis
fails, the wrapper writes `build/vitis_bundle_run/vitis_hls.log` and prints the
tail of the log so the real CSim or synthesis error is visible.

Run the accepted HLS_NL JSONL dataset and verify that Vitis emits Verilog:

```bash
VITIS_HLS_BIN="/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
python scripts/run_hls_nl_vitis_batch.py \
  --input data/hls_nl/hls_nl_repaired.accepted.jsonl \
  --out-dir build/hls_nl_accepted_vitis \
  --limit 10 \
  --part xczu7ev-ffvc1156-2-e \
  --clock 10
```

The batch runner writes `vitis_verilog_report.json` and
`vitis_verilog_results.jsonl` under the output directory. A record is marked
`pass` only when `vitis_hls` exits successfully and at least one `.v` or `.sv`
file appears under `hls_nl_project/solution1/syn/verilog`.

Use `--run-full-cosim` when you want C/RTL CoSim as well as Verilog emission.
In full-CoSim mode the batch runner executes split Vitis phases
(`run_csim.tcl`, `run_csynth.tcl`, `run_cosim.tcl`) so the JSON report can
identify the earliest failed or timed-out phase. Each phase gets its own
`vitis_<phase>.log`, plus an aggregate `vitis_full.log`. The default batch
timeout is 900 seconds per phase; the smoke config uses 300 seconds per phase.

For a full-CoSim corpus where only passing cases are kept for upload:

```bash
VITIS_HLS_BIN="/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
python scripts/run_hls_nl_vitis_batch.py \
  --config configs/hls_nl_full_cosim.json

python scripts/export_cosim_successes.py \
  --report build/hls_nl_accepted_full_cosim/vitis_batch_report.json \
  --out-dir hls_nl_full_cosim_passes
```

The export directory contains only `status=pass` full-CoSim cases with compact
evidence files. Failed rows are not copied as projects; they are listed in
`hls_nl_full_cosim_passes/failed.jsonl` for later inspection.

For the first CoSim check, use the small JSON config and shell wrapper:

```bash
cd c2hlsc_agent
VITIS_HLS_BIN="/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
  bash scripts/run_hls_nl_cosim_smoke.sh
```

The default config is `configs/hls_nl_cosim_smoke.json`, which runs one JSONL
record from `data/hls_nl/hls_nl_repaired.accepted.jsonl` through CSim, CSynth,
and C/RTL CoSim. The wrapper prints the summary,
the Vitis log tail, generated Verilog files, and any discovered CoSim artifacts.
The smoke timeout is intentionally short so a bad protocol testbench fails
quickly. Increase the sample with `HLS_NL_LIMIT=3` after the first row looks
sane.

Run on a remote Linux host from this machine:

```bash
REMOTE=user@linux-host \
REMOTE_DIR=~/c2hlsc_agent \
VITIS_SETTINGS_REMOTE=/opt/Xilinx/Vitis/2022.1/settings64.sh \
  bash c2hlsc_agent/scripts/run_remote_vitis.sh \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

## CLI

```bash
python -m c2hlsc_agent.cli convert \
  --input path/to/input.c \
  --top TOP_FUNCTION_NAME \
  --out build/hls_project \
  --part xczu7ev-ffvc1156-2-e \
  --clock 10 \
  --num-tests 1000 \
  --no-run-vitis
```

Vitis execution is opt-in:

```bash
python -m c2hlsc_agent.cli convert \
  --config c2hlsc_agent/examples/vector_add/config.yaml \
  --out build/vector_add \
  --run-vitis
```

Supported options:

- `--config config.yaml`
- `--no-run-vitis`
- `--run-vitis`
- `--cosim-tool xsim`
- `--rtl verilog`
- `--seed 1234`
- `--max-iterations 1`
- `--auto-repair`
- `--keep-going`
- `--use-llm` / `--no-llm`
- `--llm-backend auto|none|anthropic|openai`
- `--llm-base-url http://localhost:11434/v1`
- `--llm-model qwen2.5-coder`
- `--verbose`

Use `python -m c2hlsc_agent.cli repair --help` for the separate external-evidence
repair command.

## Config Format

```yaml
input_files:
  - input.c
top: vector_add
include_dirs: []
compiler_flags: []
part: xczu7ev-ffvc1156-2-e
clock: 10
num_tests: 100
seed: 1
interface_mode: ap_memory
allow_pragmas: true
allow_performance_pragmas: false
arguments:
  a:
    direction: input
    length: 16
  b:
    direction: input
    length: 16
  out:
    direction: output
    length: 16
  n:
    range: [0, 16]
```

Argument metadata is important for pointers and arrays. If a pointer bound is missing,
the tool uses a conservative default bound and emits a diagnostic.

## Generated Files

For each conversion, the output directory contains:

- `input.c` copied golden reference source
- `src/hls_top.hpp`
- `src/hls_top.cpp`
- `tb/testbench.cpp`
- `run_hls.tcl`
- `Makefile`
- `run_all.sh`
- `conversion_report.md`
- `conversion_report.json`
- `repair_audit.json` when a repair iteration was attempted

## Verification Order

1. Static analysis
2. Host software equivalence with `g++`
3. Vitis HLS `csim_design`
4. Vitis HLS `csynth_design`
5. Vitis HLS `cosim_design`

When `--no-run-vitis` is used, Vitis phases are marked `skipped`; host equivalence is
still run when `g++` is available.

By default, `convert` does not mutate a failing project after verification. This keeps
the current split-machine workflow explicit: run Vitis/CoSim wherever the toolchain is
installed, bring the earliest failing phase log back, then invoke `repair` with that
evidence. `--auto-repair` is available only for local experiments where generation,
verification, and repair all run in the same agent session.

Manual repair from external evidence:

```bash
python -m c2hlsc_agent.cli repair \
  --project build/vector_add \
  --stage cosim \
  --evidence /path/from/vitis_machine/vitis_cosim.log
```

The repair backend only applies mechanical, auditable repairs to generated files:
missing standard includes, C++ `restrict` compatibility, original helper-function
support inclusion, and generated interface pragma removal after interface-stage
failures. Repairs are recorded in `repair_audit.json`; external-evidence repairs also
write `manual_repair_report.json`.

## Testbench Generation

The generated `tb/testbench.cpp` keeps one synchronized oracle path:

- macro-renamed original C as the golden reference
- generated HLS-C top under the same input vectors
- directed scalar boundaries for ranged arguments such as `n: [0, 16]`
- output-only buffer sentinels so missed writes are easier to expose
- active-length output comparisons when a length-like scalar, such as `n`, is declared
- compact mismatch traces with the seed, active compare length, scalar values, and
  same-index input values

This matters for CoSim because C/RTL simulation checks the RTL through the testbench.
Inactive output elements outside the declared active range are not used as functional
equivalence evidence unless the config says they are part of the contract.

## Limitations

This first implementation is intentionally conservative:

- It preserves a single top-function body rather than performing aggressive C rewrites.
- Helper functions, complex globals, structs, and alias-heavy kernels may require manual
  refactoring or richer metadata.
- Unsupported constructs such as dynamic allocation, recursion, non-deterministic or
  runtime-only standard library calls, file I/O in the top, variable-length arrays, and
  unsafe pointer arithmetic are reported instead of silently converted.
- Performance pragmas are not added unless configured and explained in the report.

Poor HLS performance is reported but is not considered a functional failure.

## Examples

```bash
python -m c2hlsc_agent.cli convert \
  --config c2hlsc_agent/examples/vector_add/config.yaml \
  --out /tmp/c2hlsc_vector_add \
  --no-run-vitis

python -m c2hlsc_agent.cli convert \
  --config c2hlsc_agent/examples/simple_fir/config.yaml \
  --out /tmp/c2hlsc_fir \
  --no-run-vitis

python -m c2hlsc_agent.cli convert \
  --config c2hlsc_agent/examples/bit_ops/config.yaml \
  --out /tmp/c2hlsc_bit_ops \
  --no-run-vitis
```

Generate Vitis-oriented testbench bundles from an `HLS_NL.json` dataset:

```bash
python scripts/generate_hls_nl_testbenches.py \
  --input /path/to/HLS_NL.json \
  --out-dir build/hls_nl_testbenches
```

The generated bundles are labeled by oracle strength: `semantic` for recognized
self-checking patterns, `property` for stateful/protocol drivers that need a
contract audit, and `smoke` for deterministic CoSim stimulus only.

Run unit tests:

```bash
python -m unittest discover -s c2hlsc_agent/tests
```
