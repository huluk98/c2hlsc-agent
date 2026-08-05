"""RTLLM v2.0 benchmark model plus an Icarus Verilog verifier.

This module is the *oracle* half of the RTLLM agent: it knows how to find the 50
benchmark designs on disk, how to build a sandbox for one candidate RTL file, and how
to turn ``iverilog`` + ``vvp`` output into a pass/fail verdict. It contains no LLM
code and never reads a model response -- keeping the judge separate from the player is
the same split the HLS side of this repo uses (``equivalence.py`` vs ``hlsc_generator.py``).

Two oracles are reported side by side, because they disagree and the disagreement is
informative:

``func_pass`` (official)
    Literally the rule the benchmark's own ``auto_run.py`` uses: the simulator's stdout
    contains ``"Pass"`` or ``"pass"``. It is generous -- several RTLLM testbenches print
    per-vector ``Failed at ...`` lines and *still* reach a banner containing "Passed",
    and a design that prints nothing but the word "bypass" would score.

``func_pass_strict``
    The official marker AND no failure marker anywhere in the output AND the run did not
    time out or flood its output. This is the number to trust when comparing agents;
    ``func_pass`` is the number to quote when comparing against published RTLLM results.

Both oracles read one stream, so the candidate must not be able to write to it. ``vvp``
merges the design's output with the testbench's, and the pass rule is a substring test --
so a module containing ``initial $display("Design pass");`` would score, and one adding
``$finish`` would score on the strict oracle too by ending the run before the testbench
can print its failure banner (measured: ``adder_8bit`` with zeroed outputs plus those two
lines scored ``func_pass_strict=True``). :func:`find_illegal_system_tasks` therefore
rejects any candidate containing an output or simulation-control system task *before*
compiling it, as the ``illegal_system_task`` failure family. The generator prompt already
forbids those tasks; this is the gate that enforces it, and the benchmark's own
``verified_*.v`` files contain none of them, so the reference baseline is unaffected.

The other direction is not fixable by a gate: four testbenches pass a module with **no
logic at all**, because an X-valued output makes their ``if`` condition X and the error
counter is never incremented. Those designs are listed in :data:`VACUOUS_ORACLE_DESIGNS`
and are measured, not guessed -- :func:`empty_stub_rtl` builds the port-only stub and the
driver's ``--empty-baseline`` mode scores it for every design.

Simulator notes (measured on this benchmark checkout with iverilog 12.0):

- Three testbenches ``$readmemh`` a golden data file (``alu``/``reference.dat``,
  ``calendar``/``reference.txt``, ``signal_generator``/``tri_gen.txt``) and ``asyn_fifo``
  reads three more. Those files ship *inside the design directory*, so the sandbox must
  copy the design's non-Verilog support files next to the testbench. Without that copy the
  three designs are unpassable for any RTL; with it the reference RTL passes all four.
- Two testbenches use SystemVerilog that iverilog rejects outright. Rather than drop the
  designs (which would silently penalise the model for a simulator gap) the runner applies
  a small, semantics-preserving rewrite to a *copy* of the testbench -- see
  ``TESTBENCH_SHIMS``. Each shim was verified to (a) compile and pass with the benchmark's
  own reference RTL and (b) still fail on obviously wrong RTL.

With both of those in place the **oracle ceiling** measured on RTLLM v2.0 with iverilog 12.0
is 50/50 syntax and 47/50 functional, for the official and the strict oracle alike; the
three shortfalls are listed in ``KNOWN_ORACLE_ISSUES``. An agent cannot beat 47/50 here, so
report agent scores against that ceiling rather than against 50. The honest *floor* is 4/50,
the contents of ``VACUOUS_ORACLE_DESIGNS``: quote both bounds or neither.

Beyond scoring, this module also produces the *evidence* the repair agent is allowed to see,
split into two tracks that must never be merged (see the section near the bottom of the file):

- **self-derived** (:func:`run_self_trace`, :func:`diagnose_timeout`) -- an instrumented COPY
  of the candidate prints the candidate's OWN ports and registers. Nothing that ran here is
  ever scored, the scored candidate is never modified, and only the instrumentation's own
  tagged lines survive, so the testbench's stdout cannot ride along. Admissible in a strict,
  publishable measurement.
- **oracle-derived** (:func:`oracle_behaviour_diff`) -- the benchmark's ``verified_*.v`` is
  run on the same testbench and the first divergence between the two stdout streams is
  reported. Behaviour only; the reference source never leaves the function. This is an
  UPPER BOUND and is not comparable to published RTLLM numbers.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Sequence

DEFAULT_BENCHMARK_URL = "https://github.com/hkust-zhiyao/RTLLM.git"
IVERILOG_STANDARD = "-g2012"
DEFAULT_COMPILE_TIMEOUT = 120
DEFAULT_SIM_TIMEOUT = 30

#: Tail slice kept for every captured log. Matches ``equivalence.PhaseResult.to_dict``.
LOG_TAIL_CHARS = 4000

#: Name of the compiled simulation binary inside a sandbox.
SIM_BINARY = "sim"

#: Directories whose designs are model output shipped with the benchmark, not benchmarks.
SKIP_PATH_MARKER = "_chatgpt"

DESIGN_DESCRIPTION_FILE = "design_description.txt"
TESTBENCH_FILE = "testbench.v"

#: Files that must never be copied into a sandbox: ``*.v`` would leak the golden RTL,
#: the makefile targets VCS, and the description is handed to the model separately.
_SUPPORT_FILE_EXCLUDES = frozenset({DESIGN_DESCRIPTION_FILE, "makefile", "Makefile"})

#: Substrings that mark a *failed* run, surveyed across all 50 RTLLM testbenches. Matched
#: case-insensitively. Every one of them appears only on a genuine failure path in the
#: benchmark: the ``Test completed with %d ... failures`` banners live exclusively in the
#: ``else`` branch of an ``if (error == 0)``, and ``square_wave``'s ``Error: %d`` $monitor
#: (which would print unconditionally) is commented out upstream.
FAILURE_MARKERS = (
    "===========Error===========",
    "Failed",
    "Test completed with",
    "Error:",
    "ERROR:",
)

#: The benchmark's own oracle, kept verbatim from ``auto_run.py``.
PASS_MARKERS = ("Pass", "pass")

FAILURE_FAMILIES = (
    "compile_error",
    "missing_module",
    "port_mismatch",
    "functional_mismatch",
    "timeout",
    "no_output",
    "simulator_unsupported",
    "missing_golden_data",
    "illegal_system_task",
    "runaway_output",
)

#: Designs whose oracle is broken independently of the RTL under test. Measured on this
#: checkout by running the benchmark's own ``verified_*.v`` through its own testbench with
#: this module: 50/50 compile, 47/50 pass, and these three are the whole remainder.
#:
#: Note what is *not* here: ``alu``, ``calendar`` and ``signal_generator`` look unpassable
#: if the sandbox only copies ``testbench.v``, because their ``$readmemh`` golden data
#: ships as ``reference.dat`` / ``reference.txt`` / ``tri_gen.txt`` inside the design
#: directory. ``evaluate_rtl`` copies those support files, and all three then pass.
KNOWN_ORACLE_ISSUES = {
    "clkgenerator": (
        "Upstream oracle bug: the benchmark's own verified_clkgenerator.v fails its own "
        "testbench under iverilog ('Test completed with 20 failures'), so no RTL can score."
    ),
    "radix2_div": (
        "Upstream oracle bug: the benchmark's own verified_radix2_div.v fails its own "
        "testbench under iverilog (3 'Error: dividend=...' lines then "
        "'===========Failed==========='), so no RTL can score."
    ),
    "ring_counter": (
        "Simulator-ordering bug: the testbench's two always @(posedge clk) blocks race, and "
        "iverilog runs the 'i = i + 1' block before the 'if (i == 9)' pass check, so the "
        "banner never prints -- the reference RTL matches all 10 expected values (no 'Failed "
        "at' line) yet the run ends silently at t=100 and scores 0."
    ),
}

#: Designs whose oracle is *vacuous*: a module with the right port list and NO LOGIC AT ALL
#: scores a pass on both the official and the strict oracle. ``KNOWN_ORACLE_ISSUES`` catalogues
#: oracles that are too strict; this is the opposite failure, and a report that corrects only
#: the first direction is biased upward from both ends.
#:
#: Measured, not guessed: :func:`empty_stub_rtl` builds the port-only stub for every design and
#: the driver's ``--empty-baseline`` mode scores it. On this checkout with iverilog 12.0 exactly
#: these four report ``syntax_pass=func_pass=func_pass_strict=True`` for a module whose body is
#: empty. The mechanism is X-optimism: every output is X, so the testbench's check condition
#: (``if ((A > B && !A_greater) || ...)`` in comparator_3bit/testbench.v:33, ``if
#: (!sequence_detected)``, ``if (wave_out_tb == 1)``) evaluates to X, the ``if`` takes the false
#: branch, ``error`` is never incremented, and the ``if (error == 0)`` banner prints.
#:
#: These designs are still run and still counted in ``totals``. What they must not do is inflate
#: an *adjusted* rate: the driver drops them from the sound-oracle basis alongside
#: ``KNOWN_ORACLE_ISSUES``.
VACUOUS_ORACLE_DESIGNS = {
    "comparator_3bit": (
        "X-optimistic oracle: with all outputs undriven the testbench's combined check "
        "condition evaluates to X, the error counter stays 0 and the pass banner prints. An "
        "empty module scores a strict pass."
    ),
    "comparator_4bit": (
        "X-optimistic oracle: same combined check as comparator_3bit. An empty module scores "
        "a strict pass."
    ),
    "sequence_detector": (
        "X-optimistic oracle: 'if (!sequence_detected) error = error + 1' never fires while "
        "sequence_detected is X. An empty module scores a strict pass."
    ),
    "square_wave": (
        "X-optimistic oracle: 'if (wave_out_tb == 1)' is X for an undriven output, so the "
        "consecutive-ones check never runs. An empty module scores a strict pass."
    ),
}

_RING_COUNTER_ARRAY_INIT = """reg [7:0] data [0:9];
    initial begin
        data[0] = 8'b00000001;
        data[1] = 8'b00000001;
        data[2] = 8'b00000010;
        data[3] = 8'b00000100;
        data[4] = 8'b00001000;
        data[5] = 8'b00010000;
        data[6] = 8'b00100000;
        data[7] = 8'b01000000;
        data[8] = 8'b10000000;
        data[9] = 8'b00000001;
    end"""

#: design name -> ordered ``(pattern, replacement, rationale)`` rewrites applied to a COPY
#: of ``testbench.v``. Both entries translate SystemVerilog that iverilog rejects into
#: plain Verilog-2001 with identical behaviour; neither weakens a check. Verified by
#: running the shimmed testbench against the reference RTL (passes) and against RTL with
#: the outputs tied to zero (still fails).
TESTBENCH_SHIMS = {
    "ring_counter": (
        (
            r"reg\s*\[\s*7\s*:\s*0\s*\]\s*data\s*\[\s*0\s*:\s*9\s*\]\s*=\s*\{[^}]*\}\s*;",
            _RING_COUNTER_ARRAY_INIT,
            "iverilog rejects the SystemVerilog array declaration initializer "
            "('sorry: Assignment to an entire array or to an array slice is not yet "
            "supported'); the same ten values are assigned in order from an initial block, "
            "which still settles at time 0, well before the first posedge at t=5.",
        ),
    ),
    "asyn_fifo": (
        (
            r"initial\s+begin\s*\n(\s*)repeat\s*\(\s*17\s*\)",
            "initial begin : rtllm_write_burst\n\\1repeat (17)",
            "Names the initial block that drives the write burst so the loop can be left "
            "early without SystemVerilog's 'break'.",
        ),
        (
            r"\bbreak\s*;",
            "disable rtllm_write_burst;",
            "iverilog rejects 'break' ('sorry: break statements not supported'). Nothing "
            "follows the repeat inside that block, so disabling the enclosing named block "
            "is exactly 'leave the loop' and preserves the write sequence.",
        ),
    ),
}


@dataclass(frozen=True)
class RtllmDesign:
    """One RTLLM benchmark design directory."""

    name: str
    category: str
    directory: Path
    description: str
    testbench: Path
    reference_files: "tuple[Path, ...]"

    def to_dict(self) -> "dict[str, object]":
        return {
            "name": self.name,
            "category": self.category,
            "directory": str(self.directory),
            "description": self.description,
            "testbench": str(self.testbench),
            "reference_files": [str(path) for path in self.reference_files],
            "known_oracle_issue": KNOWN_ORACLE_ISSUES.get(self.name),
            "vacuous_oracle": VACUOUS_ORACLE_DESIGNS.get(self.name),
        }

    @property
    def support_files(self) -> "tuple[Path, ...]":
        """Non-Verilog data files the testbench may ``$readmemh`` / ``$fopen``."""

        return _support_files(self.directory)


@dataclass
class SimResult:
    """Verdict for one candidate RTL file against one design's testbench."""

    design: str
    syntax_pass: bool
    func_pass: bool
    func_pass_strict: bool
    timed_out: bool
    compile_log: str
    sim_log: str
    duration_s: float
    failure_family: "str | None"
    shim_applied: bool = False
    runaway_output: bool = False

    def to_dict(self) -> "dict[str, object]":
        return {
            "design": self.design,
            "syntax_pass": self.syntax_pass,
            "func_pass": self.func_pass,
            "func_pass_strict": self.func_pass_strict,
            "timed_out": self.timed_out,
            "compile_log": self.compile_log[-LOG_TAIL_CHARS:],
            "sim_log": self.sim_log[-LOG_TAIL_CHARS:],
            "duration_s": round(self.duration_s, 3),
            "failure_family": self.failure_family,
            "shim_applied": self.shim_applied,
            "runaway_output": self.runaway_output,
        }


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def _support_files(directory: Path) -> "tuple[Path, ...]":
    files = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix == ".v":
            continue
        if entry.name in _SUPPORT_FILE_EXCLUDES:
            continue
        files.append(entry)
    return tuple(files)


