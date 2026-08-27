# Input contract — what you supply, what you get back

Everything here is read out of the code, not recalled: `analyze.py` for what the C parser
accepts, `config.py` for the keys, `convert.py` for the pragma mapping, `hls_project.py`
for the artifact list. Where behaviour is surprising it is marked **Trap**.

You supply **two files**. Everything else is generated.

```
your_design/
  input.c        # the golden behavioural reference — never rewritten by the agent
  config.yaml    # the contract: what each argument is, and how hard to test it
```

---

## 1. `input.c` — what the analyzer accepts

### How the top function is found

A regex looks for `<return-type> <top>(<params>) {` and then brace-matches to the end.
Consequences worth knowing:

- The top must be a **definition, not a prototype** — the pattern requires the opening `{`.
- `#include`s are not followed. Anything the top calls must be **in the same file**, and
  the whole file is `#include`d into the generated testbenches, so it must compile as C.
- Only **`input_files[0]`** is analysed and copied into the project. Extra entries in
  `input_files` change the run fingerprint (and so the run id) but are **not compiled**.
- Comments are stripped before the body is scanned, so a rejected construct inside a
  comment does not trip the checks.

### Parameter parsing

Per parameter, `_parse_arg` extracts array dimensions (`[...]`), pointer depth (`*` count),
the name (last token), and the type (everything before it). `restrict` / `__restrict` /
`__restrict__` are stripped, because they are C99 and the generated headers are C++.

`length` is taken from config; if absent, the **first numeric array dimension** is used —
so `int a[16]` self-describes, but `int *a` does not.

### Direction inference

Direction is decided in this order:

1. `arguments.<name>.direction` from config, if set — always wins.
2. Otherwise, for pointer-like arguments, the body is scanned: written **and** read →
   `inout`; written only → `output`; neither → `input`.
3. Scalars and `const` arguments → `input`.

Direction drives the trace column roles (`in` / `out`) and which buffers the oracle
compares, so it is worth setting explicitly when the inference has to read a non-obvious
body.

### Hard rejections

These are `error` diagnostics. They stop the run unless you pass `--keep-going`.

| Code | Trigger |
|---|---|
| `dynamic-allocation` | `malloc` / `calloc` / `realloc` / `free` |
| `unsupported-stdlib-call` | `rand`, `srand`, `qsort`, `bsearch`, `time`, `clock`, `exit`, `abort`, `setjmp`, `longjmp` |
| `system-call` | `system`, `popen`, `fork`, `exec*` |
| `file-io` | `fopen`, `fclose`, `fread`, `fwrite`, `fprintf`, `fscanf`, `printf`, `scanf` |
| `function-pointer` | an indirect call `(*fp)(...)` |
| `unbounded-loop` | `for(;;)` or `while(1)` |
| `recursion` | the top calls itself |
| `pointer-arithmetic` | `p++`, `p += n`, `p + n`, `*(p + n)` on a pointer argument |
| `variable-length-array` | a local array whose bound is not a literal |

**Write indexed access against a configured bound** (`a[i]` with `n` in range), not pointer
walking. That is the single most common reason a real design is rejected.

### Warnings

| Code | Meaning |
|---|---|
| `missing-pointer-bound` | a pointer-like argument has no `length`; **16 is assumed**. Set it. |

---

## 2. `config.yaml` — every key

Both YAML and JSON are accepted. Relative paths resolve against the **config file's own
directory**, not the working directory.

### Design

| Key | Default | Effect |
|---|---|---|
| `input_files` (or `input`) | `[]` | List; **only the first is analysed**. `--input` overrides. |
| `top` | `null` | Top function name. Required (or `--top`). |
| `part` | `xczu7ev-ffvc1156-2-e` | Vitis part, written into the tcl. |
| `clock` (or `clock_period`) | `10.0` | Clock period in ns. |
| `allow_pragmas` | `true` | When false, **no** `#pragma HLS` is emitted at all, whatever `interface_mode` says. |
| `interface_mode` | `default` | See below. |
| `compiler_flags` | `[]` | Extra flags threaded into the generated host build. |
| `include_dirs` | `[]` | **Accepted, resolved, and then never used.** Parsed by `load_config` and read by nothing. Put include paths in `compiler_flags` instead. |

