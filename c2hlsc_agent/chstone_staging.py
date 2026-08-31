"""Staging that lets CHStone's C reach this repo's host-equivalence harness.

The harness compiles ``tb/testbench.cpp`` (C++) and textually ``#include``s the golden
reference ``input.c`` into it inside ``extern "C" { ... }``. For ordinary single-function
kernels that is fine. For CHStone it is not, and it fails in two distinct ways that have
nothing to do with the candidate under test:

``original_c_not_valid_cpp``
    CHStone is C89-era C. K&R parameter definitions (``blowfish``, ``motion``), tentative
    definitions redeclared at file scope (``jpeg``), ``register`` declarations (``jpeg``)
    and unsigned initialisers narrowed into ``int`` arrays (``adpcm``, ``jpeg``) are all
    legal C that a C++ front end rejects outright. ``extern "C"`` changes *linkage*, not
    the language the tokens are parsed as, so wrapping the include does not help.

``golden_candidate_symbol_collision``
    The testbench macro-renames the golden *function* but not the ~dozens of file-scope
    globals the CHStone top closes over. Once the candidate also carries those definitions
    -- which it must, since the CHStone "kernel" is a whole ``main`` -- the link dies with
    ``multiple definition of 'main_result'``.

Both are defects of the *staging*, not of the benchmark and not of the candidate. This
module fixes them the only way that changes no semantics:

1.  The golden reference is compiled **as C, by a C compiler, in its own translation
    unit** (``golden_ref.c`` -> ``golden_ref.o``). CHStone's C is never handed to ``g++``
    again, so every construct above simply stops being an error.
2.  ``objcopy --keep-global-symbol=<top>_ref`` reduces that object to exactly one exported
    symbol. Every other global becomes local, so the golden's state cannot collide with
    -- or, worse, be silently *shared* with -- the candidate's.
3.  ``tb/testbench.cpp`` drops the ``#include "../input.c"`` block and declares
    ``extern "C" int <top>_ref(void);`` instead. The oracle it compares against is
    unchanged; only how that oracle is built changed.

What this module deliberately does **not** do: it does not edit CHStone sources, it does
not touch ``src/hls_top.cpp`` (the thing under measurement), it does not reduce the
stimulus count, and it does not relax what the testbench compares.

The one compiler flag it adds, ``-Wno-narrowing``, applies only to the C++ side and is
justified in :func:`narrowing_relaxation_note`.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

#: Suffix the testbench uses for the golden symbol (``chstone_main`` -> ``chstone_main_ref``).
GOLDEN_SUFFIX = "_ref"

#: Name of the mutant translation unit written by :func:`write_mutant_source`.
MUTANT_SOURCE = "hls_top_mutant.cpp"

#: Make target that builds and runs the mutant.
MUTANT_TARGET = "mutation-check"

#: Files the converter emits that this module rewrites. Everything else is left alone.
_TESTBENCH = Path("tb/testbench.cpp")
_MAKEFILE = Path("Makefile")


def golden_symbol(top: str) -> str:
    return f"{top}{GOLDEN_SUFFIX}"


def narrowing_relaxation_note() -> str:
    """Why ``-Wno-narrowing`` on the C++ side changes no semantics.

    CHStone initialises ``int`` arrays with unsigned hex literals (``0xffffffff``).
    In C that is an ordinary implementation-defined conversion; GCC documents it as
    two's-complement truncation. In C++ the *same* conversion inside a braced initialiser
    is additionally ill-formed-if-narrowing, which GCC reports through ``-Wnarrowing``.
    Suppressing that diagnostic restores the C behaviour byte for byte -- it is a
    diagnostic switch, not a codegen switch -- and the harness asserts exactly that by
    comparing the emitted array bytes from a C and a C++ build
    (``tests/test_run_chstone_staging.py::NarrowingRelaxationTest``).
    """

    return (
        "-Wno-narrowing suppresses a C++-only diagnostic for a conversion GCC performs "
        "identically in C and C++ (two's-complement truncation); the emitted data is "
        "byte-identical, verified by test_run_chstone_staging.NarrowingRelaxationTest"
    )


@dataclass
class StagingResult:
    """What the staging did to one generated project."""

    applied: bool = False
    steps: list[str] = field(default_factory=list)
    staged_siblings: list[str] = field(default_factory=list)
    golden_source: str | None = None
    golden_export: str | None = None
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "steps": list(self.steps),
            "staged_siblings": list(self.staged_siblings),
            "golden_source": self.golden_source,
            "golden_export": self.golden_export,
            "skipped_reason": self.skipped_reason,
        }


# --------------------------------------------------------------------------- #
# sibling sources
# --------------------------------------------------------------------------- #


def stage_sibling_sources(project_dir: Path, source_dir: Path, top_file_name: str) -> list[str]:
    """Copy the CHStone top's sibling sources next to ``input.c``.

    CHStone tops ``#include`` their siblings by relative name (``softfloat.c``,
    ``getbits.c``, ...). The converter copies only ``--input`` into the project, so without
    this the *golden reference* cannot even be preprocessed -- and a missing-include error
    in the reference reads exactly like a defect in the generated HLS-C.
    """

    if not project_dir.exists() or not source_dir.exists():
        return []
    copied: list[str] = []
    for sibling in sorted(source_dir.iterdir()):
        if not sibling.is_file() or sibling.name in {top_file_name, "hls.tcl"}:
            continue
        target = project_dir / sibling.name
        if target.exists():
            continue
        shutil.copy(sibling, target)
        copied.append(sibling.name)
    return copied


# --------------------------------------------------------------------------- #
# golden reference translation unit
# --------------------------------------------------------------------------- #


def render_golden_source(top: str) -> str:
    ref = golden_symbol(top)
    return f"""/* Generated by c2hlsc_agent CHStone staging -- DO NOT EDIT.
 *
 * The golden reference is CHStone's own C. It is compiled HERE, by a C compiler, in its
 * own translation unit, and linked into the equivalence testbench as the single symbol
 * `{ref}`. Nothing about the oracle changed: this is the same `input.c`, renamed the same
 * way the testbench used to rename it. Only the front end that parses it changed, from
 * g++ (which rejects legal C89) to gcc.
 */