def discover_designs(
    root: Path,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> "list[RtllmDesign]":
    """Walk ``root`` for RTLLM designs.

    A design is any directory holding both ``design_description.txt`` and ``testbench.v``.
    Paths containing ``_chatgpt`` are skipped: those hold the benchmark's archived model
    output, not benchmark problems. ``include``/``exclude`` filter on the design *name*
    with exact, case-sensitive matches. The result is sorted by name so runs are
    reproducible and ``--limit`` is meaningful.
    """

    root = Path(root)
    include_set = set(include or ())
    exclude_set = set(exclude or ())
    designs: "list[RtllmDesign]" = []
    seen: "set[str]" = set()

    for description_path in sorted(root.rglob(DESIGN_DESCRIPTION_FILE)):
        if SKIP_PATH_MARKER in description_path.as_posix():
            continue
        directory = description_path.parent
        testbench = directory / TESTBENCH_FILE
        if not testbench.is_file():
            continue
        name = directory.name
        if include_set and name not in include_set:
            continue
        if name in exclude_set:
            continue
        if name in seen:
            continue
        try:
            relative = directory.relative_to(root)
            category = relative.parent.as_posix()
        except ValueError:  # pragma: no cover - defensive
            category = ""
        if category == ".":
            category = ""
        references = tuple(
            path
            for path in sorted(directory.glob("*.v"))
            if path.name != TESTBENCH_FILE
        )
        designs.append(
            RtllmDesign(
                name=name,
                category=category,
                directory=directory,
                description=_read_text(description_path),
                testbench=testbench,
                reference_files=references,
            )
        )
        seen.add(name)

    designs.sort(key=lambda design: design.name)
    return designs


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# oracles
# ---------------------------------------------------------------------------


def has_failure_marker(text: str) -> bool:
    """True when the simulator output contains any surveyed RTLLM failure banner."""

    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in FAILURE_MARKERS)


def classify_output(
    text: str,
    timed_out: bool = False,
    runaway: bool = False,
) -> "tuple[bool, bool]":
    """Return ``(official, strict)`` verdicts for simulator stdout.

    ``official`` is byte-for-byte the benchmark rule from ``auto_run.py``: the word
    ``Pass`` or ``pass`` appears anywhere. ``strict`` additionally demands that no
    failure marker was printed and that the run finished on its own -- neither killed by
    the watchdog (``timed_out``) nor killed for flooding its output (``runaway``).

    A testbench that prints ``===========Failed===========`` and then somewhere else the
    word "pass" therefore yields ``(True, False)`` -- callers must report the two numbers
    separately rather than picking one.

    Both verdicts are substring tests over one stream that the design under test also
    writes to, which is only sound because :func:`find_illegal_system_tasks` refuses any
    candidate able to write to it. Do not call this on output from an ungated candidate.
    """

    text = text or ""
    official = any(marker in text for marker in PASS_MARKERS)
    strict = official and not timed_out and not runaway and not has_failure_marker(text)
    return official, strict


def classify_failure(
    compile_log: str,
    sim_log: str,
    syntax_pass: bool,
    timed_out: bool,
    runaway: bool = False,
) -> "str | None":
    """Bucket one run into a ``FAILURE_FAMILIES`` label, or ``None`` if it truly passed.

    "Truly passed" means the *strict* oracle: a run that reaches a pass banner but also
    printed ``Failed at ...`` is reported as ``functional_mismatch`` so the repair agent
    still sees actionable evidence. Use ``SimResult.func_pass`` for the headline metric.

    Pure function of the stored logs and flags, so re-running it on a serialized
    :class:`SimResult` reproduces the same label -- including ``illegal_system_task``,
    which is recognised from the refusal banner :data:`ILLEGAL_TASK_MARKER` rather than
    from out-of-band state.
    """

    compile_log = compile_log or ""
    sim_log = sim_log or ""

    if not syntax_pass:
        if ILLEGAL_TASK_MARKER in compile_log:
            return "illegal_system_task"
        if "Unknown module type" in compile_log:
            return "missing_module"
        if "sorry:" in compile_log.lower():
            return "simulator_unsupported"
        if _looks_like_port_mismatch(compile_log):
            return "port_mismatch"
        return "compile_error"

    _, strict = classify_output(sim_log, timed_out, runaway)
    if strict:
        return None
    if runaway:
        return "runaway_output"
    if timed_out:
        return "timeout"
    if "sorry:" in sim_log.lower():
        return "simulator_unsupported"
    if _looks_like_missing_golden_data(sim_log):
        return "missing_golden_data"
    if not sim_log.strip():
        return "no_output"
    return "functional_mismatch"