#### `interface_mode`

| Value | Pointer-like args | Scalars | Return |
|---|---|---|---|
| `default` | *(no pragmas at all)* | — | — |
| `ap_memory` | `ap_memory` | `s_axilite` | `s_axilite` |
| `m_axi` | `m_axi` | `s_axilite` | `s_axilite` |
| `axis` | `axis` | `s_axilite` | `s_axilite` |
| `s_axilite` | `s_axilite` | `s_axilite` | `s_axilite` |

**Trap.** An unrecognised value is **not** an error. Pointer arguments silently get *no*
pragma, while scalars and the return still get `s_axilite`. A typo like `ap_memmory` gives
you a project that synthesises with the wrong interface and says nothing. Spell it exactly.

**Trap.** `m_axi` and `axis` put an AXI adapter on the RTL ports, which the standalone
direct-RTL testbench does not model. For those modes, rely on Vitis cosim, or regenerate
from the synthesised netlist with `tb/gen_rtl_tb.py --from-rtl`.

### Stimulus

| Key | Default | Effect |
|---|---|---|
| `num_tests` (or `random_test_count`) | `100` | Total cycles in every testbench tier. |
| `seed` | `1` | Seeds the `mt19937_64` used by every tier, so all tiers see identical stimulus. |
| `directed_tests` | `[zeros, ones, minmax, alternating]` | Occupies the **first N cycles**; the rest are pseudo-random. |
| `arguments` | `{}` | Per-argument metadata; see below. |

Valid `directed_tests` names: **`zeros`, `ones`, `minmax`, `alternating`, `random`**.
An unknown name raises `StimulusError` rather than being ignored — though it currently
surfaces as an uncaught traceback rather than a clean CLI message. The last line names the
offender and lists the supported set, e.g. `unknown directed_tests pattern(s): wibble`.

`random` means "leave this slot pseudo-random". `minmax` only applies to integer types; a
float slot falls through to random.

`extra_vectors` exists on the config object but is **not read from your file** — it is run
evidence, written only by coverage refinement.

#### `arguments.<name>`

| Sub-key | Type | Effect |
|---|---|---|
| `direction` | `input` \| `output` \| `inout` | Overrides body inference. Sets trace column roles. |
| `length` | int | Element count for a pointer/array argument. Sizes the testbench buffers. |
| `range` | `[lo, hi]` | Scalar drawn inside `[lo, hi]` inclusive, instead of over the whole type. |
| `interface` | string | Per-argument interface override, recorded in the report. |

**Set `range` on any scalar used as a loop bound.** Without it the scalar is drawn across
the entire integer range, and a testbench that then loops `for (i = 0; i < n; ++i)` writes
past the end of its buffers. This is not theoretical — it was a live bug, fixed in
`d7ce172`.

#### Active-length clamping

If a scalar is named like a length — `n`, `len`, `length`, `size`, `count`, `num`, `limit`,
`samples`, `elements`, or `<array>_len` / `num_<array>` / `<array>_count` and friends — and
its `range` fits **inside** the companion array's `length`, then every tier compares only
that many elements. The tail of the buffer is outside the declared contract, and comparing
it would report a false mismatch.

This is why `n: {range: [0, 16]}` next to `a: {length: 16}` matters: it is what makes a
partially-filled output buffer verifiable.

### Vitis