#define {top} {ref}
#include "input.c"
#undef {top}
"""


def write_golden_source(project_dir: Path, top: str) -> Path:
    path = project_dir / "golden_ref.c"
    path.write_text(render_golden_source(top), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# testbench rewrite
# --------------------------------------------------------------------------- #

#: The block ``c2hlsc_agent.testgen`` emits to pull the golden in. Matched structurally
#: (opening ``extern "C" {``, an ``#include "../input.c"``, closing ``}``) rather than by
#: exact text, so a formatting change in testgen degrades to "not applied" instead of a
#: silently wrong rewrite.
_EXTERN_C_OPEN = re.compile(r'^\s*extern\s+"C"\s*\{\s*$')
_INPUT_INCLUDE = re.compile(r'^\s*#\s*include\s+"\.\./input\.c"\s*$')
_BLOCK_CLOSE = re.compile(r"^\s*\}\s*$")


def _golden_call_signature(text: str, top: str) -> tuple[str, bool]:
    """Return type of the golden call the testbench actually emits, and whether it is nullary.

    Read out of the testbench rather than assumed, so a declaration that does not match the
    call site can never be written. CHStone's top is ``int chstone_main(void)``; anything
    with arguments is not a CHStone top and this module declines to stage it.
    """

    match = re.search(
        rf"^\s*(?P<ret>[A-Za-z_][\w \t\*]*?)\s+ref_ret\s*=\s*{re.escape(top)}{GOLDEN_SUFFIX}\s*\((?P<args>[^)]*)\)",
        text,
        re.M,
    )
    if not match:
        return "", False
    return match.group("ret").strip(), not match.group("args").strip()


def rewrite_testbench(text: str, top: str) -> tuple[str, bool]:
    """Replace the inlined golden with an ``extern "C"`` declaration of the linked one.

    Returns ``(new_text, changed)``. ``changed`` is False when the expected block is not
    present, which the caller must treat as "staging did not apply" rather than ignore.
    """

    ref = golden_symbol(top)
    return_type, nullary = _golden_call_signature(text, top)
    if not return_type or not nullary:
        return text, False
    lines = text.splitlines()
    start = end = None
    for index, line in enumerate(lines):
        if _EXTERN_C_OPEN.match(line):
            for cursor in range(index + 1, min(index + 12, len(lines))):
                if _BLOCK_CLOSE.match(lines[cursor]):
                    end = cursor
                    break
                if _INPUT_INCLUDE.match(lines[cursor]):
                    start = index
            if start is not None and end is not None:
                break
            start = end = None
    if start is None or end is None:
        return text, False
    replacement = [
        "// c2hlsc_agent CHStone staging: the golden reference is CHStone's own C, compiled",
        "// as C in golden_ref.c and linked in as a single exported symbol. Including it here",
        "// fed C89 to a C++ front end and leaked every file-scope global into this binary.",
        f'extern "C" {return_type} {ref}(void);',
    ]
    new_lines = lines[:start] + replacement + lines[end + 1:]
    return "\n".join(new_lines) + "\n", True


# --------------------------------------------------------------------------- #
# Makefile rewrite
# --------------------------------------------------------------------------- #

#: The testbench rule as ``hls_project.render_makefile`` writes it. Matched on both the
#: prerequisite list *and* the recipe, so a rule that has been reshaped is left alone
#: instead of being replaced on the strength of its target name.
_TB_RULE = re.compile(
    r"^\$\(TB_EXE\):[^\n]*\btb/testbench\.cpp\b[^\n]*\n(?:\t[^\n]*\n)+",
    re.M,
)


def render_make_rules(top: str, *, relax_narrowing: bool) -> str:
    ref = golden_symbol(top)
    narrowing = ""
    if relax_narrowing:
        narrowing = (
            "# " + narrowing_relaxation_note() + "\n"
            "CXXFLAGS += -Wno-narrowing\n"
        )
    return f"""{narrowing}CC ?= gcc
