from __future__ import annotations

import os
import signal
import subprocess
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PhaseResult:
    name: str
    status: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    log_path: Path | None = None
    summary: str = ""
    #: How many values this phase actually compared, when it reports one. ``None`` means
    #: the phase does not report a count, not that it compared nothing -- only a phase that
    #: emits its own count can be held to it. This exists because `pass` was defined as the
    #: absence of a failure: without a quantity here, a phase that compared 3600 elements
    #: and one that compared zero were indistinguishable at the point the verdict is formed.
    comparisons: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "returncode": self.returncode,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "log_path": str(self.log_path) if self.log_path else None,
            "summary": self.summary,
            "comparisons": self.comparisons,
        }

    @property
    def is_vacuous(self) -> bool:
        """A phase that reports a count of zero has not agreed with anything."""

        return self.comparisons == 0


@dataclass
class Mismatch:
    test_index: int
    argument: str
    expected: str
    actual: str
    element_index: int | None = None
    seed: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "test_index": self.test_index,
            "argument": self.argument,
            "element_index": self.element_index,
            "expected": self.expected,
            "actual": self.actual,
            "seed": self.seed,
        }


def format_mismatch(mismatch: Mismatch) -> str:
    index = "" if mismatch.element_index is None else f"[{mismatch.element_index}]"
    seed = "" if mismatch.seed is None else f" seed={mismatch.seed}"
    return (
        f"test={mismatch.test_index} {mismatch.argument}{index}: "
        f"expected={mismatch.expected} actual={mismatch.actual}{seed}"
    )


def parse_mismatches(text: str) -> list[Mismatch]:
    mismatches: list[Mismatch] = []
    array_pattern = re.compile(
        r"Mismatch test=(?P<test>\d+)\s+arg=(?P<arg>\w+)\s+index=(?P<index>\d+)\s+"
        r"expected=(?P<expected>\S+)\s+actual=(?P<actual>\S+)\s+seed=(?P<seed>\d+)"
    )
    return_pattern = re.compile(
        r"Mismatch test=(?P<test>\d+)\s+return\s+expected=(?P<expected>\S+)\s+"
        r"actual=(?P<actual>\S+)\s+seed=(?P<seed>\d+)"
    )
    for match in array_pattern.finditer(text):
        mismatches.append(
            Mismatch(
                test_index=int(match.group("test")),
                argument=match.group("arg"),
                element_index=int(match.group("index")),
                expected=match.group("expected"),
                actual=match.group("actual"),
                seed=int(match.group("seed")),
            )
        )
    for match in return_pattern.finditer(text):
        mismatches.append(
            Mismatch(
                test_index=int(match.group("test")),
                argument="return",
                expected=match.group("expected"),
                actual=match.group("actual"),
                seed=int(match.group("seed")),
            )
        )
    return mismatches


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Ask a timed-out command and its children to stop.

    On POSIX the process group does it. On Windows there are no process groups in that
    sense, so taskkill /T is the equivalent -- without it a timed-out compiler or
    simulator leaves orphaned children holding the output files open.
    """

    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T"], capture_output=True, check=False)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (AttributeError, ProcessLookupError, PermissionError):
        proc.kill()


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        if proc.poll() is None:
            proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError):
        proc.kill()


def run_command(command: list[str], cwd: Path, phase: str, timeout: int = 120) -> PhaseResult:
    timeout_s = timeout
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as timeout:
        _terminate_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            stdout, stderr = proc.communicate()
        log_path = cwd / f"{phase}.log"
        log_path.write_text((stdout or "") + "\n--- stderr ---\n" + (stderr or ""), encoding="utf-8")
        # Re-raise enriched with the partial output, chained to the original so the
        # evidence trail shows this came from the watchdog rather than a fresh fault.
        raise subprocess.TimeoutExpired(command, timeout_s, output=stdout, stderr=stderr) from timeout
    stdout = stdout or ""
    stderr = stderr or ""
    status = "pass" if proc.returncode == 0 else "fail"
    log_path = cwd / f"{phase}.log"
    log_path.write_text(stdout + "\n--- stderr ---\n" + stderr, encoding="utf-8")
    comparisons = parse_comparisons(stdout)
    # A phase that says it compared nothing cannot be a pass, whatever its exit code. The
    # generated benches guard this themselves; this is the same rule held one level up, so
    # a bench that loses its guard cannot quietly take the ladder with it.
    if status == "pass" and comparisons == 0:
        status = "fail"
    return PhaseResult(phase, status, proc.returncode, stdout, stderr, log_path, comparisons=comparisons)


#: Every tier reports what it examined in one of these shapes; the count is the evidence.
_COMPARISON_PATTERNS = (
    re.compile(r"compared (\d+) value\(s\)"),          # oracle testbench
    re.compile(r"(\d+) value\(s\) compared"),          # paired-trace comparator
    re.compile(r"RTL_TB: COMPARED (\d+)"),             # direct-RTL testbench
)


def parse_comparisons(stdout: str) -> int | None:
    """The number of values a phase reports having compared, or None if it reports none.

    None and 0 mean different things and must not be conflated: None is "this phase does
    not report a count", 0 is "this phase ran and examined nothing".
    """

    for pattern in _COMPARISON_PATTERNS:
        found = pattern.search(stdout)
        if found:
            return int(found.group(1))
    return None


@dataclass
class VerificationState:
    phases: dict[str, PhaseResult] = field(default_factory=dict)
    mismatches: list[Mismatch] = field(default_factory=list)

    def add_phase(self, result: PhaseResult) -> None:
        self.phases[result.name] = result

    def status_for(self, phase: str) -> str:
        return self.phases.get(phase, PhaseResult(phase, "skipped")).status
