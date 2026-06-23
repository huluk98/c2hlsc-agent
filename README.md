# c2hlsc_agent

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
VITIS_HLS_ROOT="/nvme1/vitis2024.2 1113 1001/Vitis_HLS/2024.2" \
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
VITIS_HLS_ROOT="/nvme1/vitis2024.2 1113 1001/Vitis_HLS/2024.2" \
  bash scripts/run_vitis_linux.sh \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

Alternatively, let the script call that named Conda env directly:

```bash
C2HLSC_CONDA_ENV=hlsc \
VITIS_HLS_ROOT="/nvme1/vitis2024.2 1113 1001/Vitis_HLS/2024.2" \
  bash scripts/run_vitis_linux.sh \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

If the settings script does not put `vitis_hls` on `PATH`, pass the binary directly:

```bash
VITIS_HLS_BIN="/nvme1/vitis2024.2 1113 1001/Vitis_HLS/2024.2/bin/vitis_hls" \
  bash scripts/run_vitis_linux.sh \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

There is also a Python wrapper that can read the binary path from a local text
file. Put your path in `vitis_hls_bin_path.txt`, then run the wrapper:

```bash
conda activate hlsc
echo "/nvme1/vitis2024.2 1113 1001/Vitis_HLS/2024.2/bin/vitis_hls" > vitis_hls_bin_path.txt
python scripts/run_vitis_with_bin.py \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

Or pass the binary path without editing:

```bash
python scripts/run_vitis_with_bin.py \
  --vitis-hls-bin "/nvme1/vitis2024.2 1113 1001/Vitis_HLS/2024.2/bin/vitis_hls" \
  --config examples/vector_add/config.yaml \
  --out build/vector_add
```

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
- `--keep-going`
- `--verbose`

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

## Verification Order

1. Static analysis
2. Host software equivalence with `g++`
3. Vitis HLS `csim_design`
4. Vitis HLS `csynth_design`
5. Vitis HLS `cosim_design`

When `--no-run-vitis` is used, Vitis phases are marked `skipped`; host equivalence is
still run when `g++` is available.

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

Run unit tests:

```bash
python -m unittest discover -s c2hlsc_agent/tests
```