OBJCOPY ?= objcopy
GOLDEN_OBJ ?= golden_ref.o
GOLDEN_EXPORT ?= {ref}
# CHStone's C is compiled by a C compiler, not by g++. -w only silences warnings; it is the
# language, not the diagnostics, that this line is about.
GOLDEN_CFLAGS ?= -w -O1
MUT_EXE ?= c2hlsc_tb_mutant

# Reduce the golden object to exactly one exported symbol so the reference's file-scope
# state can neither collide with nor be shared with the candidate's.
$(GOLDEN_OBJ): golden_ref.c input.c
\t$(CC) $(GOLDEN_CFLAGS) -c golden_ref.c -o $(GOLDEN_OBJ)
\t$(OBJCOPY) --keep-global-symbol=$(GOLDEN_EXPORT) $(GOLDEN_OBJ)

$(TB_EXE): tb/testbench.cpp src/hls_top.cpp src/hls_top.hpp $(GOLDEN_OBJ)
\t$(CXX) $(CXXFLAGS) tb/testbench.cpp src/hls_top.cpp $(GOLDEN_OBJ) -o $(TB_EXE) -lm

# Mutation check: the same testbench against a candidate whose returned value is perturbed
# by one. A green run here would mean the equivalence test proves nothing.
$(MUT_EXE): tb/testbench.cpp src/{MUTANT_SOURCE} src/hls_top.hpp $(GOLDEN_OBJ)
\t$(CXX) $(CXXFLAGS) tb/testbench.cpp src/{MUTANT_SOURCE} $(GOLDEN_OBJ) -o $(MUT_EXE) -lm

{MUTANT_TARGET}: $(MUT_EXE)
\t./$(MUT_EXE)

