# CHStone and Rosetta — what ran, what did not, and why

Two C/C++ HLS benchmark suites driven through this repo's conversion agent, alongside the
RTLLM v2.0 work in [`rtllm_v2_session_handoff.md`](rtllm_v2_session_handoff.md).

Read the coverage statement first. It governs how every number below may be quoted.

---

## Coverage: only rung 1 of the ladder ran

This repo's verifier ladder is:

```
host software equivalence  ->  Vitis CSim  ->  Vitis CSynth  ->  C/RTL CoSim
        ^ this ran                  ^ none of these ran
```

**Neither `vitis_hls` nor Xilinx SDx was available in the environment these results come
from.** CHStone's own `Makefile` drives `vitis_hls hls.tcl`, and Rosetta targets
SDAccel/SDSoC — so for both suites the synthesis rungs were *not attempted*, and nothing
here is stubbed, estimated or simulated to stand in for them. Every result row carries
`vitis_available: false` / `xilinx_available: false` and a `rungs_not_attempted` list so a
host-equivalence pass can never be misread as a synthesized design.

To finish the ladder on a machine that has Vitis, each CHStone row also carries a ready
`vitis_followup_cmd`; the pattern is:

```bash
python3 -m c2hlsc_agent.cli convert \
  --input <staged_top>.c --top chstone_main --out <project> \
  --vitis-ssh USER@VITIS_HOST --keep-going
```

---

## CHStone

12 self-checking C programs. Each benchmark's own `hls.tcl` compiles one top file with
`-Dmain=chstone_main` and sets `chstone_main` as the HLS top — so the kernel is the whole
benchmark's `main`: zero arguments, returns 0 on success. The harness reads `hls.tcl` for
the top file rather than guessing, because it is not always `<dir>/<dir>.c`
(`jpeg/main.c`, `motion/mpeg2.c`, `sha/sha_driver.c` all differ), and it renames the K&R
`main ()` definition to `chstone_main` — the identifier only, since every CHStone top puts
its `int` on the preceding line and prepending another yields `int int`.

### Commands

```bash
git clone https://github.com/ferrandi/CHStone.git ~/CHStone

# Calibration rung: compile and run CHStone's own C. rc == 0 means its self-check passed.
python3 scripts/run_chstone.py --benchmark ~/CHStone --native-baseline \
  --out-dir runs/chstone_native --workers 4

# Agent rung: convert to HLS-C and run host software equivalence.
python3 scripts/run_chstone.py --benchmark ~/CHStone \
  --out-dir runs/chstone_agent --workers 4

# The repo's LLM ("strong") generator instead of the deterministic converter.
python3 scripts/run_chstone.py --benchmark ~/CHStone --out-dir runs/chstone_llm \
  --use-llm --llm-backend claude-cli --llm-model opus --auto-repair --max-iterations 2 \
  --workers 4 --timeout 2400
```

`--strict-diagnostics` stops at static analysis instead of pushing past non-synthesizable
constructs; the default (`--keep-going`) matches CHStone's own Vitis flow, which tolerates
`printf` in the top.

### Measured

| rung | result |
| --- | --- |
| native self-check (calibration) | **12/12 pass** |
| deterministic converter, **with repair** | **0/12** |
| LLM generator, **with repair** | **6/12** (`aes`, `dfadd`, `dfdiv`, `dfmul`, `dfsin`, `gsm`) |
| Vitis CSim / CSynth / CoSim | not attempted (no `vitis_hls`) |

**Run the repair loop.** `--auto-repair --max-iterations N` is independent of `--use-llm`:
`hlsc_repair_agent` applies deterministic mechanical repairs (missing includes,
helper-source inclusion, `restrict` compatibility, interface-pragma stripping) with no
model at all, and escalates to an LLM patch only when one is configured. An earlier version
of this harness gated `--auto-repair` behind `--use-llm`, so the first deterministic result
published here measured single-shot generation and called it the agent. Both numbers above
now include repair.

The LLM generator is the headline: **6/12**, against 0/12 for the deterministic converter
under identical repair settings. On `dfmul` it emitted a self-contained 566-line translation
— the SoftFloat call graph inlined into a namespace, `printf` guarded behind
`#ifndef __SYNTHESIS__`, the 256-entry `countLeadingZeros32` table replaced by a branchless
cascade — and passed with `all 100 tests passed`. `gsm` (714 lines) and `aes` (659 lines)
likewise pass, each after 2 repair iterations. Runs take 8–20 minutes per benchmark.