# --------------------------------------------------------------------------- #
# candidate admissibility
# --------------------------------------------------------------------------- #

#: System tasks a candidate design file may not contain. Two hazards, one rule:
#:
#: * output tasks (``$display``/``$write``/``$monitor``/``$strobe`` and their ``$f...``
#:   variants -- ``$fdisplay(1, ...)`` writes to stdout) let the design write into the very
#:   stream ``classify_output`` greps, so a module printing ``"Design pass"`` would score;
#: * control tasks (``$finish``/``$stop``) let the design end the run at t=0, before the
#:   testbench can print its failure banner, which fakes a *strict* pass.
#:
#: ``$dump*`` is included because it writes files from inside the design under test; it is
#: not a scoring hazard, but it is forbidden by the generator prompt for the same reason.
#: Deliberately NOT listed: ``$signed``/``$unsigned``/``$clog2``/``$bits``/``$time``/
#: ``$random``, which are legitimate in RTL and cannot reach stdout.
_ILLEGAL_TASK_RE = re.compile(
    r"\$(?:display|write|monitor|strobe|fdisplay|fwrite|fmonitor|fstrobe|dump)\w*",
    re.IGNORECASE,
)
_ILLEGAL_CONTROL_RE = re.compile(r"\$(?:finish|stop)\w*", re.IGNORECASE)

#: Stamped into ``compile_log`` when a candidate is refused, so ``classify_failure`` can
#: recover the ``illegal_system_task`` label from the stored result alone.
ILLEGAL_TASK_MARKER = "rtllm_bench: refused -- illegal system task in the design file"


def _strip_comments_and_strings(text: str) -> str:
    """Blank out ``//`` / ``/* */`` comments and string literals in one left-to-right pass.

    Regex-per-construct would mis-handle a ``//`` inside a string (and a quote inside a
    comment), and a false positive here fails a legitimate design, so the scan is exact.
    """

    text = text or ""
    out: "list[str]" = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if char == "/" and nxt == "/":
            end = text.find("\n", index)
            index = length if end < 0 else end
        elif char == "/" and nxt == "*":
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            out.append(" ")
        elif char == '"':
            cursor = index + 1
            while cursor < length and text[cursor] != '"':
                cursor += 2 if text[cursor] == "\\" else 1
            index = cursor + 1
            out.append('""')
        else:
            out.append(char)
            index += 1
    return "".join(out)


def find_illegal_system_tasks(rtl_text: str) -> "tuple[tuple[int, str], ...]":
    """Return ``((line_number, token), ...)`` for every inadmissible system task.

    The pass oracle is a substring test over a stream the design under test shares with
    the testbench, so a candidate that can print is a candidate that can score itself.
    This is the gate that makes the oracle sound; the prompt asking the model not to print
    is not a gate. Comments and string literals are excluded, so a ``// no $display here``
    note and a ``"$finish"`` inside a string are both fine.
    """

    stripped = _strip_comments_and_strings(rtl_text or "")
    found: "list[tuple[int, str]]" = []
    for pattern in (_ILLEGAL_TASK_RE, _ILLEGAL_CONTROL_RE):
        for match in pattern.finditer(stripped):
            line = stripped.count("\n", 0, match.start()) + 1
            found.append((line, match.group(0)))
    return tuple(sorted(set(found)))


#: Tokens listed in a refusal. Bounded so the message survives ``_tail``'s 4000-char slice
#: with :data:`ILLEGAL_TASK_MARKER` intact -- ``classify_failure`` recovers the family from it.
_MAX_LISTED_VIOLATIONS = 20


def illegal_task_report(violations: "Sequence[tuple[int, str]]") -> str:
    """The refusal text handed to the repair agent as ``compile_log``."""

    shown = list(violations)[:_MAX_LISTED_VIOLATIONS]
    listed = ", ".join(f"line {line}: {token}" for line, token in shown) or "(none)"
    if len(violations) > len(shown):
        listed += f", ... ({len(violations) - len(shown)} more)"
    return (
        f"{ILLEGAL_TASK_MARKER}\n"
        "The design file contains simulation output or control system tasks. The benchmark "
        "oracle greps the simulator's stdout for 'Pass'/'pass', and the design under test "
        "shares that stream with the testbench, so such a design could report its own "
        "verdict (or end the run with $finish before the testbench reports one). The "
        "candidate was refused without being compiled.\n"
        f"Offending tokens: {listed}\n"
        "Fix: delete every $display/$write/$monitor/$strobe/$fdisplay/$finish/$stop/$dump* "
        "from the design and drive the outputs with real logic instead. Only the hidden "
        "testbench may print."
    )


def _looks_like_port_mismatch(log: str) -> bool:
    lowered = log.lower()
    return (
        "is not a port of" in lowered
        or "unknown module port" in lowered
        or "port expression must support" in lowered
    )


def _looks_like_missing_golden_data(log: str) -> bool:
    return "unable to open" in log.lower() and "readmem" in log.lower()


# ---------------------------------------------------------------------------
# reference RTL
# ---------------------------------------------------------------------------

_MODULE_RE = re.compile(r"\bmodule\s+(verified_[A-Za-z_$][A-Za-z0-9_$]*)")


def reference_rtl_text(design: RtllmDesign) -> str:
    """Concatenate the design's reference Verilog with the top module renamed.

    RTLLM ships each golden design as ``module verified_<something>`` while the testbench
    instantiates ``<design name>``. The top is the (single) ``verified_*`` module declared
    in the first reference file -- note that it is *not* always the first module in the
    file (``asyn_fifo`` declares ``dual_port_RAM`` first) and *not* always
    ``verified_<design name>`` (``adder_pipe_64bit`` ships ``verified_adder_64bit`` and
    ``multi_pipe_4bit`` ships ``verified_multi_pipe``). Designs whose reference already
    declares the plain name (``clkgenerator``, ``ring_counter``, ...) are left untouched.
    """

    if not design.reference_files:
        return ""
    chunks = [_read_text(path) for path in design.reference_files]
    text = "\n\n".join(chunk for chunk in chunks if chunk)
    top = _top_module_name(chunks[0] if chunks else "")
    if not top or top == design.name:
        return text
    return re.sub(r"\b%s\b" % re.escape(top), design.name, text)


def _top_module_name(text: str) -> "str | None":
    matches = _MODULE_RE.findall(text or "")
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# empty baseline (vacuous-oracle detection)
# ---------------------------------------------------------------------------

_PORT_DECL_RE = re.compile(r"^\s*(input|output|inout)\b")
_BODY_START_RE = re.compile(
    r"^\s*(always|assign|initial|endmodule|generate|function|task|reg\b|wire\b|"
    r"integer\b|genvar\b|parameter\b|localparam\b)"
)
_ANY_MODULE_RE = re.compile(
    r"(?m)^[ \t]*module[ \t\r\n]+(?P<name>\\\S+|[A-Za-z_][A-Za-z0-9_$]*)"
)


def _matching_paren(text: str, open_index: int) -> int:
    """Index of the ``)`` closing the ``(`` at ``open_index``, or ``-1``."""

    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _top_module_declaration(text: str, design_name: str) -> "re.Match[str] | None":
    """The reference's declaration of the module the testbench instantiates.

    Prefers an exact name match. RTLLM references declare helper modules ABOVE the top
    (``asyn_fifo`` starts with ``dual_port_RAM``), so the fallback is the LAST declaration
    -- needed for the handful of designs whose reference does not use the ``verified_``
    prefix that ``reference_rtl_text`` renames (``fixed_point_substractor`` ships
    ``module fixed_point_subtractor``, matching its testbench but not its directory).
    """

    declarations = list(_ANY_MODULE_RE.finditer(text or ""))
    if not declarations:
        return None
    for match in declarations:
        if match.group("name") == design_name:
            return match
    return declarations[-1]


def empty_stub_rtl(design: RtllmDesign) -> str:
    """Build the design's port list with an EMPTY body -- the floor any oracle must reject.

    Derived from the reference file's declaration of the top module (the header with its
    parameter and port lists, plus the non-ANSI ``input``/``output``/``inout`` declarations
    that follow it) with every statement removed. Nothing drives any output, so an oracle
    that passes this stub is vacuous for that design -- see :data:`VACUOUS_ORACLE_DESIGNS`.

    Harness-only: this reads the golden file and must never be reachable from a prompt.
    Returns ``""`` when the declaration cannot be located; a stub that fails to build or to
    compile simply scores as a failure, so the measurement can only *under*-report vacuity,
    never invent it.
    """

    text = reference_rtl_text(design)
    header = _top_module_declaration(text, design.name)
    if not header:
        return ""

    cursor = header.end()
    parameters = ""
    hash_index = text.find("#", cursor)
    semicolon = text.find(";", cursor)
    open_paren = text.find("(", cursor)
    # `module foo #(parameter W = 8) (ports);` -- keep the parameter block, the testbench
    # may rely on its defaults, then step over it to reach the port list.
    if 0 <= hash_index < (open_paren if open_paren >= 0 else len(text)) and (
        semicolon < 0 or hash_index < semicolon
    ):
        params_open = text.find("(", hash_index)
        params_close = _matching_paren(text, params_open) if params_open >= 0 else -1
        if params_close < 0:
            return ""
        parameters = text[hash_index : params_close + 1]
        cursor = params_close + 1
        semicolon = text.find(";", cursor)
        open_paren = text.find("(", cursor)

    name = header.group("name")
    if open_paren < 0 or (0 <= semicolon < open_paren):  # `module foo;` -- no ports at all
        return "module %s%s;\nendmodule\n" % (name, parameters)

    close_paren = _matching_paren(text, open_paren)
    if close_paren < 0:
        return ""
    ports = text[open_paren : close_paren + 1]

    declarations: "list[str]" = []
    rest = text[close_paren + 1 :]
    body_start = rest.find(";")
    for line in (rest[body_start + 1 :] if body_start >= 0 else "").splitlines():
        if _PORT_DECL_RE.match(line):
            stripped = line.strip()
            declarations.append("  " + (stripped if stripped.endswith(";") else stripped + ";"))
        elif _BODY_START_RE.match(line):
            break
    body = ("\n".join(declarations) + "\n") if declarations else ""
    return "module %s%s%s;\n%sendmodule\n" % (name, parameters, ports, body)


