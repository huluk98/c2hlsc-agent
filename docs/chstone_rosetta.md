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

Each Rosetta agent-rung row carries the same thing as `xilinx_followup_cmd`, with the app's
generated `--config` (which supplies `-DSW` and the two include paths) attached.

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
| deterministic converter, **with repair** | **6/12** (`adpcm`, `aes`, `dfadd`, `dfdiv`, `dfmul`, `dfsin`) |
| LLM generator, **with repair** | **8/12** (the six above, plus `gsm`, `sha`) |
| Vitis CSim / CSynth / CoSim | not attempted (no `vitis_hls`) |

> **These numbers replace an earlier 0/12 and 6/12, and the difference is almost entirely a
> harness fix, not a better agent.** Under the previous staging the equivalence testbench
> `#include`d the golden reference `input.c` into itself; CHStone is C89 and the testbench is
> C++, so for most benchmarks that build failed *before the candidate was ever exercised*.
> Those rows were zeros recorded for a defect in the harness — measurements that never
> happened — and reporting the change as "+6 passes" would badly understate what moved.
> The honest framing is reachability:
>
> | staging | repair rounds | benchmarks that reached the oracle at all | passed |
> | --- | :-: | :-: | :-: |
> | `legacy_inline` | 0 | 8/12 | 0 |
> | `legacy_inline` | 1 | **3/12** | 0 |
> | `golden_c_tu` (current) | 0 | 12/12 | 0 |
> | `golden_c_tu` (current) | 1 | **12/12** | 6 |
>
> Note the second row. Under the old staging, **turning repair on made things worse**:
> enabling a repair round pulled the original C into the candidate as well, so the link
> failed with `multiple definition of 'main_result'` and reachability collapsed from 8 to 3.
> The current staging compiles the golden reference **as C, in its own translation unit**,
> which removes both the C-as-C++ failure and the symbol collision.
>
> Measured with `scripts/run_chstone.py --legacy-staging` if you want to reproduce the old
> behaviour; the arms are in `runs/abl_det_legacy_r{0,1,2,3}` and `runs/abl_det_staged_r{0,1,2,3}`.

**Run the repair loop.** `--auto-repair --max-iterations N` is independent of `--use-llm`:
`hlsc_repair_agent` applies deterministic mechanical repairs (missing includes,
helper-source inclusion, `restrict` compatibility, interface-pragma stripping) with no
model at all, and escalates to an LLM patch only when one is configured. An earlier version
of this harness gated `--auto-repair` behind `--use-llm`, so the first deterministic result
published here measured single-shot generation and called it the agent. Both numbers above
now include repair.

The LLM generator is the headline: **8/12**, against 6/12 for the deterministic converter
under identical repair and staging settings. On `dfmul` it emitted a self-contained 566-line
translation — the SoftFloat call graph inlined into a namespace, `printf` guarded behind
`#ifndef __SYNTHESIS__`, the 256-entry `countLeadingZeros32` table replaced by a branchless
cascade — and passed with `all 100 tests passed`. `gsm` (714 lines) and `aes` (659 lines)
likewise pass. Runs take 8–20 minutes per benchmark.

Now that all 12 reach the oracle, the generator gap is directly readable: the LLM passes the
same six the deterministic converter does, **plus `gsm` and `sha`**. That +2 is the only part
of the CHStone result that is an agent-quality difference; everything else the staging fix
moved was a measurement that had not previously been taken.

The four remaining failures split into two families, and neither is wrong logic:

| family | n | benchmarks | whose defect |
| --- | :-: | --- | --- |
| `candidate_includes_original_c` | 2 | `blowfish`, `motion` | the generator re-includes the original C, reintroducing K&R definitions `g++` rejects |
| `generated_hlsc_does_not_compile` | 2 | `jpeg`, `mips` | the generated HLS-C itself |

`jpeg` is the largest benchmark in the suite and `mips` the one whose top closes over the
most file-scope state; both are genuine generation failures, not harness artifacts. For the
deterministic converter the same two families cover its six failures, with `gsm` and `sha`
additionally failing to compile.

### Where the deterministic path actually stops

Without repair, all 12 fail identically: the converter lifts the **top function body** into
`src/hls_top.cpp` without the file-scope state a CHStone `main` closes over — test-vector
arrays, `#define`s, helper functions, globals — so the generated HLS-C references
undeclared symbols (`test_compressed`, `IN_END`, `float64`, `N`, `x1`).

