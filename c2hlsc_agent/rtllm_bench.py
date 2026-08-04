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
    time out. This is the number to trust when comparing agents; ``func_pass`` is the
    number to quote when comparing against published RTLLM results.

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
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

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
)

#: Designs whose oracle is broken independently of the RTL under test. Measured on this
#: checkout by running the benchmark's own ``verified_*.v`` through its own testbench.
KNOWN_ORACLE_ISSUES = {
    "clkgenerator": (
        "Upstream oracle bug: the benchmark's own verified_clkgenerator.v fails its own "
        "testbench under iverilog (20 'Failed at' lines, no pass banner), so no RTL can score."
    ),
    "radix2_div": (
        "Upstream oracle bug: the benchmark's own verified_radix2_div.v fails its own "
        "testbench under iverilog (3 'Error: dividend=...' lines), so no RTL can score."
    ),
    "ring_counter": (
        "Simulator-ordering bug: the testbench's two always @(posedge clk) blocks race, and "
        "iverilog runs the 'i = i + 1' block before the 'if (i == 9)' pass check, so the "
        "banner never prints -- the reference RTL matches all 10 expected values yet scores 0."
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


def classify_output(text: str, timed_out: bool = False) -> "tuple[bool, bool]":
    """Return ``(official, strict)`` verdicts for simulator stdout.

    ``official`` is byte-for-byte the benchmark rule from ``auto_run.py``: the word
    ``Pass`` or ``pass`` appears anywhere. ``strict`` additionally demands that no
    failure marker was printed and that the run finished on its own.

    A testbench that prints ``===========Failed===========`` and then somewhere else the
    word "pass" therefore yields ``(True, False)`` -- callers must report the two numbers
    separately rather than picking one.
    """

    text = text or ""
    official = any(marker in text for marker in PASS_MARKERS)
    strict = official and not timed_out and not has_failure_marker(text)
    return official, strict


def classify_failure(
    compile_log: str,
    sim_log: str,
    syntax_pass: bool,
    timed_out: bool,
) -> "str | None":
    """Bucket one run into a ``FAILURE_FAMILIES`` label, or ``None`` if it truly passed.

    "Truly passed" means the *strict* oracle: a run that reaches a pass banner but also
    printed ``Failed at ...`` is reported as ``functional_mismatch`` so the repair agent
    still sees actionable evidence. Use ``SimResult.func_pass`` for the headline metric.
    """

    compile_log = compile_log or ""
    sim_log = sim_log or ""

    if not syntax_pass:
        if "Unknown module type" in compile_log:
            return "missing_module"
        if "sorry:" in compile_log.lower():
            return "simulator_unsupported"
        if _looks_like_port_mismatch(compile_log):
            return "port_mismatch"
        return "compile_error"

    _, strict = classify_output(sim_log, timed_out)
    if strict:
        return None
    if timed_out:
        return "timeout"
    if "sorry:" in sim_log.lower():
        return "simulator_unsupported"
    if _looks_like_missing_golden_data(sim_log):
        return "missing_golden_data"
    if not sim_log.strip():
        return "no_output"
    return "functional_mismatch"


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


def _tail(text: str, limit: int = LOG_TAIL_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return "...[%d chars truncated]...\n" % (len(text) - limit) + text[-limit:]


def _run(command: "list[str]", cwd: Path, timeout: int) -> "tuple[int | None, str, str, bool]":
    """Run ``command`` capturing stdout/stderr, killing the whole process group on timeout.

    Returns ``(returncode, stdout, stderr, timed_out)``. Never raises: a missing binary or
    an OS error comes back as ``returncode=None`` with the reason on stderr. Partial
    output produced before a timeout is preserved.
    """

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return None, "", "failed to launch %s: %s" % (command[0], exc), False

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout or "", stderr or "", False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - the SIGKILL path
            _kill_group(proc, signal.SIGKILL)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        return proc.returncode, stdout or "", stderr or "", True
    except Exception as exc:  # pragma: no cover - defensive
        _kill_group(proc, signal.SIGKILL)
        return None, "", "error while running %s: %s" % (command[0], exc), False


def _kill_group(proc: "subprocess.Popen[str]", sig: int = signal.SIGTERM) -> None:
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
) -> SimResult:
    """Compile and simulate ``rtl_text`` against ``design``'s testbench in ``workdir``.

    Thread-safe as long as each concurrent call gets its own ``workdir``: no module state
    is mutated and every tool runs with ``cwd=workdir``. Never raises -- a broken toolchain
    comes back as ``syntax_pass=False`` with the reason in ``compile_log``.
    """

    workdir = Path(workdir)
    started = time.time()
    shim_applied = False
    try:
        shim_applied = _prepare_workdir(design, rtl_text, workdir, apply_shims)
    except OSError as exc:
        return SimResult(
            design=design.name,
            syntax_pass=False,
            func_pass=False,
            func_pass_strict=False,
            timed_out=False,
            compile_log="failed to prepare sandbox %s: %s" % (workdir, exc),
            sim_log="",
            duration_s=time.time() - started,
            failure_family="compile_error",
            shim_applied=False,
        )

    compile_cmd = [
        "iverilog",
        IVERILOG_STANDARD,
        "-o",
        SIM_BINARY,
        "%s.v" % design.name,
        TESTBENCH_FILE,
    ]
    code, out, err, compile_timed_out = _run(compile_cmd, workdir, compile_timeout)
    compile_log = _join_streams(out, err)
    binary = workdir / SIM_BINARY
    syntax_pass = code == 0 and binary.is_file() and not compile_timed_out

    sim_log = ""
    sim_timed_out = False
    func_pass = func_pass_strict = False
    if syntax_pass:
        code, out, err, sim_timed_out = _run(["vvp", SIM_BINARY], workdir, sim_timeout)
        # stdout and stderr are judged together so that re-running classify_output /
        # classify_failure on the stored SimResult reproduces the verdict exactly. vvp
        # writes $display to stdout, so this matches the benchmark's `make sim > out.txt`.
        sim_log = _join_streams(out, err)
        func_pass, func_pass_strict = classify_output(sim_log, sim_timed_out)

    timed_out = compile_timed_out or sim_timed_out

    failure_family = classify_failure(compile_log, sim_log, syntax_pass, timed_out)

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