# ---------------------------------------------------------------------------
# testbench shims
# ---------------------------------------------------------------------------


def apply_testbench_shims(design_name: str, text: str) -> "tuple[str, bool]":
    """Return ``(text, applied)`` after running this design's shim rules, if any."""

    rules = TESTBENCH_SHIMS.get(design_name)
    if not rules:
        return text, False
    applied = False
    for pattern, replacement, _rationale in rules:
        new_text, count = re.subn(pattern, replacement, text, count=1)
        if count:
            applied = True
            text = new_text
    return text, applied


def shim_rationale(design_name: str) -> str:
    """Human-readable explanation of why this design's testbench is rewritten."""

    rules = TESTBENCH_SHIMS.get(design_name) or ()
    return " ".join(rationale for _pattern, _replacement, rationale in rules)


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------


#: Prefix stamped on a truncated log. Fixed length so ``_tail`` output is exactly
#: ``LOG_TAIL_CHARS`` and ``SimResult.to_dict``'s defensive re-slice cannot eat the marker.
_TRUNCATION_MARKER = "...[log truncated]...\n"


def _tail(text: str, limit: int = LOG_TAIL_CHARS) -> str:
    """Keep the last ``limit`` characters: a tool's failure signature is at the END."""

    text = text or ""
    if len(text) <= limit:
        return text
    return _TRUNCATION_MARKER + text[-(limit - len(_TRUNCATION_MARKER)) :]


#: Bytes of each captured stream kept in RAM (the TAIL -- a tool's signature is at the end).
#: Everything past it is counted and dropped as it arrives, so a chatty run costs a fixed
#: amount of memory regardless of how much it prints.
CAPTURE_TAIL_BYTES = 64 * 1024

#: Total bytes one stream may emit before the run is killed as ``runaway_output``. Without
#: a cap a design with a ``$display`` inside a repeating block buffers its entire stream:
#: measured 683 MB (and 1.5 GB peak RSS) for a *10-second* sim timeout on this checkout,
#: which at the documented ``--workers 8`` is an OOM kill of the whole sweep with no report
#: written. It also broke the watchdog -- a 10 s timeout took 29 s wall, because the kill
#: path still had to drain and decode the backlog.
RUNAWAY_OUTPUT_BYTES = 8 * 1024 * 1024

_READ_CHUNK = 1 << 16
_POLL_INTERVAL = 0.02  # watchdog granularity, in seconds
_KILL_GRACE = 10  # seconds to wait for a killed process / its reader threads


class _RunOutcome(NamedTuple):
    returncode: "int | None"
    stdout: str
    stderr: str
    timed_out: bool
    runaway: bool


class _BoundedCapture:
    """Sink for one stream: keeps the last ``tail`` bytes, counts and discards the rest.

    Single writer (the reader thread) and single reader (the watchdog loop), which is why
    plain attributes are enough: ``total``/``overflowed`` are only ever read for a
    monotone "has it blown the budget yet" decision.

    ``head`` (default 0, i.e. off) additionally keeps the FIRST ``head`` bytes. Verification
    logs are judged by their tail, which is why that is the default; a *behaviour trace* is
    judged by its beginning -- the first transitions after reset are where a design goes
    wrong -- so the trace runner asks for both ends and lets the middle go.
    """

    def __init__(
        self,
        tail: int = CAPTURE_TAIL_BYTES,
        limit: int = RUNAWAY_OUTPUT_BYTES,
        head: int = 0,
    ) -> None:
        self._tail = max(1, tail)
        self._limit = max(1, limit)
        self._head_limit = max(0, head)
        self._head = bytearray()
        self._buffer = bytearray()
        self.total = 0
        self.overflowed = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._head_limit and len(self._head) < self._head_limit:
            self._head.extend(chunk[: self._head_limit - len(self._head)])
        self.total += len(chunk)
        self._buffer.extend(chunk)
        excess = len(self._buffer) - self._tail
        if excess > 0:
            del self._buffer[:excess]
        if self.total > self._limit:
            self.overflowed = True

    def text(self) -> str:
        body = self._buffer.decode("utf-8", errors="replace")
        if self.total <= len(self._buffer):  # nothing was dropped: the tail IS the stream
            return body
        if not self._head_limit:
            return _TRUNCATION_MARKER + body
        # Nothing was dropped only in the branch above, so head and tail cannot overlap.
        head = self._head.decode("utf-8", errors="replace")
        return head + ("\n" if not head.endswith("\n") else "") + _TRUNCATION_MARKER + body


def _drain(stream: object, sink: _BoundedCapture) -> None:
    """Pump one pipe into ``sink`` until EOF. Never raises: the watchdog owns the process."""

    read = getattr(stream, "read", None)
    if read is None:  # pragma: no cover - defensive
        return
    try:
        while True:
            chunk = read(_READ_CHUNK)
            if not chunk:
                break
            sink.feed(chunk)
    except (OSError, ValueError):  # pragma: no cover - pipe closed under us by the kill
        pass
    finally:
        try:
            stream.close()  # type: ignore[union-attr]
        except (OSError, ValueError):  # pragma: no cover - already closed
            pass