With repair on, that is no longer the whole story. `hlsc_repair_agent` applies exactly the
right mechanical fix — *"include original source with renamed top to supply helper
definitions"* — and the wall moves. Under the **old** `legacy_inline` staging that fix
immediately collided with the harness, and the 0/12 decomposed into three causes:

| family (legacy staging) | n | benchmarks | whose defect |
| --- | :-: | --- | --- |
| `golden_candidate_symbol_collision` | 5 | `aes`, `dfadd`, `dfdiv`, `dfmul`, `dfsin` | the equivalence harness |
| `original_c_not_valid_cpp` | 4 | `adpcm`, `blowfish`, `jpeg`, `motion` | the C-vs-C++ flow |
| `generated_hlsc_does_not_compile` | 3 | `gsm`, `mips`, `sha` | the converter |

Only the last three were the converter's own reach. The other nine were properties of the
flow, and both causes have since been fixed at the source by the `golden_c_tu` staging:

- **Symbol collision.** The repair included the original source to supply helpers, so the
  golden TU and the HLS-C TU both defined the original's file-scope globals
  (`float_rounding_mode`, `float_exception_flags`, `sha_info_data`, `main_result`) and the
  link failed with `multiple definition of`. Compiling the golden reference as a **separate
  C translation unit** removes the collision outright.
- **C compiled as C++.** CHStone is C; the equivalence testbench is C++. Narrowing
  conversions, K&R parameter declarations and tentative-definition redeclarations are legal
  C that `g++` rejects. Compiling the golden **as C** removes this for the reference side.

With both fixed, all 12 benchmarks reach the oracle and the deterministic converter scores
6/12. What remains — `blowfish`/`motion` re-including the original C, `jpeg`/`mips` failing
to compile — is on the generated side, which is where a converter result belongs.

