# Pipeline defects, causes, and fixes — CHStone and Rosetta through the full Vitis ladder

Working record of every defect found while taking this repo's C → HLS-C conversion agent
from "host software equivalence only" to the complete four-rung ladder on a machine that
has Vitis HLS.

Each entry gives the **verbatim error**, the **input file that triggers it**, the **cause**,
the **fix** with a file reference, and the **evidence** that the fix works. Nothing here is
inferred: every error string was produced by a real run on this machine, and every claim of
"fixed" is backed by a re-run.

Read [Coverage and honesty rules](#coverage-and-honesty-rules) before quoting any number.

---

## Contents

- [Environment](#environment)
- [How to reproduce](#how-to-reproduce)
- [Summary of results](#summary-of-results)
- [Part 1 — Host equivalence: the translation unit was not self-contained](#part-1--host-equivalence-the-translation-unit-was-not-self-contained)
- [Part 2 — Host equivalence: the argument model was a toy](#part-2--host-equivalence-the-argument-model-was-a-toy)
- [Part 3 — The oracle itself was vacuous](#part-3--the-oracle-itself-was-vacuous)
- [Part 4 — The Vitis ladder](#part-4--the-vitis-ladder)
- [Known limitations and open items](#known-limitations-and-open-items)
- [Coverage and honesty rules](#coverage-and-honesty-rules)
- [Generated artifacts reference](#generated-artifacts-reference)

---

## Environment

| Item | Value |
| --- | --- |
| Host | Windows 10 Pro 10.0.19045 |
| HLS tool | **Vitis HLS 2024.2** (64-bit), SW Build 5238294, `D:\Xilinx\Vitis_HLS\2024.2\bin\vitis_hls.bat` |
| RTL simulator | Vivado 2024.2 `xsim` (bundled; used by `cosim_design`) |
| Target part | `xczu7ev-ffvc1156-2-e`, 10 ns clock |
| Host compiler (Windows) | MSYS2 UCRT64 gcc/g++ 16.2.0, `C:\msys64\ucrt64\bin` |
| Host compiler (WSL) | Ubuntu 26.04, gcc/g++ 15.2.0 |
| Python | 3.12.10 (Windows venv `.venv-win`), 3.14.4 (WSL venv `~/.venv-c2hlsc`) |
| C frontend | `libclang` 18.1.1 (pip wheel; ships `libclang.dll`/`.so`) |
| CHStone | `github.com/ferrandi/CHStone`, shallow clone |
| Rosetta | `github.com/cornell-zhang/rosetta`, shallow clone |

`C2HLSC_VITIS_BIN` is set to the Vitis launcher. The repo reads it (see
[V1](#v1--the-vitis-launcher-cannot-be-started-on-windows)).

> **libclang is an optional dependency.** Install it with
> `pip install 'c2hlsc-agent[closure]'`. Without it the converter still runs, but the
> generated translation unit carries no file-scope context and the conversion report says
> so explicitly — it does not silently emit an empty closure.

---

## How to reproduce

```bash
# benchmarks
git clone --depth 1 https://github.com/ferrandi/CHStone.git   ~/CHStone
git clone --depth 1 https://github.com/cornell-zhang/rosetta.git ~/rosetta

# environment
python -m venv .venv-win
./.venv-win/Scripts/pip install libclang PyYAML pytest
export C2HLSC_VITIS_BIN="D:\Xilinx\Vitis_HLS\2024.2\bin\vitis_hls.bat"

# CHStone: host equivalence only
python scripts/run_chstone.py --benchmark ~/CHStone \
  --out-dir runs/chstone --auto-repair --max-iterations 2 --workers 4

# CHStone: full ladder, with repair driven off the failing Vitis phase
python scripts/run_chstone.py --benchmark ~/CHStone \
  --out-dir runs/chstone_vitis --auto-repair --max-iterations 2 \
  --run-vitis --workers 4 --timeout 3600

# Rosetta
python scripts/run_rosetta.py --benchmark ~/rosetta --agent \
  --auto-repair --max-iterations 2 --out-dir runs/rosetta --workers 3
python scripts/run_rosetta.py --benchmark ~/rosetta --agent \
  --auto-repair --max-iterations 2 --run-vitis --out-dir runs/rosetta_vitis --workers 3
```

`--run-vitis` is new (see [Part 4](#part-4--the-vitis-ladder)). Both runners refuse to fake
a Vitis rung: without `vitis_hls` they report the rungs as not attempted.

---

## Summary of results

Deterministic converter (no LLM), `--auto-repair --max-iterations 2` throughout.

### Host software equivalence (rung 1)

| suite | before this work | after |
| --- | --- | --- |
| CHStone | 6/12 (docs recorded 0/12 before the `golden_c_tu` staging) | **11/12** |
| Rosetta | 0/5 | **5/5** |

Every pass is checked against a mutant: the candidate's result is perturbed and the test
must go red. CHStone's runner does this natively (`mutation check: {'red': 11}`); Rosetta's
does not, so it was done by hand — see [Part 3](#part-3--the-oracle-itself-was-vacuous),
which is also where a **vacuous 5/5** was caught and fixed.

### Full ladder (CSim → CSynth → C/RTL CoSim)

**CHStone — 12 benchmarks, deterministic converter, `--run-vitis --auto-repair`**

| benchmark | host equiv | CSim | CSynth | CoSim |
| --- | :-: | :-: | :-: | :-: |
| `adpcm` | PASS | pass | pass | **pass** |
| `aes` | PASS | pass | pass | **pass** |
| `blowfish` | PASS | pass | pass | **pass** |
| `dfadd` | PASS | pass | pass | **pass** |
| `dfdiv` | PASS | pass | pass | **pass** |
| `dfmul` | PASS | pass | pass | **pass** |
| `dfsin` | PASS | pass | pass | **pass** |
| `gsm` | PASS | pass | pass | **pass** |
| `mips` | PASS | pass | pass | **pass** |
| `sha` | PASS | pass | pass | **pass** |
| `motion` | PASS | pass | **fail** | blocked |
| `jpeg` | **FAIL** | — | — | — |

**11/12 host equivalence** (`mutation check: {'red': 11}` — no false greens).
**10/12 complete the full ladder through C/RTL co-simulation.**

The two that do not are both properties of the input, not of the converter:

- `motion` — [L6](#l6--motions-source-is-not-synthesizable-as-written)
- `jpeg` — [L1](#l1--jpegs-golden-reference-is-not-re-entrant)

**Rosetta — 5 buildable apps**

| app | host equiv | CSim | CSynth | CoSim |
| --- | :-: | :-: | :-: | :-: |
| `spam-filter` | PASS | pass | see [L7](#l7--rosetta-csynth-status) | — |
| `digit-recognition` | PASS | pass | see [L7](#l7--rosetta-csynth-status) | — |
| `face-detection` | PASS | pass | **fail** ([L8](#l8--face-detection-narrows-float-to-int-in-its-own-source)) | blocked |
| `3d-rendering` | PASS | pass | see [L7](#l7--rosetta-csynth-status) | — |
| `optical-flow` | PASS | pass | see [L7](#l7--rosetta-csynth-status) | — |

**5/5 host equivalence** (mutation-checked by hand — see
[Part 3](#part-3--the-oracle-itself-was-vacuous)) and **5/5 CSim**.

---

## Part 1 — Host equivalence: the translation unit was not self-contained

### The single root cause

`convert._include_for_types` gave every generated header and source a hardcoded two-entry
declaration set:

```python
def _include_for_types(args, return_type) -> str:
    includes = ["#include <stdint.h>"]
    if "ap_int" in text or "ap_uint" in text:
        includes.append("#include <ap_int.h>")
```

`_generate_conservative_sources` then spliced the top's **signature** into the header and
its **body** into the source verbatim — carrying none of the typedefs, macros, enums,
structs, globals or helper functions they reference.

That one defect accounts for every failing row of both agent rungs, in three disguises.

---

### H1 — The generated header cannot name the types in its own signature

**Error** (all five Rosetta apps)

```
src/hls_top.hpp:6:30: error: 'DataType' was not declared in this scope
src/hls_top.hpp:6:40: error: 'IMAGE_HEIGHT' was not declared in this scope
src/hls_top.hpp:6:19: error: 'Triangle_3D' was not declared in this scope
```

**Input files**

| app | kernel | types live in |
| --- | --- | --- |
| `spam-filter` | `src/sw/sgd_sw.cpp` | `src/host/typedefs.h` |
| `digit-recognition` | `src/sw/digitrec_sw.cpp` | `src/host/typedefs.h` |
| `face-detection` | `src/sw/image.cpp` | `src/host/typedefs.h` |
| `3d-rendering` | `src/sw/rendering_sw.cpp` | `src/host/typedefs.h` |
| `optical-flow` | `src/sw/optical_flow_sw.cpp` | `src/host/typedefs.h` |

The emitted header was:

```c
#include <stdint.h>
void SgdLR_sw(DataType data[NUM_FEATURES * NUM_TRAINING], LabelType label[...], FeatureType theta[...]);
```

**Cause** — `<stdint.h>` is the only thing the header ever includes. Nothing declares
`DataType`.

**Fix** — `c2hlsc_agent/closure.py`. Compute the top's transitive file-scope closure with
libclang and emit it into the header (`ClosureResult.type_preamble`) and the source
(`ClosureResult.definition_preamble`). Types are emitted only into the header, so the
source — which includes the header — does not redefine them.

---

### H2 — The generated source cannot name the context its body closes over

**Error** (CHStone `gsm`, `jpeg`, `mips`, `sha`)

```
src/hls_top.cpp:11:3:  error: 'word' was not declared in this scope
src/hls_top.cpp:15:23: error: 'N' was not declared in this scope
src/hls_top.cpp:9:3:   error: 'main_result' was not declared in this scope
src/hls_top.cpp:94:22: error: 'AND' was not declared in this scope
```

**Input files** — `~/CHStone/gsm/gsm.c`, `jpeg/main.c`, `mips/mips.c`, `sha/sha_driver.c`
(the top file is read from each benchmark's own `hls.tcl`, because it is not always
`<dir>/<dir>.c`).

**What the missing symbols actually are** — measured, not assumed:

| benchmark | missing symbols | kind |
| --- | --- | --- |
| `gsm` | `word`, `N`, `M`, `so`, `inData`, `Gsm_LPC_Analysis` | typedef, macros, globals, fn in a *sibling* file |
| `mips` | `imem`, `IADDR`, `R`, `ADDU`, `AND`, `SLL`, … (38) | globals + opcode `#define`s |
| `sha` | `sha_stream`, `sha_info_digest`, `outData` | fn in sibling, globals |
| `jpeg` | `main_result`, `jpeg2bmp_main` | global, fn in sibling |

**Cause** — the body was lifted without its file scope. The repair agent's only relevant
repair was gated on finding a *function definition* for a missing symbol inside `input.c`:

```python
# c2hlsc_agent/hlsc_repair_agent.py
if not any(_has_function_definition(input_source, symbol) for symbol in symbols):
    return []
```

Running that gate against the real evidence returns `SKIP` for **every symbol of every
benchmark** — the missing names are overwhelmingly types, macros and globals, and the
functions live in sibling files that `input.c` `#include`s rather than in `input.c` itself.

**Fix** — same closure module. Macros are recovered with
`TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD`, which is why a real C frontend was
required: a preprocessing-based parser erases `#define N 160` before it can be hoisted.

---

### H3 — The workaround for H2 dragged C89 into a C++ front end

**Error** (CHStone `blowfish`, `motion`)

```
src/../bf_cfb64.c:82:1: error: variable or field 'BF_cfb64_encrypt' declared void
   82 | BF_cfb64_encrypt (in, out, length, ivec, num, encrypt)

src/../motion.c:100:10: error: redefinition of 'int v_r_size'
```

**Input files** — `~/CHStone/blowfish/bf_cfb64.c`, `~/CHStone/motion/motion.c`.

**Cause** — when the repair gate *did* fire, its fix was
`#include "../input.c"` **into a `.cpp`**. CHStone is C89: K&R parameter definitions and
tentative re-declarations are legal C and hard errors for `g++`.

**Fix** — the closure never copies a function's head. It regenerates the signature from
the parse tree (`closure._ansi_signature`), which libclang has already resolved:

```
K&R params recovered: [('unsigned char *', 'in'), ('unsigned char *', 'out'),
                       ('long', 'length'), ('unsigned char *', 'ivec'),
                       ('int *', 'num'), ('int', 'encrypt')]
return type: void
```

An earlier version detected K&R textually and missed `motion`'s multi-line parameter list,
so the signature is now **always** regenerated rather than conditionally.

---

### H4 — Locals were hoisted to file scope

**Error** (Rosetta `spam-filter`)

```
src/hls_top.cpp:25:37: error: 'theta' was not declared in this scope
src/hls_top.cpp:25:45: error: 'data' was not declared in this scope
```

from this emitted text:

```c
// --- globals ---
static FeatureType dot = dotProduct(theta, &data[NUM_FEATURES * training_id]);
```

**Cause** — my own bug. `walk_preorder` reaches every nested cursor, so the top's *locals*
matched the hoistable-kind test and were emitted at file scope, where their initializers
reference parameters that do not exist.

**Fix** — `closure._at_file_scope`: only declarations whose `semantic_parent` is the
translation unit are part of the file-scope closure.

---

### H5 — Copying source extents mangles multi-declarator and anonymous declarations

**Errors**

```
src/hls_top.cpp:19:8: error: 'j' does not name a type            # adpcm: `int i, j;`
src/hls_top.cpp:49:8: error: 'register' specifier conflicts with 'static'   # gsm
src/hls_top.cpp:62:3: error: namespace-scope anonymous aggregates must be static  # dfadd
src/hls_top.cpp:10:11: error: array must be initialized with a brace-enclosed initializer
```

**Cause** — variable declarations were emitted by copying the cursor's source extent. For
`int i, j;` the extent of `j` is just `j`; `register` collides with the `static` added for
internal linkage; an anonymous aggregate type cannot be re-spelled; and for `word so[N];`
the array-size expression `N` was mistaken for an initializer, emitting `word so[160] = N;`.

**Fix** — `closure._var_decl_text` reconstructs declarations from the parse tree
(type + name + array suffix), takes the initializer from the text after `=` rather than from
a child cursor, and gives an anonymous aggregate a synthetic tag.

---

### H6 — A minimal closure cannot see through token pasting

**Error** (CHStone `sha`)

```
src/hls_top.cpp:20:25: error: 'f1' was not declared in this scope
```

**Input file** — `~/CHStone/sha/sha.c`:

```c
#define FUNC(n,i)                                          \
    temp = ROT32(A,5) + f##n(B,C,D) + E + W[i] + CONST##n; \
    ...
```

**Cause** — `f1` exists only after preprocessing, so it appears in no reference edge and a
minimal (reference-following) closure drops it.

**Fix** — hoist the **whole** non-system file scope
(`closure._ClosureBuilder.seed_all`), which is complete by construction. The per-symbol
reference walk still runs first, so emission order stays dependency-correct, and the number
of directly-referenced symbols is recorded for reporting.

---

### H7 — Linkage and header duplication (two opposite header styles)

Once headers were re-included rather than hoisted, two link failures appeared:

```
src/hls_top.cpp:89:12: error: 'int SubByte(int)' was declared 'extern' and later 'static'
ld.bfd: multiple definition of `key'; first defined here
```

**Input files** — `~/CHStone/aes/aes.h`, `~/CHStone/sha/sha.h`,
`~/rosetta/*/src/host/typedefs.h`.

**Cause** — two header styles need opposite treatment:

| header | contains | correct treatment |
| --- | --- | --- |
| Rosetta `typedefs.h` | types and `const int` bounds (internal linkage) | **re-include** — its own guard prevents the duplicate that broke the testbench TU |
| CHStone `aes.h` | `int key[32];` — C's tentative-definition idiom | **hoist** — including it defines `key` in every TU and the link fails |

**Fix**

- `closure._ClosureBuilder._header_is_includable` — re-include a header only when it
  defines nothing mutable at file scope.
- `closure._has_visible_declaration` — a hoisted function follows the linkage of any
  declaration already visible, instead of always being `static`.
- `hls_project.py` — `-I .` added to `CXXFLAGS` so a re-included project header resolves
  from `src/hls_top.cpp`.

---

### H8 — A hoisted body needs libc headers nothing in the closure includes

**Error** (Rosetta `spam-filter`)

```
src/hls_top.cpp:21:25: error: 'expf' was not declared in this scope
```

**Input file** — `~/rosetta/spam-filter/src/sw/sgd_sw.cpp:24`:

```c
return 1.0f / (1.0f + expf(-exponent));
```

No file in that app includes a math header; Rosetta's own build supplies one transitively.

**Cause** — system declarations are deliberately not hoisted (they would collide with the
real header), so `expf` had no declaration in the generated TU.

**Fix** — `<cmath>` is stated **only when a math call is undeclared anywhere in the
closure**. A blanket standard-include list was tried first and rejected: it broke CHStone's
`adpcm`, which defines its own `abs`, turning `<cstdlib>`'s declaration into
`'int abs(int)' was declared 'extern' and later 'static'`.

---

### H9 — CRLF checkouts break every hoisted multi-line macro

**Error** (CHStone `sha`, `gsm`, `jpeg` — Windows only)

```
src/hls_top.hpp:24:26: error: stray '##' in program
src/hls_top.hpp:28:6:  error: expected ')' before '<' token
```

The emitted bytes:

```
#define FUNC(n,i)^I^I^I^I^I^I\^M
    temp = ROT32(A,5) + f##n(B,C,D) + E + W[i] + CONST##n;^I\^M
```

**Cause** — the Windows clone has CRLF line endings. A backslash followed by **CR-LF** is
not a line continuation, so the macro body spills into the header as code. This is why the
same benchmarks passed under WSL and failed natively.

**Fix** — `closure._read` normalizes CRLF to LF. Because libclang reports extents as byte
offsets into the file *as it read it*, CRLF is mapped to LF **plus a padding space** rather
than being collapsed, so every later offset stays where libclang expects it.

---

## Part 2 — Host equivalence: the argument model was a toy

`FunctionArg` already carried `array_dims` (the real shape). Both consumers discarded it.

### A1 — Array bounds were believed only when written as literal digits

**Symptom** — every Rosetta array argument fell back to the default test length **16**,
while the kernels index millions of elements. From the generated contract:

```
// - data: direction=input length=16 not compared
[warning] missing-pointer-bound: argument 'data' has no configured bound; using conservative test length 16
```

**Cause** — `analyze.py`:

```python
for dim in array_dims:
    if dim.strip().isdigit():
        length = int(dim.strip())
```

Rosetta writes `data[NUM_FEATURES * NUM_TRAINING]`, `Data[IMAGE_HEIGHT][IMAGE_WIDTH]` —
never a literal.

**Fix** — `analyze.collect_constants` + `analyze.evaluate_constant` resolve `#define`,
`const int` and `enum` constants and evaluate integer constant expressions;
`analyze._source_with_local_headers` follows local `#include`s, because the bounds live in
`src/host/typedefs.h`, not in the kernel. Multi-dimensional arrays contribute the product,
and per-dimension values are kept in a new `FunctionArg.resolved_dims`.

Two regex bugs were found and fixed while building this, both worth recording because they
are the same class of mistake the analyzer already made once:

- `[^/]+` for a macro value matches newlines, so `#define NUM_TRAINING 18000` swallowed the
  rest of the file.
- `\s+` between the macro name and its value also matches a newline, so the valueless guard
  `#define __TYPEDEFS_H__` consumed the *next* line as its value.

**Verified**

| app | resolved |
| --- | --- |
| `spam-filter` | `NUM_FEATURES`=1024, `NUM_TRAINING`=4500 → 4,608,000 |
| `digit-recognition` | `NUM_TRAINING`=18000, `DIGIT_WIDTH`=4, `NUM_TEST`=2000, `K_CONST`=3 |
| `face-detection` | `IMAGE_HEIGHT`=240, `IMAGE_WIDTH`=320 |
| `optical-flow` | `MAX_HEIGHT`=436, `MAX_WIDTH`=1024 |
| `3d-rendering` | `NUM_3D_TRI`=3192 |

---

### A2 — A 2-D parameter never type-checks

**Error**

```
tb/testbench.cpp:109:40: error: cannot convert 'bit8*' {aka 'unsigned char*'}
                                to 'bit8 (*)[256]' {aka 'unsigned char (*)[256]'}
tb/testbench.cpp:141:25: error: cannot convert 'pixel_t*' to 'pixel_t (*)[1024]'
```

**Cause** — `testgen.py` emitted one flat 1-D stack array per pointer-like argument:

```python
declarations.append(f"    {storage_type} ref_{arg.name}[{arg.length}] = {{}};")
```

**Fix** — `testgen._array_declaration` allocates on the heap and casts to the declared
shape:

```cpp
std::vector<bit8> ref_Data_storage(76800);
auto ref_Data = reinterpret_cast<bit8(*)[320]>(ref_Data_storage.data());
```

Heap allocation is not optional once bounds are real: the testbench holds **two copies of
every argument**, and `spam-filter` alone is 4.6 M elements.

---

### A3 — Struct-typed arguments had no stimulus

**Error**

```
tb/testbench.cpp:53:29: error: no matching function for call to 'Triangle_3D::Triangle_3D(int)'
tb/testbench.cpp:56:29: error: no matching function for call to 'velocity_t::velocity_t(int)'
```

**Cause** — `patterned_value<T>` opened with `static_cast<T>(0)`; `values_equal` and the
print helper likewise assumed arithmetic `T`.

**Fix** — all three are overloaded on `std::is_arithmetic`. The non-arithmetic overload
byte-fills the object deterministically, **masking each byte to `0x3F`** so any float or
double member lands on a small finite value — an unmasked fill can produce NaN, and
`NaN != NaN` would be reported as a mismatch that is not one. Comparison uses `memcmp`;
printing returns 0 with the mismatch index locating the element.

---

### A4 — 100 iterations of a full-size kernel never finish

**Symptom** — `digit-recognition` timed out and was reported as `host_behavior_mismatch`,
which is misleading: nothing mismatched.

**Cause** — with bounds finally correct, the kernel is 2000 × 18000 comparisons, run twice
per iteration, 100 times.

**Fix** — `testgen._effective_tests` scales the iteration count to the stimulus and records
the reduction in the testbench contract:

```
// - iterations reduced from 100 to 1: the stimulus is 4613524 elements per iteration at
//   the kernel's declared bounds, which is the real shape rather than a truncated one
```

A handful of iterations over the real shape is a far stronger check than a hundred over a
16-element buffer.

---

### A5 — Struct stimulus was finite but enormous, and walked off the buffer

**Error** (Rosetta `3d-rendering`, Windows)

```
make: *** [Makefile:23: test] Segmentation fault
```

**Input file** — `~/rosetta/3d-rendering/src/sw/rendering_sw.cpp`, whose top takes
`Triangle_3D triangle_3ds[NUM_3D_TRI]` and indexes its framebuffer with coordinates derived
from those vertices.

**Cause** — my own bug in [A3](#a3--struct-typed-arguments-had-no-stimulus). Masking *every*
byte to `0x3F` keeps float members finite, but leaves integer members around `0x3F3F3F3F`
— about 1.06 billion. A kernel that turns a vertex into an array index follows that value
straight off the end of the buffer. It happened to survive under WSL and crashed natively,
which is exactly how an out-of-bounds write behaves.

**Fix** — write only the low byte of each 4-byte group
(`testgen.patterned_value`, `testgen.output_sentinel`): integer members stay small, float
members stay tiny finite denormals, and the stimulus is still varied and deterministic.

> This removes the pathological magnitudes *the generator itself introduced*. It does not
> make every kernel safe against arbitrary stimulus — a kernel that indexes memory with
> values from its input still trusts that input. Use the `arguments:` config to constrain
> ranges where the domain requires it.

---

## Part 3 — The oracle itself was vacuous

**This is the most important entry in this document.** Rosetta reached 5/5 and the result
was worthless.

**Symptom** — the generated testbench said so itself:

```
// - WARNING: no return value or output/inout argument is available to compare
// - data:  direction=input length=4608000 not compared
// - label: direction=input length=4500    not compared
// - theta: direction=input length=1024    not compared
```

Three of five apps (`spam-filter`, `3d-rendering`, `optical-flow`) compared **nothing** and
passed anyway.

**Input file** — `~/rosetta/spam-filter/src/sw/sgd_sw.cpp`:

```c
void SgdLR_sw(DataType data[...], LabelType label[...], FeatureType theta[NUM_FEATURES])
{
  ...
      updateParameter(theta, gradient, -STEP_SIZE);   // theta is written HERE
}
```

**Cause** — `analyze._infer_pointer_directions` only detects writes made *directly in the
top's body*:

```python
write_pattern = rf"(?:\*\s*{name}|{name}\s*\[[^\]]+\])\s*(?:=(?!=)|\+=|...)"
```

`SgdLR_sw` never assigns `theta`; it passes it to `updateParameter`. So the kernel's only
output was classified `input`, never compared, and the equivalence check passed without
testing anything.

**Fix** — a non-const pointer handed to any call counts as `inout`. For an equivalence
oracle the safe direction is to compare: a buffer that turns out not to change still
matches, whereas one wrongly skipped hides every mismatch in it.

**Evidence after the fix** — every app now compares its real outputs, and every pass goes
red under a targeted mutation (`out[0] = out[0] + 1` inserted into the generated top):

```
spam-filter          mutant theta[0]+1    -> rc=2  RED (good)
3d-rendering         mutant output[0]+1   -> rc=2  RED (good)
optical-flow         mutant outputs[0]+1  -> rc=2  RED (good)
face-detection       mutant result_x[0]+1 -> rc=2  RED (good)
digit-recognition    mutant results[0]+1  -> rc=2  RED (good)
```

> **Open item.** `scripts/run_rosetta.py` has no built-in anti-false-green check.
> `scripts/run_chstone.py` does (`mutation check: {'red': N}`), which is why CHStone's
> numbers were trustworthy throughout and Rosetta's were not. Porting it is small and would
> have caught this automatically.

---

## Part 4 — The Vitis ladder

With `vitis_hls` present the ladder can run. Getting it to run surfaced six more defects,
all the same shape as Part 1: **the generated project was not self-contained across both
build systems.** The Makefile knew things the tcl did not.

### V1 — The Vitis launcher cannot be started on Windows

**Error**

```
FileNotFoundError: [WinError 2] The system cannot find the file specified
```

while `shutil.which('vitis_hls')` returns `D:\Xilinx\Vitis_HLS\2024.2\bin\vitis_hls.BAT`.

**Cause** — `hls_runner` ran `Popen(["vitis_hls", "-f", ...])`. `CreateProcess` only appends
`.exe` when searching PATH, so it never finds the `.bat`; and a batch file cannot be
executed directly regardless.

**Fix** — `hls_runner.vitis_executable` / `hls_runner.vitis_command`: resolve through
`C2HLSC_VITIS_BIN` first, then `shutil.which` (which honours `PATHEXT`), and launch a batch
file through `cmd /c`.

---

### V2 — CoSim transferred exactly one element per pointer

**Error** (`examples/vector_add`)

```
Mismatch test=1 arg=out index=1 expected=-2 actual=1625396401 seed=7 compare_len=16 n=16 a[i]=-1 b[i]=-1
ERROR: [COSIM 212-359] Aborting co-simulation: C TB simulation failed, nonzero return value '1'.
```

**Cause** — the first mismatch is at **index 1**, and `actual` is the testbench's own output
sentinel. A pointer parameter carries no size, so Vitis assumes `depth=1`, transfers one
element back, and everything after `out[0]` still holds its sentinel. The design is correct:
host equivalence, CSim and CSynth all passed.

**Fix** — `convert._pragma_lines` emits the analyzed bound:

```c
#pragma HLS INTERFACE ap_memory port=out depth=16
```

> **Note on the repair agent.** Its response to this CoSim failure was to *remove the
> interface pragmas* (`remove 5 generated interface pragma(s) after cosim failure`) — the
> exact opposite of the fix, since the missing `depth` on those pragmas was the cause. With
> `depth` emitted, `vector_add` and `dfmul` pass the whole ladder in one iteration with no
> repair at all.

---

### V3 — Vitis could not find the project's own headers

**Error**

```
../../../../tb/../src/hls_top.hpp:10:10: fatal error: 'softfloat.h' file not found
ERROR: [SIM 211-100] 'csim_design' failed: compilation error(s).
```

**Input file** — `~/CHStone/dfmul/softfloat.h`, reached from the hoisted closure of
`dfmul/dfmul.c`.

**Cause** — the generated tcl passed no `-cflags`. The Makefile compiles with `-I src -I .`;
Vitis builds CSim from `c2hlsc_project/solution1/csim/build`, several levels down, so the
quoted include cannot resolve.

**Fix** — `hls_project._vitis_cflags`, applied to every `add_files`:

```tcl
add_files src/hls_top.cpp -cflags "-I[file normalize .] -I[file normalize src]"
```

Paths are made absolute by Tcl at script-evaluation time. Relative paths are not enough:
CoSim's TB preprocess runs from a *different* working directory than CSim, which showed up
as the same "file not found" one rung later.

---

### V4 — The golden reference was staged into the Makefile but not the tcl

**Error**

```
ld.lld: error: undefined symbol: chstone_main_ref
```

**Cause** — `chstone_staging` compiles CHStone's C as its own translation unit
(`golden_ref.c` → `golden_ref.o`, reduced by `objcopy --keep-global-symbol`) and links it
into the host testbench. Vitis was never told: its CSim compiles only `tb/testbench.cpp` and
`src/hls_top.cpp`.

**Fix** — `chstone_staging.stage_vitis_tcl` adds `golden_ref.c` to every generated tcl as a
**testbench** file (`add_files -tb`), so it is compiled for CSim/CoSim and stays out of
synthesis — it is the oracle, not the design.

---

### V5 — Narrowing initializers are rejected by synthesis and no flag suppresses it

**Error** (CHStone `adpcm`)

```
ERROR: [HLS 207-3953] constant expression evaluates to 4294967295
                      which cannot be narrowed to type 'int' (src/hls_top.cpp:178:7)
```

**Input file** — `~/CHStone/adpcm/adpcm.c:814`:

```c
  0, 0xffffffff, 0xffffffff, 0, 0,
```

hoisted as `static const int test_result[100] = { 0, 0xffffffff, ... };`

**Cause** — legal C (implementation-defined conversion), narrowing in a C++ braced
initializer. The host build gets past it with `-Wno-narrowing`; **Vitis's synthesis front
end has its own check that no compiler flag reaches.** Verified directly: adding
`-Wno-narrowing -Wno-c++11-narrowing -fpermissive -Wno-error=narrowing` let CSim pass and
CSynth still emitted 10 of these errors.

**Fix** — `closure._cast_narrowing_initializers` casts out-of-range literals to the element
type (`(int)0xffffffff`), which is exactly the conversion C was already performing. The
narrowing flags are still mirrored into the tcl by `chstone_staging` for CSim's benefit.

**Evidence** — `adpcm` after the fix: `csim=pass csynth=pass cosim=pass`.

---

### V6 — Windows paths in the tcl are eaten as Tcl escapes

**Error** (all five Rosetta apps)

```
Invalid attribute value '-IC:/Users/.../project -DSW -w -IC:Userslukeench
osettaspam-filtersrcsw -includecstdio ...'
```

**Input** — the harness config's include dirs, e.g.
`C:\Users\luke\bench\rosetta\spam-filter\src\sw`.

**Cause** — my own bug in the fix for [V3](#v3--vitis-could-not-find-the-projects-own-headers).
Inside a Tcl double-quoted string a backslash is an escape: `\b` becomes a backspace and
`\r` a carriage return, so `...\bench\rosetta...` collapses to `...ench` + a line break.
That also terminates the quoted string early, which is why Vitis reports the whole thing as
an invalid attribute value.

**Fix** — `hls_project._tcl_path` renders every path with forward slashes, which Tcl and
Vitis both accept on Windows and which carry no escape meaning.

---

### V7 — Harness force-includes are rejected by synthesis

**Error** (Rosetta `spam-filter`, `3d-rendering`, `optical-flow`, `digit-recognition`)

```
ERROR: [HLS 207-812] 'cstdio' file not found (<built-in>:1:10)
```

**Input** — the Rosetta harness passes `-include cstdio -include iostream -include cstring
-include cstdlib` so that the *original* kernel compiles on a modern libstdc++.

**Cause** — CSim accepts those forced includes; the synthesis front end has no such header
search path. The generated design no longer needs them anyway, because the closure states
its own includes.

**Fix** — `hls_project._vitis_cflags(config, for_testbench=...)`. Include paths and defines
reach both files; `-include X` reaches only the testbench, which still compiles the
original source.

---

### V8 — Vitis rungs were unreachable from the suite runners

Both runners hardcoded `--no-run-vitis`.

**Fix** — `--run-vitis` on both:

- `scripts/run_chstone.py` drives its own ladder (`_vitis_ladder`) because its staging must
  be re-asserted between repair rounds. Repair is invoked with the **failing phase** as the
  stage, so the agent sees `csynth.log` or `cosim.log` rather than the host log. After each
  Vitis repair the host rung is re-run: a Vitis repair that regresses host equivalence is
  rejected and the ladder stops.
- `scripts/run_rosetta.py` flips the flag on its single `cli convert` call, which already
  runs the ladder and repairs per phase. Per-phase statuses are read back from
  `conversion_report.json`.

Both record `csim` / `csynth` / `cosim` per row, and `rungs_not_attempted` still lists
anything that did not run, so a host-equivalence pass can never be misread as a
synthesized design.

---

## Known limitations and open items

### L1 — `jpeg`'s golden reference is not re-entrant

`jpeg` is the one CHStone benchmark that does not reach host equivalence, and **it is not a
converter defect.** Linking the golden alone and calling it twice:

```
call1 rc=0
Huffman read error        <- second call
```

It decodes from a global stream whose read pointer is never reset. A repeated-call
equivalence loop is not a valid oracle for it, whatever the converter emits. The harness
currently scores this as a candidate failure; classifying it as an unreachable oracle would
be the honest treatment.

### L2 — `chstone_main` is a weak oracle by construction

It takes no arguments, so host equivalence reduces to calling both versions and comparing a
single `int`. It is a genuine check but far weaker than the argument-driven equivalence the
repo performs on ordinary kernels. Targeting CHStone's **inner** kernels (`float64_mul`,
`float64_add`, `Gsm_LPC_Analysis(word *s, word *LARc)`) would give real stimulus-driven
equivalence and is the obvious next experiment.

### L3 — Rosetta's runner has no mutation check

See [Part 3](#part-3--the-oracle-itself-was-vacuous). Until it does, quote Rosetta numbers
only alongside a manual mutation run.

### L4 — Six pre-existing test failures, unrelated to this work

`tests/test_run_chstone_staging.py` and `tests/test_rtllm_bench.py` fail identically before
and after every change here. Cause: gcc 15 defaults to C23, which removed K&R function
definitions, so those fixtures no longer compile as intended. They are environmental.

### L5 — The Windows and WSL toolchains disagree

`gsm`, `sha` and `jpeg` passed under WSL and failed natively on Windows until [H9](#h9--crlf-checkouts-break-every-hoisted-multi-line-macro)
was fixed. Any future "passes on Linux" claim should be re-checked natively, since only the
native path can reach Vitis on this machine.

---

### L6 — `motion`'s source is not synthesizable as written

**Error**

```
ERROR: [HLS 214-134] in function 'Get_Bits1()': Pointer to pointer is not supported
                     for variable '' (src/hls_top.cpp:392:12)
```

**Input file** — `~/CHStone/motion/getbits.c`, which walks its input with a file-scope
pointer variable:

```c
static unsigned char *ld_Rdptr;
...
ld_Rdptr = ld_Rdbfr;
ld_Bfr |= *ld_Rdptr++ << (24 - Incnt);
```

`motion` passes host equivalence and CSim; only synthesis rejects it. This is a property of
CHStone's code, not of the conversion — making it synthesizable means replacing the pointer
walk with an index, which is a rewrite the conservative converter deliberately does not
perform.

### L7 — Rosetta CSynth status

Rosetta's kernels are large (`spam-filter` alone is a 4500 × 1024 loop nest), so CSynth
takes considerably longer than CHStone's. The run in progress at the time of writing had
CSim green for all five; the CSynth column above records what completed. Re-run
`scripts/run_rosetta.py --run-vitis` to refresh, and read an incomplete CSynth as *not
attempted*, never as a pass.

### L8 — `face-detection` narrows float to int in its own source

**Error**

```
ERROR: [HLS 207-3954] type 'float' cannot be narrowed to 'int' in initializer list
                      (src/hls_top.cpp:326:19)
```

**Input file** — `~/rosetta/face-detection/src/sw/image.cpp`:

```c
MySize sz = { (IMAGE_WIDTH/factor), (IMAGE_HEIGHT/factor) };   // factor is a float
```

This is Rosetta's own code inside a function body, copied verbatim under the converter's
"preserve the original body" policy. Unlike [V5](#v5--narrowing-initializers-are-rejected-by-synthesis-and-no-flag-suppresses-it),
which fixes *hoisted file-scope* initializers, rewriting expressions inside a body is a
transformation the conservative converter does not do. Fixing it means either an explicit
cast in the Rosetta source or an opt-in body-rewriting pass.

## Coverage and honesty rules

1. **A rung that did not run is reported as not attempted, never as a pass.** Every result
   row carries `rungs_not_attempted`.
2. **A pass is only quotable with its mutation check.** CHStone's runner rebuilds every
   PASS against a perturbed candidate and requires it to go red. Rosetta's does not yet
   ([L3](#l3--rosettas-runner-has-no-mutation-check)).
3. **A benchmark blocked by the harness is reported as unreachable, not as a zero.**
4. **Denominators are stated.** Rosetta's agent rung is out of the 5 buildable apps; `BNN`
   is a vendored copy of another repo with its own build and is not attempted.
5. **CSynth passing is not a claim about QoR.** No timing or area target was set or checked;
   these runs establish synthesizability and C/RTL equivalence only.

---

## Generated artifacts reference

What the converter emits into `<out>/` for one benchmark, and which build system consumes
each file.

| file | produced by | consumed by |
| --- | --- | --- |
| `input.c` | copy of the top source (siblings staged alongside) | golden reference, closure input |
| `src/hls_top.hpp` | `convert` + `closure.type_preamble` | testbench, `hls_top.cpp`, Vitis |
| `src/hls_top.cpp` | `convert` + `closure.definition_preamble` | host `make test`, Vitis `add_files` |
| `tb/testbench.cpp` | `testgen` | host `make test`, Vitis `add_files -tb` |
| `golden_ref.c` / `.o` | `chstone_staging` | host link, Vitis `add_files -tb` |
| `Makefile` | `hls_project` (rewritten by staging) | `make test` |
| `run_csim.tcl` / `run_csynth.tcl` / `run_cosim.tcl` / `run_hls.tcl` | `hls_project` (rewritten by staging) | `vitis_hls -f` |
| `conversion_report.json` | `cli convert` | runners, repair agent |
| `repair_audit.json` | `hlsc_repair_agent` | audit trail of every applied repair |
| `software_equivalence.log`, `csim.log`, `csynth.log`, `cosim.log` | each phase | repair evidence, this document |

### Where the fixes live

| area | file | entry points |
| --- | --- | --- |
| closure extraction | `c2hlsc_agent/closure.py` | `extract_closure`, `_ansi_signature`, `_var_decl_text`, `_read`, `_at_file_scope`, `_header_is_includable`, `_has_visible_declaration`, `_cast_narrowing_initializers` |
| bounds and directions | `c2hlsc_agent/analyze.py` | `collect_constants`, `evaluate_constant`, `_source_with_local_headers`, `_infer_pointer_directions` |
| testbench generation | `c2hlsc_agent/testgen.py` | `_array_declaration`, `_effective_tests`, `patterned_value` / `values_equal` / `printable` overloads |
| generated sources | `c2hlsc_agent/convert.py` | `_closure_for`, `_generate_conservative_sources`, `_pragma_lines` |
| Vitis project | `c2hlsc_agent/hls_project.py` | `_vitis_cflags` |
| Vitis launch | `c2hlsc_agent/hls_runner.py` | `vitis_executable`, `vitis_command` |
| CHStone staging | `c2hlsc_agent/chstone_staging.py` | `stage_vitis_tcl` |
| suite runners | `scripts/run_chstone.py`, `scripts/run_rosetta.py` | `--run-vitis`, `_vitis_ladder` |
| tests | `tests/test_closure.py` | closure, constant resolution, direction inference |
