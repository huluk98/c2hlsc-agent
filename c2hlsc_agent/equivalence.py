from __future__ import annotations

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

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "returncode": self.returncode,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "log_path": str(self.log_path) if self.log_path else None,
            "summary": self.summary,
        }


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


def run_command(command: list[str], cwd: Path, phase: str, timeout: int = 120) -> PhaseResult:
    proc = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    status = "pass" if proc.returncode == 0 else "fail"
    log_path = cwd / f"{phase}.log"
    log_path.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr, encoding="utf-8")
    return PhaseResult(phase, status, proc.returncode, proc.stdout, proc.stderr, log_path)


@dataclass
class VerificationState:
    phases: dict[str, PhaseResult] = field(default_factory=dict)
    mismatches: list[Mismatch] = field(default_factory=list)

    def add_phase(self, result: PhaseResult) -> None:
        self.phases[result.name] = result

    def status_for(self, phase: str) -> str:
        return self.phases.get(phase, PhaseResult(phase, "skipped")).status