Read the 6/12 against what the flow can actually score. Four benchmarks
(`adpcm`, `blowfish`, `jpeg`, `motion`) fail because CHStone's C is not valid C++ and never
reach the model's output, and one (`mips`) fails on the golden-vs-candidate symbol
collision. **On the 7 benchmarks where the flow itself works, the LLM generator passes 6**
— `sha` is the single genuine generation failure.

The two LLM failures are **not** wrong logic. Both are link-time symbol collisions: the
golden reference and the generated HLS-C are compiled into one binary, and both define the
same file-scope globals (`sha_info_count_lo`, `sha_info_data`, `main_result`), so the link
fails with `multiple definition of ...`. The repo's equivalence testbench macro-renames the
golden *function* but not its globals. `dfadd` and `dfmul` passed precisely because the
model chose to wrap its output in a namespace; nothing instructs it to. Two clean fixes:
tell the generator to namespace unconditionally, or compile the golden as a separate
translation unit with renamed symbols. Until one lands, treat `mips`/`sha` as a harness
limitation, not a model result.

### Where the deterministic path actually stops

Without repair, all 12 fail identically: the converter lifts the **top function body** into
`src/hls_top.cpp` without the file-scope state a CHStone `main` closes over — test-vector
arrays, `#define`s, helper functions, globals — so the generated HLS-C references
undeclared symbols (`test_compressed`, `IN_END`, `float64`, `N`, `x1`).

With repair on, that is no longer the whole story. `hlsc_repair_agent` applies exactly the
right mechanical fix — *"include original source with renamed top to supply helper
definitions"* — and the wall moves. The 0/12 is now **three distinct causes**:

| family | n | benchmarks | whose defect |
| --- | :-: | --- | --- |
| `golden_candidate_symbol_collision` | 5 | `aes`, `dfadd`, `dfdiv`, `dfmul`, `dfsin` | the equivalence harness |
| `original_c_not_valid_cpp` | 4 | `adpcm`, `blowfish`, `jpeg`, `motion` | the C-vs-C++ flow |
| `generated_hlsc_does_not_compile` | 3 | `gsm`, `mips`, `sha` | the converter |

Note which five the symbol collision blocks: `aes`, `dfadd`, `dfdiv`, `dfmul`, `dfsin` are
exactly the five the LLM generator goes on to pass. It sidesteps the collision by putting
its translation in a namespace — something nothing in the prompt asks it to do.

Only the last three are the converter's reach. The other nine are properties of the flow:

- **Symbol collision.** Once the repair includes the original source to supply helpers, the
  golden reference TU and the HLS-C TU both define the original's file-scope globals
  (`float_rounding_mode`, `float_exception_flags`, `sha_info_data`, `main_result`), and the
  link fails with `multiple definition of`. This is the *same* defect that blocks the LLM
  path on `mips`/`sha`. The equivalence testbench macro-renames the golden *function* but
  not its globals.
- **C compiled as C++.** CHStone is C; the equivalence testbench is C++. Narrowing
  conversions, K&R parameter declarations and tentative-definition redeclarations are legal
  C that `g++` rejects outright.

Both are fixable in the repo rather than in the benchmark, and fixing the symbol collision
alone would unblock 5 deterministic benchmarks and 2 LLM ones. That is the highest-value
next change this exercise surfaced.

Running with `--strict-diagnostics` instead stops one rung earlier, at
`static_source_rejected` with a `file-io` diagnostic (console I/O in the top).

A second, quieter limitation worth knowing before reading too much into any CHStone
"equivalence pass": `chstone_main` takes **no arguments**, so host equivalence reduces to
calling both versions once and comparing a single return value. There are no stimuli to
vary. It is a genuine check, but a far weaker one than the argument-driven equivalence the
repo performs on ordinary kernels.

### A harness bug that masked a real result

Worth recording, because it is the exact failure mode this documentation exists to prevent.
CHStone tops `#include` their sibling sources (`softfloat.c`, `softfloat-macros`,
`softfloat-specialize`), and the converter copies only `--input` into the project as
`input.c`. The **golden reference** therefore could not compile, and the first version of
this harness attributed that to the generated HLS-C — reporting the LLM path as 0/4 when
`dfmul` and `dfadd` in fact passed. The harness now stages sibling files next to `input.c`,
re-runs host equivalence, and treats the equivalence log as authoritative over
`conversion_report.json`'s phase field, which records the state from before staging.