| Key | Default | Effect |
|---|---|---|
| `run_vitis` | `false` | Whether the Vitis rungs run at all. |
| `cosim_tool` | `null` | e.g. `xsim`; becomes `cosim_design -tool <x>`. |
| `rtl` | `verilog` | `cosim_design -rtl <x>`. |
| `vitis_ssh_host` | `null` | Run Vitis phases over SSH. **Setting it implies `run_vitis: true`.** Also read from `C2HLSC_VITIS_SSH`. |
| `vitis_remote_dir` | `~/c2hlsc_runs` | Remote scratch directory. |
| `vitis_setup` | `null` | Shell prefix that puts `vitis_hls` on PATH. |
| `vitis_bin` | `vitis_hls` | Remote executable name or absolute path. |

### Model use (all optional; the default path is fully deterministic and offline)

| Key | Default |
|---|---|
| `use_llm` | `false` |
| `llm_backend` | `auto` — one of `auto`, `none`, `claude-cli`, `anthropic`, `openai` |
| `llm_model` | per backend |
| `llm_base_url` | `null` (for OpenAI-compatible local servers) |
| `llm_cli_cmd` | `claude` |
| `llm_candidates` | `1` — best-of-N, scored by host equivalence |
| `nl_spec` | `null` |

### Bounded run

| Key | Default |
|---|---|
| `max_iterations` | `1` |
| `auto_repair` | `false` |
| `keep_going` | `false` |
| `run_id` | derived from the inputs |
| `max_wall_seconds` | `14400` |
| `max_llm_calls` | `8` |
| `max_vitis_runs` | `8` |

Budgets are **immutable per run id**. Re-running with the same inputs resumes the same run;
`--new-run` starts a fresh one. A run already recorded as passed refuses to restart.

---

## 3. Worked example

`input.c`:

```c
#include <stdint.h>

void guarded_scale(const int32_t *a, int32_t *out, int n) {
  for (int i = 0; i < n; ++i) {
    if (a[i] == 12345) {
      out[i] = a[i] * 2;
    } else {
      out[i] = a[i] + 1;
    }
  }
}
```

`config.yaml`:

```yaml
input_files: [input.c]
top: guarded_scale
part: xczu7ev-ffvc1156-2-e
clock: 10
num_tests: 64
seed: 7
interface_mode: ap_memory
allow_pragmas: true
arguments:
  a:   {direction: input,  length: 16}   # length is required: `*a` has no array dim to read
  out: {direction: output, length: 16}
  n:   {range: [0, 16]}                  # named like a length and fits inside 16,
                                         # so every tier clamps to n elements
```

Run it:

```powershell
python -m c2hlsc_agent convert --input input.c --top guarded_scale `
  --config config.yaml --out build\guarded