The prediction this document made before the fix ("fixing the symbol collision alone would
unblock 5 deterministic benchmarks and 2 LLM ones") is worth scoring honestly: the
deterministic converter gained **6** (the predicted 5, plus `adpcm`, which was blocked by
the C-as-C++ cause rather than the collision), and the LLM path gained **2** (`mips` was
predicted and did *not* unblock; `sha` and `adpcm` did). The direction was right, the
per-benchmark attribution was not.

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

The deterministic converter's 0/12 survived *that* fix unchanged, and its failures were
provably on the generated side (`hls_top.cpp` referencing `float64`, `N`, `x1`). `adpcm` is
single-file, so it had no siblings to stage and failed that way all along — which is why
the original result looked coherent enough to trust. Calibration is what catches this: if
the reference cannot build, no candidate result from that run means anything.

**This happened twice.** The sibling-staging bug above is one instance; the
`legacy_inline` staging described earlier in this section is a second, larger one, and it
survived the first fix because calibration only proves the *reference* builds — it says
nothing about whether the reference and the candidate can be linked into one binary, which
is where the second bug lived. The deterministic converter's 0/12 was, in the end, mostly
that second bug: it reads 6/12 once the golden reference is compiled as its own C
translation unit. Two lessons, both cheap: a rung that scores zero for every benchmark is
far more likely to be a harness defect than a uniformly incapable generator, and
"reached the oracle at all" deserves to be a reported metric in its own right — it is now
`reachable` in `report.json`.

---

## Rosetta

Six FPGA applications built for Xilinx SDAccel/SDSoC. The Xilinx-only headers sit behind
`#ifdef OCL` / `#ifdef SDSOC`, so the `src/sw` kernel plus `src/host` builds and runs with
plain `g++ -DSW`. The harness reads each app's `Makefile` for `HOST_SRC_CPP` and
`SW_KERNEL_SRC` rather than guessing the source list, and for `KERNEL_NAME` — the software
top is `<KERNEL_NAME>_sw` — rather than guessing the kernel entry point. It resolves for
all five buildable apps: `rendering_sw`, `DigitRec_sw`, `face_detect_sw`, `optical_flow_sw`,
`SgdLR_sw`.

### Commands

```bash
git clone https://github.com/cornell-zhang/rosetta.git ~/rosetta

# Calibration rung: build and run each app's own software path against its golden output.
python3 scripts/run_rosetta.py --benchmark ~/rosetta --sw-baseline \
  --out-dir runs/rosetta_sw --workers 3 --timeout 1800

# Agent rung: convert each src/sw kernel to HLS-C and run host software equivalence.
python3 scripts/run_rosetta.py --benchmark ~/rosetta --agent \
  --auto-repair --max-iterations 2 --out-dir runs/rosetta_agent --workers 3 --timeout 1800

# The repo's LLM generator instead of the deterministic converter.
python3 scripts/run_rosetta.py --benchmark ~/rosetta --agent \
  --use-llm --llm-backend claude-cli --llm-model opus --auto-repair --max-iterations 2 \
  --out-dir runs/rosetta_llm --workers 3 --timeout 2400
```

`--use-llm` and `--auto-repair` imply `--agent`, so an agent-only flag can never be
silently ignored by the software-baseline default.

### The software baseline's oracle, and where it runs out

Three apps ship `outputs_golden.txt`, and their host code writes `outputs.txt`, so a run
can be compared against a golden result. **The other apps ship no golden output at all**,
and the harness reports them as `no_trustworthy_oracle` rather than scoring a zero exit
code as a pass — an exit code proves the program did not crash, not that it computed
anything correct.

This matters because it was nearly a silent failure. `digit-recognition` prints
`Checking results:` to stdout with no accuracy figure and exits 0; a naive harness scores
that green. The accuracy actually goes to `outputs.txt` via `check_results()`, and reading
it is the difference between a real oracle and a vacuous one.

### Measured — software baseline (calibration)

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
nothing to do with the code under test. The agent rung passes the same four force-includes,
plus `-DSW -I src/sw -I src/host`, through `--config` so the conversion compiles the kernel
the same way the suite does — `-DSW` is what selects the plain-C++ typedefs over the
`ap_int`/`ap_fixed` ones, and the two `-I` paths let the copied `input.c` resolve its own
quoted includes (`"sgd_sw.h"` and, through it, `"../host/typedefs.h"`) from inside the
generated project. No Rosetta source is copied, staged or rewritten.

### Measured — agent rung (`src/sw` kernel → HLS-C → host equivalence)

The same ladder rung as CHStone: convert the kernel and compare the generated HLS-C against
the original on shared stimulus. **No `vitis_hls` and no Xilinx SDx**, so synthesis,
SDAccel and SDSoC were *not attempted*; every row carries `xilinx_available: false` and a
`rungs_not_attempted` list, exactly like the software-baseline rows.

| rung | result |
| --- | --- |
| software baseline (calibration) | 5/5 build and run; **1/3** among apps with a golden output |
| deterministic converter, **with repair** | **0/5** |
| LLM generator, **with repair** | **0/5** |
| HLS synthesis / SDAccel / SDSoC | not attempted (no Xilinx tooling) |

**Quote the agent rung as 0/5.** Not 0/6 — `BNN` is a vendored copy of another repo with
its own build and is not attempted. Unlike the software baseline there is no
"no trustworthy oracle" exclusion here: the original `src/sw` kernel *is* the oracle for
every app, so the denominator is all five buildable apps.

Nothing reached host equivalence on either path, and none of it is a judgement about the
Rosetta code. Every app stops inside this repo's own conversion flow, at one of four walls
the compiler names outright.

#### Where the deterministic path stops

| wall | apps | whose defect |
| --- | :-: | --- |
| `top_signature_misparsed` | 4 | the analyzer |
| `multidim_array_arg_unsupported` | 1 (3 by shape) | the testbench generator |
| `struct_arg_stimulus_unsupported` | 2 | the testbench generator |
| `generated_header_missing_app_types` | 1 | the generated project |

**The analyzer captures a comment as the return type.** `analyze._extract_function` runs
its extraction regex over the *raw* source, so a `//` comment on the line above the
definition is swallowed into the captured return type. Four of the five Rosetta tops have
exactly such a comment:

| app | comment above the top | emitted declaration |
| --- | --- | --- |
| `spam-filter` | `// top-level function` | `level function void SgdLR_sw(...)` |
| `face-detection` | `// top-level function` | `level function void face_detect_sw(...)` |
| `optical-flow` | `// top-level sw function` | `level sw function void optical_flow_sw(...)` |
| `digit-recognition` | `// sw top function` | `sw top function void DigitRec_sw(...)` |

g++ then rejects the generated header and everything that includes it:

```
src/hls_top.hpp:6:1: error: 'level' does not name a type
tb/testbench.cpp:116:5: error: 'level' was not declared in this scope
  116 |     level function void ref_ret = SgdLR_sw_ref(ref_data, ref_label, ref_theta);
```

`3d-rendering` is the only app that escapes it, and only by luck: its top is preceded by a
`/* ... */` banner, and the regex's character class cannot cross the `*` and `/`. CHStone
never surfaced this because every CHStone top is K&R `int\nmain ()` with no comment line
between. The harness detects it by re-running the *identical* regex on comment-stripped
source and comparing — the converter itself is untouched.

**The testbench generator cannot express a 2-D array parameter.** It declares one flat 1-D
array per pointer-like argument, so a `[H][W]` parameter never type-checks:

```
tb/testbench.cpp:109:40: error: cannot convert 'bit8*' {aka 'unsigned char*'}
                                to 'bit8 (*)[256]' {aka 'unsigned char (*)[256]'}
```

Measured on `3d-rendering`. `face-detection` (`Data[IMAGE_HEIGHT][IMAGE_WIDTH]`) and
`optical-flow` (six `[MAX_HEIGHT][MAX_WIDTH]` parameters) have the same shape but abort on
the misparse first, so the compiler never gets to say it. Read the counts in the report as a
floor for that reason.

**Struct-typed arrays have no stimulus.** `patterned_value<T>` starts with
`static_cast<T>(0)`, which no struct accepts:

```
tb/testbench.cpp:53:29: error: no matching function for call to 'Triangle_3D::Triangle_3D(int)'
tb/testbench.cpp:56:29: error: no matching function for call to 'velocity_t::velocity_t(int)'
```

**The generated header declares the top in types it never includes.** `src/hls_top.hpp`
carries only `#include <stdint.h>`, so the app's own typedefs are undeclared:

```
src/hls_top.hpp:6:6:  error: variable or field 'rendering_sw' declared void
src/hls_top.hpp:6:19: error: 'Triangle_3D' was not declared in this scope
src/hls_top.hpp:6:57: error: 'bit8' was not declared in this scope
```

This is the same family as CHStone's "generated HLS-C references undeclared symbols". It is
also the one wall the LLM path repeatedly tries to patch around, with partial success (see
below).

`hlsc_repair_agent` has no mechanical repair for any of the four: all five apps record a
single `no_change` iteration. Its repertoire is missing standard includes, `restrict`
compatibility, helper-source inclusion and interface-pragma stripping — none of which is
the defect here. (It is classified as `testbench_or_c_semantics` on three apps and, because
of the false-positive VLA diagnostic below, as `static_source_rejected` on
`digit-recognition` and `face-detection`; the outcome is the same either way.)

#### What the LLM path adds, and why it cannot close the gap

The LLM generator (`--llm-backend claude-cli --llm-model opus`, `--auto-repair
--max-iterations 2`) is also **0/5**, at the same four walls, 24 minutes for the sweep at
three workers — 7–13 minutes per app. It is worth reading anyway, because the failure is
not the model's.

It produced substantial, self-contained translations for all five apps — 276 to 816 lines,
helpers given internal linkage to sidestep the golden-vs-candidate link collision that cost
CHStone five benchmarks, arithmetic order preserved for bit-exactness — and on three apps it
*diagnosed the harness's own defects from the artifact and worked around them*. On
`spam-filter` the first attempt opened with:

```c
// The generated hls_top.hpp declares the top function with a stray
// "level function" prefix carried over from the signature metadata ...
#define level
#define function
#include "hls_top.hpp"
#undef function
#undef level
```

Nothing in the prompt mentions the misparse. The workaround is correct, and it exposes the
next wall — `hls_top.hpp` reduces to a clean `void SgdLR_sw(DataType ..., LabelType ...,
FeatureType ...)` whose types it still never includes:

```
src/hls_top.hpp:6:30: error: 'DataType' was not declared in this scope
```

The repair iteration then fixed *that* — by including `"sgd_sw.h"` ahead of `hls_top.hpp`,
guarded with `__has_include` — and in doing so dropped the macro workaround, so the run
landed back on the misparse. `3d-rendering` and `optical-flow` show the same shape: the
model restates the app's typedefs itself but places them after `#include "hls_top.hpp"`, so
the header still fails first. The repair loop rewrites one file against one failure at a
time, and these apps need two unrelated fixes to hold simultaneously.

None of that would have been enough regardless. `tb/testbench.cpp` carries the same
`level function void` text and the same flat-1-D-array and `static_cast<T>(0)` code, and the
repair loop is deliberately forbidden to touch it — only `src/hls_top.cpp` is ever
rewritten, so the golden oracle and the testbench stay outside the model's reach. That is
the right design; it is also why no model can rescue a defect that lives in the generated
testbench. On `3d-rendering` — the one app that escapes the misparse entirely — two of the
three remaining walls are in `tb/testbench.cpp`, so even a perfect `src/hls_top.cpp` could
not have compiled.

#### The oracle here is weaker than "host equivalence" sounds

Worth stating before anyone reads a future Rosetta pass as a strong result. Every Rosetta
top takes arrays whose bounds are *named constants* (`NUM_FEATURES * NUM_TRAINING`,
`MAX_HEIGHT`, `NUM_3D_TRI`), and the analyzer takes a test length only from a literal digit
dimension. So **all 20 array arguments across the five apps fall back to the default test
length 16**, and the analyzer says so, once per argument:

```
[warning] missing-pointer-bound: argument 'data' has no configured bound; using conservative test length 16
```

The kernels then index far past that — `SgdLR_sw` walks `data[i * 1024 + j]` for 4500
samples, 4.6 M elements against a 16-element buffer. Even with all four walls fixed, a pass
from this testbench would be reading out of bounds and would not be evidence of anything.
Configuring the real bounds is not a drop-in fix either: `optical-flow`'s six 436×1024
frames are ~18 MB per copy, and the testbench declares two copies of every argument as
locals. Sound Rosetta equivalence needs the testbench generator to size arrays from the
declared bounds and heap-allocate them, which is a change to the repo, not to the harness.

#### `--strict-diagnostics`, and a false positive worth fixing

Running with `--strict-diagnostics` instead of the default `--keep-going` stops
`digit-recognition` and `face-detection` one rung earlier, at `analyzed` /
`static_source_rejected`. The diagnostic that triggers it is wrong: the analyzer's
variable-length-array check accepts only literal digit bounds, so `int dists[K_CONST]`
(a `#define 3`) and `unsigned char Data[IMAGE_HEIGHT][...]` (a `const int 240`) are reported
as variable-length arrays. Both are compile-time constants. Three errors across the suite,
all false.

---

## What this does and does not show

**Does show.** CHStone's 12 programs all self-check clean natively, giving a solid
calibration rung. The repo's deterministic converter cannot take a whole-program,
globals-closing top through to compiling HLS-C, and the reason is identical across all 12.
The LLM generator can: it produced self-contained, documented translations that pass host
equivalence on 6 of the 12 benchmarks — 6 of the 7 where the flow itself works — with the
rest blocked by a fixable golden-vs-candidate symbol collision or by C that `g++` rejects,
rather than by wrong logic.
Rosetta's software path builds and runs for all 5 buildable apps, and one of the three
judgeable apps reproduces its golden output exactly.

On Rosetta's **agent** rung the result is 0/5 on both paths, and it is a measurement of this
repo rather than of Rosetta: four of the five apps die on a comment absorbed into the parsed
return type, and the multi-dimensional-array parameters, struct-typed arrays and app
typedefs that the rest need are things the generated testbench and header cannot currently
express. Those are four named, individually fixable defects with the compiler's own words
attached — the same kind of finding as CHStone's symbol collision, and arguably more
actionable, since Rosetta's kernels have real arguments and are the fairer target the
CHStone write-up asks for.

**Does not show.** Nothing about synthesizability, timing, area, or C/RTL equivalence for
either suite — the Vitis and Xilinx rungs did not run. Nor does it show that the converter
would fail on these programs' *inner* kernels (`adpcm_main`, `sha_stream`, and so on);
this run targeted the suite-declared `chstone_main` top, and the inner kernels have real
arguments and are a fairer target for the converter. That is the obvious next experiment.

Nor does Rosetta's 0/5 show that the generator produces *wrong* HLS-C. Every app stopped at
compile time, so no Rosetta kernel's generated translation was ever executed against its
original — and even if one had been, the 16-element default stimulus above means the
comparison would not have been sound. Whether this repo can translate a Rosetta kernel
correctly is still an open question; this run only establishes what has to be fixed before
it can be asked.