The deterministic converter's 0/12 survived the fix unchanged, and its failures are
provably on the generated side (`hls_top.cpp` referencing `float64`, `N`, `x1`). `adpcm` is
single-file, so it had no siblings to stage and failed that way all along — which is why
the original result looked coherent enough to trust. Calibration is what catches this: if
the reference cannot build, no candidate result from that run means anything.

---

## Rosetta

Six FPGA applications built for Xilinx SDAccel/SDSoC. The Xilinx-only headers sit behind
`#ifdef OCL` / `#ifdef SDSOC`, so the `src/sw` kernel plus `src/host` builds and runs with
plain `g++ -DSW`. The harness reads each app's `Makefile` for `HOST_SRC_CPP` and
`SW_KERNEL_SRC` rather than guessing the source list.

### Commands

```bash
git clone https://github.com/cornell-zhang/rosetta.git ~/rosetta

python3 scripts/run_rosetta.py --benchmark ~/rosetta --sw-baseline \
  --out-dir runs/rosetta_sw --workers 3 --timeout 1800
```

### The oracle, and where it runs out

Three apps ship `outputs_golden.txt`, and their host code writes `outputs.txt`, so a run
can be compared against a golden result. **The other apps ship no golden output at all**,
and the harness reports them as `no_trustworthy_oracle` rather than scoring a zero exit
code as a pass — an exit code proves the program did not crash, not that it computed
anything correct.

This matters because it was nearly a silent failure. `digit-recognition` prints
`Checking results:` to stdout with no accuracy figure and exits 0; a naive harness scores
that green. The accuracy actually goes to `outputs.txt` via `check_results()`, and reading
it is the difference between a real oracle and a vacuous one.

### Measured

| app | built | ran | oracle | verdict |
| --- | :-: | :-: | --- | :-: |
| `face-detection` | Y | Y | golden file | **PASS** |
| `digit-recognition` | Y | Y | golden file | FAIL — 1870/2000 vs golden 1878/2000 |
| `3d-rendering` | Y | Y | golden file | FAIL — pixel differences on 2 of 257 rows |
| `spam-filter` | Y | Y | *none shipped* | excluded |
| `optical-flow` | Y | Y | *none shipped* | excluded |
| `BNN` | — | — | vendored copy of another repo, own build | not attempted |

**5/5 build and run. 1/3 pass among apps with a golden output.** Quote it as 1/3, not 1/5
and not 1/6 — the denominator is "apps this suite gives you a way to judge".

Two findings about the suite itself, both measured rather than assumed:

- **`digit-recognition`'s golden does not match its own software implementation.** The sw
  path deterministically yields 1870/2000 at both `-O0` and `-O2`, while
  `outputs_golden.txt` says 1878/2000, differing on 44 output lines. Since optimization
  level does not move it, this is not a build artifact — the golden was produced by a
  different implementation (most likely the fixed-point SDSoC/hardware version, whose
  `ap_uint<256>` typedefs and tie-breaking differ from the sw kernel).
- **`3d-rendering` differs by a handful of pixels** on 2 of 257 rows, with the rest byte
  identical. Same class of issue.

One environment fix was required and is worth stating plainly: three apps
(`face-detection`, `spam-filter`, `optical-flow`) do not compile on a modern GCC because
they rely on transitive `#include`s that newer libstdc++ no longer provides. The harness
force-includes `cstdio`, `iostream`, `cstring` and `cstdlib`. That adds no symbol the
sources do not already use, and without it those three read as build failures that have
nothing to do with the code under test.

---

## What this does and does not show

**Does show.** CHStone's 12 programs all self-check clean natively, giving a solid
calibration rung. The repo's deterministic converter cannot take a whole-program,
globals-closing top through to compiling HLS-C, and the reason is identical across all 12.
The LLM generator can: it produced self-contained, documented translations that pass host
equivalence on 2 of the 4 benchmarks tried, with the other 2 blocked by a fixable
golden-vs-candidate symbol collision rather than by wrong logic.
Rosetta's software path builds and runs for all 5 buildable apps, and one of the three
judgeable apps reproduces its golden output exactly.

**Does not show.** Nothing about synthesizability, timing, area, or C/RTL equivalence for
either suite — the Vitis and Xilinx rungs did not run. Nor does it show that the converter
would fail on these programs' *inner* kernels (`adpcm_main`, `sha_stream`, and so on);
this run targeted the suite-declared `chstone_main` top, and the inner kernels have real
arguments and are a fairer target for the converter. That is the obvious next experiment.