"""


def rewrite_makefile(text: str, top: str, *, relax_narrowing: bool) -> tuple[str, bool]:
    """Swap the single-TU testbench rule for the golden-object one."""

    match = _TB_RULE.search(text)
    if match is None:
        return text, False
    recipe = match.group(0)
    if "src/hls_top.cpp" not in recipe or "-o $(TB_EXE)" not in recipe:
        # The rule builds something other than "testbench + candidate into one binary";
        # this staging has no idea how to preserve whatever that is.
        return text, False
    rules = render_make_rules(top, relax_narrowing=relax_narrowing)
    new_text = text[: match.start()] + rules + text[match.end():]
    new_text = new_text.replace(
        "rtl-vectors rtl-testbench rtl-cosim clean vitis",
        f"rtl-vectors rtl-testbench rtl-cosim clean vitis {MUTANT_TARGET}",
        1,
    )
    new_text = new_text.replace(
        "\trm -f $(TB_EXE)",
        "\trm -f $(TB_EXE) $(MUT_EXE) $(GOLDEN_OBJ)",
        1,
    )
    return new_text, True


# --------------------------------------------------------------------------- #
# mutation source
# --------------------------------------------------------------------------- #

#: Lines of the candidate that must keep the top's *original* spelling: the repair agent's
#: support block renames the top so the copied ``input.c`` does not redefine it, and
#: rewriting those two directives would let ``input.c`` define the top at global scope.
_PREPROC_TOP_LINE = re.compile(r"^\s*#\s*(?:define|undef)\b")


def render_mutant_source(candidate: str, top: str) -> tuple[str, bool]:
    """Rename the candidate's top and add a wrapper that perturbs its returned value.

    Returns ``(source, changed)``. ``changed`` is False when the top's identifier never
    appears outside preprocessor directives, in which case the caller must report the
    mutation check as *inconclusive* -- never as evidence.
    """

    body_name = f"{top}_c2hlsc_premutation"
    word = re.compile(rf"\b{re.escape(top)}\b")
    out: list[str] = []
    renamed = 0
    for line in candidate.splitlines():
        if _PREPROC_TOP_LINE.match(line) and word.search(line):
            out.append(line)
            continue
        line, count = word.subn(body_name, line)
        renamed += count
        out.append(line)
    if renamed == 0:
        return candidate, False
    out.append("")
    out.append("// c2hlsc_agent CHStone staging -- MUTANT. Perturbs the candidate's returned")
    out.append("// value by one. The equivalence test must go red against this build; if it")
    out.append("// stays green the test is vacuous and no pass from it may be quoted.")
    out.append(f"int {body_name}();")
    out.append(f"int {top}() {{ return {body_name}() + 1; }}")
    return "\n".join(out) + "\n", True


def write_mutant_source(project_dir: Path, top: str) -> Path | None:
    candidate = project_dir / "src" / "hls_top.cpp"
    if not candidate.exists():
        return None
    source, changed = render_mutant_source(candidate.read_text(encoding="utf-8"), top)
    if not changed:
        return None
    path = project_dir / "src" / MUTANT_SOURCE
    path.write_text(source, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


_TCL_FILES = ("run_csim.tcl", "run_cosim.tcl", "run_hls.tcl")


_NARROWING_CFLAGS = "-Wno-narrowing -Wno-c++11-narrowing"


def stage_vitis_tcl(project_dir: Path, *, relax_narrowing: bool = True) -> list[str]:
    """Add golden_ref.c to every generated tcl, and mirror the host's narrowing flags.

    Returns the tcl files actually changed. Idempotent.
    """

    changed: list[str] = []
    for name in _TCL_FILES:
        path = project_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if relax_narrowing and _NARROWING_CFLAGS not in text:
            text = text.replace('-cflags "', f'-cflags "{_NARROWING_CFLAGS} ')
            path.write_text(text, encoding="utf-8")
            if path.name not in changed:
                changed.append(path.name)
        if "golden_ref.c" in text:
            continue
        marker = "add_files -tb tb/testbench.cpp"
        index = text.find(marker)
        if index < 0:
            continue
        end = text.find("\n", index)
        if end < 0:
            continue
        # reuse whatever -cflags the testbench line already carries
        tb_line = text[index:end]
        cflags = ""
        if "-cflags" in tb_line:
            cflags = tb_line[tb_line.index("-cflags"):]
        insertion = f'\nadd_files -tb golden_ref.c {cflags}'.rstrip()
        path.write_text(text[:end] + insertion + text[end:], encoding="utf-8")
        changed.append(name)
    return changed


def stage_project(
    project_dir: Path,
    source_dir: Path,
    top_file_name: str,
    top: str,
    *,
    relax_narrowing: bool = True,
) -> StagingResult:
    """Apply the whole CHStone staging to a generated project.

    Idempotent: re-running it on an already staged project reports ``applied`` without
    rewriting anything, so it is safe to call between repair rounds.
    """

    result = StagingResult()
    if not project_dir.exists():
        result.skipped_reason = f"project directory missing: {project_dir}"
        return result
    result.staged_siblings = stage_sibling_sources(project_dir, source_dir, top_file_name)
    if result.staged_siblings:
        result.steps.append(f"staged {len(result.staged_siblings)} sibling source(s) next to input.c")

    testbench = project_dir / _TESTBENCH
    makefile = project_dir / _MAKEFILE
    if not testbench.exists() or not makefile.exists():
        result.skipped_reason = "generated project has no tb/testbench.cpp or Makefile"
        return result

    ref = golden_symbol(top)
    tb_text = testbench.read_text(encoding="utf-8")
    already = f'extern "C"' in tb_text and f" {ref}(void);" in tb_text
    if not already:
        new_tb, changed = rewrite_testbench(tb_text, top)
        if not changed:
            _, nullary = _golden_call_signature(tb_text, top)
            result.skipped_reason = (
                f"tb/testbench.cpp does not call {ref}() with zero arguments; this staging "
                "is only valid for a CHStone-shaped nullary top"
                if not nullary
                else "tb/testbench.cpp does not contain the expected inlined-golden block; "
                     "refusing to guess"
            )
            return result
        testbench.write_text(new_tb, encoding="utf-8")
        result.steps.append('tb/testbench.cpp: inlined golden -> extern "C" declaration')

    mk_text = makefile.read_text(encoding="utf-8")
    if "$(GOLDEN_OBJ)" not in mk_text:
        new_mk, changed = rewrite_makefile(mk_text, top, relax_narrowing=relax_narrowing)
        if not changed:
            result.skipped_reason = "Makefile does not contain the expected $(TB_EXE) rule"
            return result
        makefile.write_text(new_mk, encoding="utf-8")
        result.steps.append("Makefile: link golden_ref.o (C TU, one exported symbol)")
        if relax_narrowing:
            result.steps.append("Makefile: CXXFLAGS += -Wno-narrowing (" + narrowing_relaxation_note() + ")")

    golden = write_golden_source(project_dir, top)
    staged_tcl = stage_vitis_tcl(project_dir, relax_narrowing=relax_narrowing)
    if staged_tcl:
        result.steps.append(
            "Vitis tcl: added golden_ref.c as a testbench file (" + ", ".join(staged_tcl) + ")"
        )
    result.golden_source = str(golden)
    result.golden_export = ref
    result.applied = True
    if not result.steps:
        result.steps.append("already staged")
    return result