def _wait(proc: "subprocess.Popen[bytes]", timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _run(
    command: "list[str]",
    cwd: Path,
    timeout: int,
    *,
    output_limit: int = RUNAWAY_OUTPUT_BYTES,
    capture_head: int = 0,
) -> _RunOutcome:
    """Run ``command`` with a bounded capture, killing the process group when it misbehaves.

    Returns ``(returncode, stdout, stderr, timed_out, runaway)``. Never raises: a missing
    binary or an OS error comes back as ``returncode=None`` with the reason on stderr.
    Partial output produced before a kill is preserved (tail-sliced).

    Deliberately NOT ``communicate()``: that buffers the whole stream in one Python object
    before any truncation can happen, so an unbounded simulation takes the sweep down with
    it (see :data:`RUNAWAY_OUTPUT_BYTES`). Reader threads drain both pipes into fixed-size
    ring buffers, the pipes are read as bytes so a huge backlog is never UTF-8-decoded, and
    the watchdog kills the process group as soon as either budget is blown.
    """

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return _RunOutcome(None, "", "failed to launch %s: %s" % (command[0], exc), False, False)

    stdout = _BoundedCapture(limit=output_limit, head=capture_head)
    stderr = _BoundedCapture(limit=output_limit, head=capture_head)
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + max(0, timeout)
    timed_out = runaway = False
    while proc.poll() is None:
        if stdout.overflowed or stderr.overflowed:
            runaway = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(_POLL_INTERVAL)

    if timed_out or runaway:
        _kill_group(proc)
        if not _wait(proc, _KILL_GRACE):  # pragma: no cover - the SIGKILL path
            _kill_group(proc, signal.SIGKILL)
            _wait(proc, _KILL_GRACE)
    for reader in readers:
        reader.join(timeout=_KILL_GRACE)
    return _RunOutcome(proc.returncode, stdout.text(), stderr.text(), timed_out, runaway)


def _kill_group(proc: "subprocess.Popen[bytes]", sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (AttributeError, OSError):
        try:
            proc.kill()
        except OSError:  # pragma: no cover - process already reaped
            pass


def _prepare_workdir(design: RtllmDesign, rtl_text: str, workdir: Path, apply_shims: bool) -> bool:
    """Materialise the sandbox. Returns whether a testbench shim was applied."""

    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / ("%s.v" % design.name)).write_text(rtl_text or "", encoding="utf-8")

    testbench_text = _read_text(design.testbench)
    shim_applied = False
    if apply_shims:
        testbench_text, shim_applied = apply_testbench_shims(design.name, testbench_text)
    (workdir / TESTBENCH_FILE).write_text(testbench_text, encoding="utf-8")

    # $readmemh / $fopen golden data lives beside the testbench; the RTL never does.
    for support in design.support_files:
        try:
            shutil.copyfile(support, workdir / support.name)
        except OSError:
            continue
    return shim_applied


def evaluate_rtl(
    design: RtllmDesign,
    rtl_text: str,
    workdir: Path,
    *,
    compile_timeout: int = DEFAULT_COMPILE_TIMEOUT,
    sim_timeout: int = DEFAULT_SIM_TIMEOUT,
    apply_shims: bool = True,
    enforce_illegal_task_gate: bool = True,
) -> SimResult:
    """Compile and simulate ``rtl_text`` against ``design``'s testbench in ``workdir``.

    Thread-safe as long as each concurrent call gets its own ``workdir``: no module state
    is mutated and every tool runs with ``cwd=workdir``. Never raises -- a broken toolchain
    comes back as ``syntax_pass=False`` with the reason in ``compile_log``.

    ``enforce_illegal_task_gate=False`` skips the admissibility check of
    :func:`find_illegal_system_tasks` and lets a candidate that can write to the simulator's
    stdout run anyway. **The verdict it returns is not a sound score**: the pass oracle is a
    substring test over a stream the design under test shares with the testbench, so such a
    candidate can print its own ``Pass``. It exists for one purpose -- measuring how much the
    gate costs a set of candidates that were produced without being told about it, so the
    gate's effect on a comparison can be stated rather than assumed. Nothing that feeds a
    headline number may use it.
    """

    workdir = Path(workdir)
    started = time.time()
    try:
        return _evaluate_rtl(
            design,
            rtl_text,
            workdir,
            started,
            compile_timeout=compile_timeout,
            sim_timeout=sim_timeout,
            apply_shims=apply_shims,
            enforce_illegal_task_gate=enforce_illegal_task_gate,
        )
    except Exception as exc:  # pragma: no cover - a harness crash must not kill a sweep
        return SimResult(
            design=design.name,
            syntax_pass=False,
            func_pass=False,
            func_pass_strict=False,
            timed_out=False,
            compile_log="rtllm_bench failed to evaluate %s in %s: %r" % (design.name, workdir, exc),
            sim_log="",
            duration_s=time.time() - started,
            failure_family="compile_error",
            shim_applied=False,
        )


def _evaluate_rtl(
    design: RtllmDesign,
    rtl_text: str,
    workdir: Path,
    started: float,
    *,
    compile_timeout: int,
    sim_timeout: int,
    apply_shims: bool,
    enforce_illegal_task_gate: bool = True,
) -> SimResult:
    shim_applied = _prepare_workdir(design, rtl_text, workdir, apply_shims)

    # Admissibility BEFORE compilation: a candidate able to write to the simulator's stdout
    # (or to end the run early) can produce its own verdict, and no downstream oracle could
    # tell that apart from a real pass. The candidate is still written to the sandbox so the
    # refusal is auditable next to every other attempt.
    violations = find_illegal_system_tasks(rtl_text) if enforce_illegal_task_gate else ()
    if violations:
        compile_log = illegal_task_report(violations)
        return SimResult(
            design=design.name,
            syntax_pass=False,
            func_pass=False,
            func_pass_strict=False,
            timed_out=False,
            compile_log=_tail(compile_log),
            sim_log="",
            duration_s=time.time() - started,
            failure_family=classify_failure(compile_log, "", False, False),
            shim_applied=shim_applied,
        )

    compile_cmd = [
        "iverilog",
        IVERILOG_STANDARD,
        "-o",
        SIM_BINARY,
        "%s.v" % design.name,
        TESTBENCH_FILE,
    ]
    compiled = _run(compile_cmd, workdir, compile_timeout)
    compile_log = _join_streams(compiled.stdout, compiled.stderr)
    binary = workdir / SIM_BINARY
    syntax_pass = compiled.returncode == 0 and binary.is_file() and not compiled.timed_out

    sim_log = ""
    sim_timed_out = runaway = False
    func_pass = func_pass_strict = False
    if syntax_pass:
        simulated = _run(["vvp", SIM_BINARY], workdir, sim_timeout)
        sim_timed_out, runaway = simulated.timed_out, simulated.runaway
        # stdout and stderr are judged together so that re-running classify_output /
        # classify_failure on the stored SimResult reproduces the verdict exactly. vvp
        # writes $display to stdout, so this matches the benchmark's `make sim > out.txt`.
        sim_log = _join_streams(simulated.stdout, simulated.stderr)
        if runaway:
            # Appended before classification so the stored log explains the verdict. Worded
            # to contain neither a pass nor a failure marker, so it cannot move either oracle.
            sim_log += (
                "\n--- rtllm_bench: killed the simulation after more than %d bytes of output "
                "(runaway_output) ---\n" % RUNAWAY_OUTPUT_BYTES
            )
        func_pass, func_pass_strict = classify_output(sim_log, sim_timed_out, runaway)

    timed_out = compiled.timed_out or sim_timed_out

    failure_family = classify_failure(compile_log, sim_log, syntax_pass, timed_out, runaway)

    return SimResult(
        design=design.name,
        syntax_pass=syntax_pass,
        func_pass=func_pass,
        func_pass_strict=func_pass_strict,
        timed_out=timed_out,
        compile_log=_tail(compile_log),
        sim_log=_tail(sim_log),
        duration_s=time.time() - started,
        failure_family=failure_family,
        shim_applied=shim_applied,
        runaway_output=runaway,
    )


def _join_streams(stdout: str, stderr: str) -> str:
    stdout = stdout or ""
    stderr = stderr or ""
    if not stderr.strip():
        return stdout
    return stdout + ("\n" if stdout and not stdout.endswith("\n") else "") + "--- stderr ---\n" + stderr


def evaluate_reference(design: RtllmDesign, workdir: Path, **kwargs) -> SimResult:
    """Run the benchmark's own golden RTL -- the oracle baseline an agent is measured against."""

    return evaluate_rtl(design, reference_rtl_text(design), workdir, **kwargs)


def evaluate_empty_stub(design: RtllmDesign, workdir: Path, **kwargs) -> SimResult:
    """Run a port-only module with no logic -- the oracle FLOOR, the mirror of the ceiling.

    A design that passes here has a vacuous oracle: the score says nothing about the RTL.
    Whatever this reports is what any agent, including one that emits an empty module,
    banks for free, so it belongs beside the reference baseline in every report.
    """

    return evaluate_rtl(design, empty_stub_rtl(design), workdir, **kwargs)


# ---------------------------------------------------------------------------
# self-derived behaviour traces (evidence, NEVER a score)
# ---------------------------------------------------------------------------
#
# Everything below produces *evidence for the repair agent*, not verdicts. Two tracks,
# deliberately kept apart because they are not comparable:
#
# ``run_self_trace`` / ``diagnose_timeout`` -- the STRICT, self-derived track. A copy of the
#     candidate is instrumented with print statements over the candidate's OWN ports and
#     registers and run against the same testbench in a separate workdir. Only lines the
#     instrumentation itself emitted (tagged :data:`TRACE_MARKER`) are kept, so the
#     testbench's stdout -- and therefore anything the testbench knows about the expected
#     answer -- cannot ride along. The testbench source is never read into the result, and
#     neither is the reference RTL. Nothing here returns a :class:`SimResult`: the
#     instrumented copy is structurally incapable of being scored, and the scored candidate
#     is never touched. The instrumented copy necessarily contains system tasks the
#     :func:`find_illegal_system_tasks` gate forbids, which is exactly why it never goes
#     through :func:`evaluate_rtl`.
#
# ``oracle_behaviour_diff`` -- the ORACLE-DERIVED track, an UPPER BOUND. It runs the
#     benchmark's own ``verified_*.v`` and reports where the two stdout streams first
#     diverge. It hands over BEHAVIOUR ONLY (line number, expected line, got line); the
#     reference RTL source never leaves this function. Results produced with it are not
#     comparable to published RTLLM numbers and every caller must say so.

#: Tag on every line the self-instrumentation prints. Filtering on it is what keeps the
#: testbench's own output -- which knows the expected answers -- out of the strict track.
TRACE_MARKER = "RTLLM_TRACE"

#: Tag on the bounded-stop notice injected by :func:`diagnose_timeout`.
DIAG_MARKER = "RTLLM_DIAG"

#: Most signals one trace prints. Ports come first, so a design with many internal registers
#: still gets its interface traced. Wide traces are unreadable and blow the evidence budget.
MAX_TRACE_SIGNALS = 24

#: Output budget for a trace run. Far below :data:`RUNAWAY_OUTPUT_BYTES` because a trace is
#: read by an LLM, not archived, and a hung design prints without bound.
TRACE_OUTPUT_LIMIT = 2 * 1024 * 1024

#: Bytes kept from each END of a trace stream. The first transitions after reset and the
#: last ones before the hang are both load-bearing; the middle is not.
TRACE_CAPTURE_HEAD = 48 * 1024
TRACE_CAPTURE_TAIL = 48 * 1024

#: Digest shape: full first ``HEAD`` transitions, then ``TAIL`` evenly sampled from the rest.
TRACE_DIGEST_HEAD = 24
TRACE_DIGEST_TAIL = 12
TRACE_DIGEST_LIMIT = 4000

#: Simulation-time bound injected into the timeout re-run, in the TESTBENCH's time unit --
#: about 2000 cycles of a clock with a ``#5`` half period, whatever that unit happens to be.
#: Best effort only: a candidate that ships its own ``timescale`` directive overrides the
#: matched one and changes the unit. The authoritative bound on the re-run is the wall clock,
#: :data:`DIAG_WALL_CLOCK_S`.
DEFAULT_DIAG_TIME_LIMIT = 20_000

#: Matches ONLY the two time literals of a ``timescale`` directive. Nothing else from the
#: testbench text can escape through :func:`testbench_timescale` -- see its docstring for why
#: reading this one directive is not an oracle leak.
_TIMESCALE_RE = re.compile(
    r"`timescale\s+(\d+\s*[munpf]?s)\s*/\s*(\d+\s*[munpf]?s)", re.IGNORECASE
)

#: Wall-clock seconds for the bounded timeout re-run. Deliberately short: the design has
#: already burned one full ``sim_timeout`` proving it hangs, and the diagnosis only needs the
#: shape of the hang, not its continuation.
DIAG_WALL_CLOCK_S = 10

#: Longest stdout line quoted in an oracle diff, per side.
DIFF_LINE_CHARS = 240

_ENDMODULE_RE = re.compile(r"(?m)^[ \t]*endmodule\b")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_BODY_DECL_RE = re.compile(r"(?m)^[ \t]*(?:input|output|inout|reg|integer)\b[^;]*;")

#: Words that can precede the declared name in a port or register declaration.
_DECL_KEYWORDS = (
    "input",
    "output",
    "inout",
    "reg",
    "wire",
    "logic",
    "integer",
    "signed",
    "unsigned",
    "var",
)
_DECL_KEYWORD_RE = re.compile(r"\s*(?:%s)\b" % "|".join(_DECL_KEYWORDS))
_LEADING_RANGE_RE = re.compile(r"\s*\[[^\]]*\]")


class ModuleSpan(NamedTuple):
    """Where one ``module ... endmodule`` unit sits in a candidate file."""

    name: str
    start: int
    body_start: int  # first character after the header's `;`
    end: int  # index of the `endmodule` keyword


def _split_top_level(text: str, separator: str = ",") -> "list[str]":
    """Split on ``separator`` at bracket depth zero."""

    parts: "list[str]" = []
    current: "list[str]" = []
    depth = 0
    for char in text or "":
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == separator and depth <= 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _declared_names(chunk: str) -> "list[str]":
    """Names declared by one declaration fragment, minus arrays.

    ``reg [7:0] a, b`` -> ``["a", "b"]``; ``input wire clk`` -> ``["clk"]``. Unpacked arrays
    (``reg [7:0] mem [0:255]``) are dropped: a memory cannot be printed with ``%b`` and one
    illegal argument kills the whole instrumented copy at elaboration.
    """

    text = chunk or ""
    while True:
        match = _DECL_KEYWORD_RE.match(text)
        if match:
            text = text[match.end() :]
            continue
        match = _LEADING_RANGE_RE.match(text)
        if match:
            text = text[match.end() :]
            continue
        break

    names: "list[str]" = []
    for item in _split_top_level(text):
        item = item.split("=", 1)[0].strip()
        match = _IDENT_RE.match(item)
        if not match:
            continue
        if "[" in item[match.end() :]:  # unpacked array / memory
            continue
        names.append(match.group(0))
    return names


def module_span(rtl_text: str, design_name: str) -> "ModuleSpan | None":
    """Locate the module the testbench will instantiate, or ``None``.

    Reuses :func:`_top_module_declaration`'s rule (exact name, else the last declaration)
    so instrumentation targets the same module the harness scores.
    """

    text = rtl_text or ""
    header = _top_module_declaration(text, design_name)
    if header is None:
        return None
    semicolon = text.find(";", header.end())
    if semicolon < 0:
        return None
    end = _ENDMODULE_RE.search(text, semicolon)
    if end is None:
        return None
    return ModuleSpan(header.group("name"), header.start(), semicolon + 1, end.start())


def candidate_trace_signals(rtl_text: str, design_name: str) -> "tuple[str, ...]":
    """The candidate's own ports and registers, in trace order, capped and de-duplicated.

    Derived ONLY from the candidate file: ports from the module header (ANSI or non-ANSI),
    then every ``input``/``output``/``inout``/``reg``/``integer`` declared in the module
    body. Nothing here reads the testbench or the reference.
    """

    span = module_span(rtl_text, design_name)
    if span is None:
        return ()
    text = rtl_text or ""

    ports: "list[str]" = []
    open_paren = text.find("(", span.start)
    if 0 <= open_paren < span.body_start:
        close_paren = _matching_paren(text, open_paren)
        if 0 <= close_paren < span.body_start:
            inside = text[open_paren + 1 : close_paren]
            # `module foo #(...) (ports);` -- the parameter block is matched first; step past it.
            if text[span.start : open_paren].rstrip().endswith("#"):
                next_open = text.find("(", close_paren + 1)
                if 0 <= next_open < span.body_start:
                    next_close = _matching_paren(text, next_open)
                    inside = text[next_open + 1 : next_close] if next_close > next_open else ""
            for item in _split_top_level(inside):
                ports.extend(_declared_names(item))

    registers: "list[str]" = []
    body = text[span.body_start : span.end]
    for match in _BODY_DECL_RE.finditer(body):
        registers.extend(_declared_names(match.group(0).rstrip(";")))

    ordered: "list[str]" = []
    seen: "set[str]" = set()
    for name in list(ports) + registers:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return tuple(ordered[:MAX_TRACE_SIGNALS])


def testbench_timescale(design: RtllmDesign) -> "str | None":
    """The ``timescale`` directive the design's testbench declares, or ``None``.

    The instrumented copy must be compiled under the SAME time unit as the testbench that
    drives it. iverilog scopes ``timescale`` by position in the compilation unit and the
    design file is compiled first, so a candidate -- which never writes the directive --
    takes iverilog's 1s default while a ``1ns/1ps`` testbench runs 10^9 times finer. Two
    measured consequences of not matching: every snapshot printed ``time=0`` (so the
    diagnosis reported "time never advanced" for a design that was merely stuck), and the
    injected ``#n $finish`` bound fired 250000x too early on the 10 RTLLM testbenches that
    declare no directive at all, ending the diagnostic run before the first clock edge.

    Reading this is not an oracle leak. :data:`_TIMESCALE_RE` captures the two time literals
    and nothing else -- no expected value, no golden behaviour, no testbench text can come
    out of this function -- and the result reaches the model only as the unit of the ``time=``
    numbers in its own trace.
    """

    match = _TIMESCALE_RE.search(_read_text(design.testbench))
    if not match:
        return None
    return "`timescale %s/%s" % (
        re.sub(r"\s+", "", match.group(1)),
        re.sub(r"\s+", "", match.group(2)),
    )


def instrument_rtl(
    rtl_text: str,
    design_name: str,
    *,
    signals: "Sequence[str] | None" = None,
    time_limit: "int | None" = None,
    timescale: "str | None" = None,
) -> str:
    """Return a NON-SCORED copy of the candidate that prints its own signals.

    The probe is ``$strobe`` inside an ``always`` block over the candidate's own signals,
    not ``$monitor``: ``$monitor`` is a simulator-wide singleton and 8 of the 50 RTLLM
    testbenches already install one, so a ``$monitor`` here would silently lose the race
    (or steal the testbench's) on those designs. ``$strobe`` fires at the end of the time
    slot like ``$monitor`` does, and any number of them may coexist.

    ``time_limit`` additionally injects ``#<n> $finish``, a best-effort bound for a hung
    design (see :data:`DEFAULT_DIAG_TIME_LIMIT` on why it is only best-effort).

    ``timescale`` is the directive to compile this copy under -- pass the testbench's, from
    :func:`testbench_timescale`, so ``$time`` and the injected delay are in the same unit as
    the stimulus. It is undone with ``resetall`` at end of file so it cannot change the
    testbench, which is compiled next. Passing ``None`` emits no directive, which is right
    when the testbench declares none: both then take the same compiler default.

    Returns ``""`` when the module cannot be located, which the caller reports as "no trace
    available" rather than guessing. **The result must never be scored**: it contains system
    tasks that :func:`find_illegal_system_tasks` exists to refuse.
    """

    text = rtl_text or ""
    span = module_span(text, design_name)
    if span is None:
        return ""
    names = (
        tuple(signals) if signals is not None else candidate_trace_signals(text, design_name)
    )

    block = ["", "// ---- rtllm_bench self-instrumentation: NON-SCORED COPY, evidence only ----"]
    if names:
        fields = " ".join("%s=%%b" % name for name in names)
        args = "".join(", %s" % name for name in names)
        # `time=` and not `t=`: `time` is a Verilog keyword, so no candidate signal can
        # collide with the timestamp field and confuse the parser.
        probe = '$strobe("%s time=%%0t %s", $time%s);' % (TRACE_MARKER, fields, args)
        block.append("initial %s" % probe)
        block.append("always @(%s) %s" % (" or ".join(names), probe))
    else:
        block.append(
            'initial $strobe("%s time=%%0t (the candidate declares no traceable signals)", $time);'
            % TRACE_MARKER
        )
    if time_limit is not None:
        block.append("initial begin")
        block.append("    #%d;" % int(time_limit))
        block.append('    $display("%s bounded_stop time=%%0t", $time);' % DIAG_MARKER)
        block.append("    $finish;")
        block.append("end")
    block.append("// ---- end rtllm_bench self-instrumentation ----")

    injected = "\n".join("    " + line if line else "" for line in block) + "\n"
    body = (text[: span.end] + injected + text[span.end :]).rstrip("\n") + "\n"
    if not timescale:
        return body
    # The directive must precede the module to apply to it, and must be undone before the
    # testbench is compiled -- see testbench_timescale.
    return "%s\n%s`resetall\n" % (timescale, body)


@dataclass
class TraceResult:
    """One self-instrumented run: the candidate's own behaviour, and nothing else's.

    Deliberately NOT a :class:`SimResult`. There is no pass/fail on it because the run it
    describes is not a verification -- it cannot be mistaken for one, assigned to a score,
    or merged into a report's totals.
    """

    design: str
    ran: bool
    signals: "tuple[str, ...]"
    trace: str
    timed_out: bool
    compiled: bool
    note: str

    def to_dict(self) -> "dict[str, object]":
        return {
            "design": self.design,
            "ran": self.ran,
            "signals": list(self.signals),
            "trace": self.trace,
            "timed_out": self.timed_out,
            "compiled": self.compiled,
            "note": self.note,
        }


def run_self_trace(
    design: RtllmDesign,
    rtl_text: str,
    workdir: Path,
    *,
    compile_timeout: int = DEFAULT_COMPILE_TIMEOUT,
    sim_timeout: int = DEFAULT_SIM_TIMEOUT,
    apply_shims: bool = True,
    time_limit: "int | None" = None,
    signals: "Sequence[str] | None" = None,
    timescale: "str | None | bool" = True,
) -> TraceResult:
    """Run an instrumented COPY of the candidate and return its own signal trace.

    The scored candidate is not modified and this function never produces a verdict: it
    goes nowhere near :func:`evaluate_rtl`, so no code path can turn its output into a
    score. Only :data:`TRACE_MARKER`/:data:`DIAG_MARKER` lines survive, so the testbench's
    stdout -- which encodes the expected answers -- never reaches the caller.

    Never raises: a broken toolchain comes back as ``ran=False`` with the reason in ``note``.
    """

    workdir = Path(workdir)
    names = (
        tuple(signals) if signals is not None else candidate_trace_signals(rtl_text, design.name)
    )
    # `timescale=True` (the default) means "match the testbench"; a string overrides it and
    # None/False emits no directive.
    directive = testbench_timescale(design) if timescale is True else (timescale or None)
    instrumented = instrument_rtl(
        rtl_text, design.name, signals=names, time_limit=time_limit, timescale=directive
    )
    if not instrumented:
        return TraceResult(
            design=design.name,
            ran=False,
            signals=names,
            trace="",
            timed_out=False,
            compiled=False,
            note=(
                "no trace: could not locate a module named %r in the candidate" % design.name
            ),
        )

    try:
        _prepare_workdir(design, instrumented, workdir, apply_shims)
        compiled = _run(
            [
                "iverilog",
                IVERILOG_STANDARD,
                "-o",
                SIM_BINARY,
                "%s.v" % design.name,
                TESTBENCH_FILE,
            ],
            workdir,
            compile_timeout,
        )
        if compiled.returncode != 0 or not (workdir / SIM_BINARY).is_file():
            return TraceResult(
                design=design.name,
                ran=False,
                signals=names,
                trace="",
                timed_out=compiled.timed_out,
                compiled=False,
                note="no trace: the instrumented copy did not compile (the scored candidate is unaffected)",
            )
        simulated = _run(
            ["vvp", SIM_BINARY],
            workdir,
            sim_timeout,
            output_limit=TRACE_OUTPUT_LIMIT,
            capture_head=TRACE_CAPTURE_HEAD,
        )
    except OSError as exc:  # pragma: no cover - defensive; evidence is best-effort
        return TraceResult(
            design=design.name,
            ran=False,
            signals=names,
            trace="",
            timed_out=False,
            compiled=False,
            note="no trace: %r" % (exc,),
        )

    trace = filter_trace_lines(_join_streams(simulated.stdout, simulated.stderr))
    note = ""
    if not trace.strip():
        note = (
            "the instrumented copy ran but never reached the end of a simulation time slot, "
            "so no snapshot was ever printed -- the signature of a zero-delay combinational "
            "feedback loop"
        )
    return TraceResult(
        design=design.name,
        ran=True,
        signals=names,
        trace=trace,
        timed_out=simulated.timed_out,
        compiled=True,
        note=note,
    )


def filter_trace_lines(text: str) -> str:
    """Keep only the instrumentation's own lines, collapsing consecutive duplicates.

    This filter is the boundary of the strict track. Everything the testbench printed is
    dropped here, so no expected value can leak into self-derived evidence. Duplicates
    collapse because one ``$strobe`` per event on a 20-signal sensitivity list prints the
    same end-of-slot snapshot many times.
    """

    kept: "list[str]" = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if TRACE_MARKER not in stripped and DIAG_MARKER not in stripped:
            continue
        if kept and kept[-1] == stripped:
            continue
        kept.append(stripped)
    return "\n".join(kept)


def trace_digest(
    trace: str,
    *,
    head: int = TRACE_DIGEST_HEAD,
    tail: int = TRACE_DIGEST_TAIL,
    limit: int = TRACE_DIGEST_LIMIT,
) -> str:
    """Compact a trace to the first ``head`` transitions plus a sampled tail, under ``limit``.

    A trace is read front-to-back: the first transitions after reset show whether the design
    ever starts correctly, and an evenly sampled tail shows whether it stays correct without
    quoting a million identical cycles.
    """

    lines = [line for line in (trace or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) <= head + tail:
        selected = list(lines)
        omitted = 0
    else:
        rest = lines[head:]
        step = max(1, (len(rest) + tail - 1) // tail)
        sampled = rest[::step]
        if sampled and sampled[-1] != rest[-1]:
            sampled.append(rest[-1])
        selected = lines[:head] + ["... %d transitions omitted; sampled tail follows ..." % (len(rest) - len(sampled))] + sampled
        omitted = len(rest) - len(sampled)

    text = "\n".join(selected)
    while len(text) > limit and len(selected) > 2:
        # Drop from the middle: both ends carry the signal.
        del selected[len(selected) // 2]
        omitted += 1
        text = "\n".join(selected)
    return text[-limit:] if len(text) > limit else text


class TraceSample(NamedTuple):
    """One end-of-time-slot snapshot parsed out of a trace."""

    time: int
    values: "dict[str, str]"


_TRACE_TIME_RE = re.compile(r"%s\s+time=(\d+)" % re.escape(TRACE_MARKER))
_TRACE_PAIR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_$]*)=([^\s]+)")
_DIAG_TIME_RE = re.compile(r"%s\s+bounded_stop\s+time=(\d+)" % re.escape(DIAG_MARKER))


def bounded_stop_time(trace: str) -> "int | None":
    """Simulation time at which the injected bound fired, if it did.

    An independent witness that time advanced. Snapshots alone cannot tell "simulation time
    never moved" (a zero-delay loop) from "time moved but none of the candidate's signals
    ever changed" (an unclocked design) -- both print exactly one snapshot at t=0, and those
    two faults need opposite repairs.
    """

    match = None
    for match in _DIAG_TIME_RE.finditer(trace or ""):
        pass
    return int(match.group(1)) if match else None


def parse_trace(trace: str) -> "tuple[TraceSample, ...]":
    """Parse ``RTLLM_TRACE time=<n> sig=<bits> ...`` lines into ordered snapshots."""

    samples: "list[TraceSample]" = []
    for line in (trace or "").splitlines():
        match = _TRACE_TIME_RE.search(line)
        if not match:
            continue
        values = dict(_TRACE_PAIR_RE.findall(line[match.end() :]))
        samples.append(TraceSample(int(match.group(1)), values))
    return tuple(samples)


@dataclass
class TimeoutDiagnosis:
    """What a bounded, self-instrumented re-run learned about a hang.

    Exists because a watchdog kill leaves an EMPTY simulation log: under ``--evidence-policy
    logs`` the repair agent is otherwise handed the string "(no captured tool output)" and
    asked to guess. Every field here is derived from the candidate's own signals.
    """

    design: str
    ran: bool
    last_time: "int | None"
    time_advanced: bool
    transitions: int
    stuck: "tuple[str, ...]"
    oscillating: "tuple[str, ...]"
    digest: str
    timed_out: bool
    note: str
    #: Whether the candidate's own signals were ever observed at more than one instant. False
    #: with ``time_advanced`` True means the design is not being clocked at all -- a different
    #: fault from a zero-delay loop, needing a different repair.
    signals_moved: bool = False
    final_values: "dict[str, str]" = field(default_factory=dict)

    def to_dict(self) -> "dict[str, object]":
        return {
            "design": self.design,
            "ran": self.ran,
            "last_time": self.last_time,
            "time_advanced": self.time_advanced,
            "signals_moved": self.signals_moved,
            "transitions": self.transitions,
            "stuck": list(self.stuck),
            "oscillating": list(self.oscillating),
            "final_values": dict(self.final_values),
            "timed_out": self.timed_out,
            "note": self.note,
        }

    def report(self) -> str:
        """The text handed to the repair agent. Self-derived: no oracle, no testbench."""

        lines = [
            "Timeout diagnosis (bounded re-run of an instrumented, NON-SCORED copy of YOUR "
            "module; every value below is your own design's):",
        ]
        if not self.ran:
            lines.append(f"- the diagnostic re-run produced nothing: {self.note or 'unknown reason'}")
            return "\n".join(lines)
        lines.append(
            "- last simulation time reached: %s"
            % (
                "unknown (no snapshot printed)"
                if self.last_time is None
                else "t=%d (in the instrumented copy's time units)" % self.last_time
            )
        )
        lines.append(
            "- did simulation time advance at all: %s" % ("yes" if self.time_advanced else "NO")
        )
        lines.append("- end-of-timestep snapshots observed: %d" % self.transitions)
        if self.stuck:
            lines.append(
                "- signals that settled early and NEVER changed again: %s"
                % ", ".join(self.stuck)
            )
        if self.oscillating:
            lines.append(
                "- signals changing on almost every snapshot (oscillating): %s"
                % ", ".join(self.oscillating)
            )
        if self.final_values:
            lines.append(
                "- final observed values: %s"
                % " ".join("%s=%s" % (n, v) for n, v in self.final_values.items())
            )
        if not self.time_advanced:
            lines.append(
                "- reading: simulation time never advanced. That is a zero-delay loop -- "
                "combinational feedback, or an always block with no edge and no delay."
            )
        elif not self.signals_moved:
            lines.append(
                "- reading: simulation time advanced, but NOT ONE of your module's signals "
                "ever changed after the start. The design is not being driven: check that "
                "every port in your declaration has the exact name the description gives, "
                "and that your logic is sensitive to the clock at all."
            )
        elif self.stuck and not self.oscillating:
            lines.append(
                "- reading: time advanced but the listed signals never moved. A terminating "
                "condition, done/valid pulse, or state transition is unreachable."
            )
        if self.note:
            lines.append("- note: %s" % self.note)
        if self.digest:
            lines.append("")
            lines.append("Self-derived trace of your own signals (first transitions, then a sampled tail):")
            lines.append("```")
            lines.append(self.digest)
            lines.append("```")
        return "\n".join(lines)


def _traced_names(samples: "Sequence[TraceSample]") -> "list[str]":
    names: "list[str]" = []
    for sample in samples:
        for name in sample.values:
            if name not in names:
                names.append(name)
    return names


def _stuck_and_oscillating(
    samples: "Sequence[TraceSample]",
) -> "tuple[tuple[str, ...], tuple[str, ...]]":
    """Split traced signals into settled-and-never-moved-again and churning-every-snapshot.

    "Stuck" is deliberately *constant over the second half of the run*, not *constant over
    the whole run*: the interesting hang is a ``done`` that leaves X at the first clock edge,
    settles to 0, and never rises. Requiring zero changes overall would classify that signal
    as healthy (it changed once) and print nothing about the only signal that matters --
    measured on exactly that design before this rule was widened.
    """

    if len(samples) < 2:
        return (), ()
    stuck: "list[str]" = []
    oscillating: "list[str]" = []
    threshold = max(4, len(samples) // 2)
    tail_start = len(samples) // 2
    for name in _traced_names(samples):
        series = [sample.values[name] for sample in samples if name in sample.values]
        if len(series) < 2:
            continue
        changes = sum(1 for a, b in zip(series, series[1:]) if a != b)
        tail = [sample.values[name] for sample in samples[tail_start:] if name in sample.values]
        if changes >= threshold:
            oscillating.append(name)
        elif len(tail) >= 2 and len(set(tail)) == 1:
            stuck.append(name)
    return tuple(stuck), tuple(oscillating)


def _final_values(samples: "Sequence[TraceSample]") -> "dict[str, str]":
    """Last observed value of every traced signal."""

    final: "dict[str, str]" = {}
    for sample in samples:
        final.update(sample.values)
    return final


def diagnose_timeout(
    design: RtllmDesign,
    rtl_text: str,
    workdir: Path,
    *,
    compile_timeout: int = DEFAULT_COMPILE_TIMEOUT,
    sim_timeout: int = DIAG_WALL_CLOCK_S,
    apply_shims: bool = True,
    time_limit: int = DEFAULT_DIAG_TIME_LIMIT,
) -> TimeoutDiagnosis:
    """Re-run a hung candidate bounded and instrumented, and say what its own signals did.

    Costs exactly one extra short simulation, and only when a design already timed out. The
    result is entirely self-derived, so it is admissible under every evidence policy except
    ``none``.
    """

    trace = run_self_trace(
        design,
        rtl_text,
        workdir,
        compile_timeout=compile_timeout,
        sim_timeout=sim_timeout,
        apply_shims=apply_shims,
        time_limit=time_limit,
    )
    samples = parse_trace(trace.trace)
    times = [sample.time for sample in samples]
    stopped_at = bounded_stop_time(trace.trace)
    if stopped_at is not None:
        times.append(stopped_at)
    stuck, oscillating = _stuck_and_oscillating(samples)
    return TimeoutDiagnosis(
        design=design.name,
        ran=trace.ran,
        last_time=max(times) if times else None,
        time_advanced=bool(times) and max(times) > 0,
        signals_moved=len({s.time for s in samples}) > 1,
        transitions=len(samples),
        stuck=stuck,
        oscillating=oscillating,
        digest=trace_digest(trace.trace),
        timed_out=trace.timed_out,
        note=trace.note,
        final_values=_final_values(samples),
    )


# ---------------------------------------------------------------------------
# oracle-derived behaviour diff (UPPER BOUND -- not comparable to published numbers)
# ---------------------------------------------------------------------------


@dataclass
class BehaviourDiff:
    """Where the candidate's stdout first diverges from the reference RTL's stdout.

    ORACLE-DERIVED. Anything built from this is an upper bound on what an agent can do
    without the answer key, and must be reported on its own track.
    """

    design: str
    ran: bool
    diverged: bool
    line: "int | None"
    expected: "str | None"
    got: "str | None"
    reference_lines: int
    candidate_lines: int
    note: str

    def to_dict(self) -> "dict[str, object]":
        return {
            "design": self.design,
            "ran": self.ran,
            "diverged": self.diverged,
            "line": self.line,
            "expected": self.expected,
            "got": self.got,
            "reference_lines": self.reference_lines,
            "candidate_lines": self.candidate_lines,
            "note": self.note,
        }

    def report(self) -> str:
        """The text handed to the repair agent. Behaviour only -- never the reference RTL."""

        lines = [
            "ORACLE-DERIVED EVIDENCE (upper-bound track -- this run is NOT comparable to a "
            "self-derived or published RTLLM number):",
        ]
        if not self.ran:
            lines.append(f"- no behavioural diff available: {self.note or 'unknown reason'}")
            return "\n".join(lines)
        lines.append(
            "- a known-good implementation of this description was run on the same testbench; "
            "you are being shown WHAT IT PRINTED, never how it is written."
        )
        if not self.diverged:
            lines.append(
                "- the two output streams are identical up to the shorter one, so the "
                "divergence is not visible in stdout (%d reference lines vs %d of yours)."
                % (self.reference_lines, self.candidate_lines)
            )
            if self.note:
                lines.append("- note: %s" % self.note)
            return "\n".join(lines)
        lines.append("- first divergence at output line %d:" % (self.line or 0))
        lines.append("    expected: %s" % (self.expected if self.expected is not None else "(no line -- the reference stopped here)"))
        lines.append("    got:      %s" % (self.got if self.got is not None else "(no line -- your run stopped here)"))
        lines.append(
            "- reference printed %d line(s); your candidate printed %d."
            % (self.reference_lines, self.candidate_lines)
        )
        if self.note:
            lines.append("- note: %s" % self.note)
        return "\n".join(lines)


def _diff_lines(text: str) -> "list[str]":
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def oracle_behaviour_diff(
    design: RtllmDesign,
    candidate_sim_log: str,
    workdir: Path,
    *,
    compile_timeout: int = DEFAULT_COMPILE_TIMEOUT,
    sim_timeout: int = DEFAULT_SIM_TIMEOUT,
    apply_shims: bool = True,
) -> BehaviourDiff:
    """Diff the candidate's stdout against the reference RTL's on the same testbench.

    **Hands over behaviour only.** The reference source is written into ``workdir`` so it can
    be simulated and is never read back into the returned object: the caller receives a line
    number and two stdout lines. That is still oracle-derived -- a testbench line often names
    the expected value -- which is why every consumer stamps the result.

    Never raises: a missing reference or a broken toolchain comes back as ``ran=False``.
    """

    reference = reference_rtl_text(design)
    if not reference.strip():
        return BehaviourDiff(
            design=design.name,
            ran=False,
            diverged=False,
            line=None,
            expected=None,
            got=None,
            reference_lines=0,
            candidate_lines=len(_diff_lines(candidate_sim_log)),
            note="the benchmark ships no reference RTL for this design",
        )

    reference_sim = evaluate_rtl(
        design,
        reference,
        Path(workdir),
        compile_timeout=compile_timeout,
        sim_timeout=sim_timeout,
        apply_shims=apply_shims,
    )
    expected_lines = _diff_lines(reference_sim.sim_log)
    got_lines = _diff_lines(candidate_sim_log)
    note = ""
    if not reference_sim.syntax_pass:
        note = "the reference RTL itself did not compile here, so the comparison is unreliable"
    elif not reference_sim.func_pass:
        note = (
            "the reference RTL does not pass this testbench either (a known upstream oracle "
            "defect), so a matching output would not score"
        )

    for index in range(max(len(expected_lines), len(got_lines))):
        expected = expected_lines[index] if index < len(expected_lines) else None
        got = got_lines[index] if index < len(got_lines) else None
        if expected == got:
            continue
        return BehaviourDiff(
            design=design.name,
            ran=True,
            diverged=True,
            line=index + 1,
            expected=None if expected is None else expected[:DIFF_LINE_CHARS],
            got=None if got is None else got[:DIFF_LINE_CHARS],
            reference_lines=len(expected_lines),
            candidate_lines=len(got_lines),
            note=note,
        )

    return BehaviourDiff(
        design=design.name,
        ran=True,
        diverged=False,
        line=None,
        expected=None,
        got=None,
        reference_lines=len(expected_lines),
        candidate_lines=len(got_lines),
        note=note,
    )