```

---

## 4. What you get back

### Generated by every `convert` (25 files)

These are the files `write_project` emits, and it rewrites all of them. The one exception
is a **coverage-refinement round**, which reads `src/hls_top.cpp` back off disk and writes
it out unchanged — a repair the verifier already accepted survives refinement. A fresh
`convert` into the same directory has no such exemption and regenerates the design too.

| Artifact | What it is |
|---|---|
| `input.c` | Byte-identical copy of your golden C. The oracle. Never rewritten. |
| `src/hls_top.hpp`, `src/hls_top.cpp` | The generated HLS-C. **The only model-writable file is `hls_top.cpp`.** |
| `tb/testbench.cpp` | Host equivalence: generated design vs. golden C, same stimulus. |
| `tb/leveri_golden_tb.cpp`, `tb/leveri_hls_tb.cpp` | Paired per-cycle CSV trace writers, one per side. |
| `tb/leveri_compare.py` | The dual-tier check: static (header, roles, cycle count, stimulus columns, CFG shape, def-use) and dynamic (outputs). |
| `tb/leveri_manifest.json` | Policy id, checks performed, trace column roles. |
| `tb/stimulus_contract.json` | The argument metadata, test count and seed this project was built with, so `refine` can regenerate identical stimulus without `--config`. |
| `tb/run_gcov.py` | Line/branch coverage, plus the uncovered site list. |
| `tb/klee_driver.cpp`, `tb/run_klee.py` | Symbolic exploration; native KLEE, else the `klee/klee` container. |
| `tb/rtl_vectors_tb.cpp`, `tb/gen_rtl_tb.py`, `tb/run_rtl_sim.py`, `tb/rtl_tb_manifest.json` | Direct-RTL testbench path. |
| `tb/host_build.py` | **Every build recipe.** Runs on native Windows; `make` is not required. |
| `Makefile` | Thin alias over `host_build.py`. |
| `run_hls.tcl`, `run_csim.tcl`, `run_csynth.tcl`, `run_cosim.tcl` | Vitis entry points. |
| `run_all.py`, `run_all.sh` | Whole-flow drivers (`.py` is the portable one). |

### Written by a run, not by generation

| Artifact | Written by | Evidence for |
|---|---|---|
| `conversion_report.md` / `.json` | every run | Per-phase status, mismatches, diagnostics, decisions. **The primary result.** |
| `run_ledger.jsonl` | every run | Append-only bounded-run ledger: attempts, budgets, repeated states. |
| `coverage/gcov_report.json` | `gcov-coverage` | Line/branch percentages, uncovered lines and branches. |
| `coverage/klee_report.json` | `klee-coverage` | `status`, `mode` (`native`/`docker`/`none`), `reason`, `.ktest` count. |
| `coverage_refinement.json` | `refine` | Baseline vs. final coverage, per-round `strategy` (`klee` or `widen`), vectors added. |
| `repair_audit.json` | `repair` | Every repair applied, with owner agent and rationale. |
| `candidate_scores.json` | `--candidates N` | Per-candidate host-equivalence scores. |
| `manual_repair_report.json` | `repair` from external evidence | The externally-supplied failure and what was done with it. |
| `software_equivalence.log`, `trace_consistency.log` | every run | Raw stdout/stderr of each host rung — what to read first when one fails. |
| `leveri_golden_trace.csv`, `leveri_hls_trace.csv` | every run | The two per-cycle traces the comparator diffs. Column 1 is `cycle`; row 2 is the `in`/`out` role header. |
| `c2hlsc_tb`, `leveri_golden_tb`, `leveri_hls_tb` | every run | Compiled host binaries (`.exe` on Windows). Build output — do not commit; `host_build.py clean` removes them. |

**Trap.** `refine` drives KLEE through the library, so its evidence is
`coverage_refinement.json`. `coverage/klee_report.json` is written only by the
`klee-coverage` target. Read the round's `strategy` to tell whether KLEE actually ran —
the `widen` fallback also raises coverage, and looks identical if you only read the number.

### Build targets (`python tb/host_build.py <target>`)

`test` · `leveri-test` · `gcov-coverage` · `klee-coverage` · `rtl-vectors` ·
`rtl-testbench` · `rtl-cosim` · `clean`

---

## 5. How to read the result

Five phases, short-circuiting in this order:

```
software_equivalence → trace_consistency → csim → csynth → cosim
```

Which phases **decide** the run:

- `run_vitis: false` → `software_equivalence`, `trace_consistency`. The three Vitis phases
  report `skipped` and that is not a failure.
- `run_vitis: true` → all five.

Status vocabulary, used exactly as emitted and never promoted:

| Status | Meaning |
|---|---|
| `pass` | Ran, and the evidence supports it. |
| `fail` | Ran, and the evidence contradicts it. |
| `blocked` | Could not run because an earlier phase failed. |
| `skipped` | Not requested, or an optional tool is absent. **Never** the same as `pass`. |

An absent optional tool must never fail a build — `skipped` with a remedy is the correct
outcome, and `c2hlsc-agent doctor --install` is the remedy.

---

## 6. Commands

```powershell
python -m c2hlsc_agent convert --input input.c --top NAME --config config.yaml --out build\proj
python -m c2hlsc_agent doctor --tier core          # what each tier needs; --install to fix
python -m c2hlsc_agent refine  --project build\proj --target 100 --verbose
python -m c2hlsc_agent status  --project build\proj
python -m c2hlsc_agent components                  # the eight agents and their gates
```

`convert --help` lists the full override surface; every CLI flag beats the config file.
