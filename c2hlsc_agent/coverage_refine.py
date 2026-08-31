"""Coverage-driven stimulus refinement — the shift-left loop's feedback edge.

Collecting coverage is not the same as using it. This module closes the loop the
HLS-LeVeri work is built around: measure concrete structural coverage of the golden C and
the generated HLS-C, find what the current stimulus schedule never reaches, obtain inputs
that reach it, fold those inputs back into the testbenches as additional directed cases,
and measure again — until a coverage target is met, nothing improves, or the round budget
runs out.

Two strategies, tried in that order:

``klee``
    The real mechanism. KLEE explores the golden top symbolically and writes one
    ``.ktest`` per path it reaches; each is a concrete input assignment. Those become
    :class:`~c2hlsc_agent.stimulus.ExtraVector` entries replayed ahead of the directed
    schedule, so a branch the random stream could never hit (``x == 424242``) becomes a
    permanent, reproducible test case.

``widen``
    The honest fallback for machines without KLEE — notably macOS, where KLEE has no
    native package. It simply grows the pseudo-random schedule. It cannot reach a guarded
    equality branch, and the report says so rather than implying the loop converged.

Everything here is bounded: rounds, added vectors, and total tests all have caps, and a
round that does not improve the gate coverage ends the loop.
"""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyze import AnalysisResult
from .config import AgentConfig
from .convert import GeneratedSource
from .hls_project import write_project
from .hls_runner import host_target
from .stimulus import ExtraVector

REFINEMENT_REPORT = "coverage_refinement.json"
KTEST_MAGICS = (b"KTEST", b"BOUT\n")

#: Guardrails. Refinement adds test cases to every future run of the project, so it must
#: not be able to grow a testbench without bound.
MAX_ROUNDS = 5
MAX_VECTORS = 64
MAX_TESTS = 4096


class RefinementError(RuntimeError):
    """The refinement loop cannot run at all (missing project, unreadable report)."""


@dataclass
class RefinementRound:
    round: int
    strategy: str
    line_coverage: float | None
    branch_coverage: float | None
    gate_coverage: float | None
    new_vectors: int
    num_tests: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "strategy": self.strategy,
            "line_coverage": self.line_coverage,
            "branch_coverage": self.branch_coverage,
            "gate_coverage": self.gate_coverage,
            "new_vectors": self.new_vectors,
            "num_tests": self.num_tests,
            "note": self.note,
        }


@dataclass
class RefinementOutcome:
    status: str = "no_progress"
    target: float | None = None
    baseline_coverage: float | None = None
    final_coverage: float | None = None
    rounds: list[RefinementRound] = field(default_factory=list)
    vectors: list[ExtraVector] = field(default_factory=list)
    uncovered_lines: list[dict[str, Any]] = field(default_factory=list)
    uncovered_branches: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target,
            "baseline_coverage": self.baseline_coverage,
            "final_coverage": self.final_coverage,
            "rounds": [item.to_dict() for item in self.rounds],
            "vectors": [vector.to_dict() for vector in self.vectors],
            "uncovered_lines": self.uncovered_lines,
            "uncovered_branches": self.uncovered_branches,
            "summary": self.summary,
        }


# --------------------------------------------------------------------------- #
# KLEE .ktest decoding
# --------------------------------------------------------------------------- #


def _read_u32(handle) -> int:
    raw = handle.read(4)
    if len(raw) != 4:
        raise ValueError("truncated ktest header")
    return struct.unpack(">I", raw)[0]


def parse_ktest(path: Path) -> dict[str, bytes]:
    """Decode a KLEE ``.ktest`` into ``{symbolic object name: raw bytes}``.

    The format is a big-endian header followed by the program arguments and then the
    symbolic objects, each a length-prefixed name and a length-prefixed byte blob. Only
    the objects matter here: they are the concrete input assignment KLEE found.
    """

    with path.open("rb") as handle:
        magic = handle.read(5)
        if magic not in KTEST_MAGICS:
            raise ValueError(f"{path}: not a ktest file (magic {magic!r})")
        _read_u32(handle)  # version
        for _ in range(_read_u32(handle)):  # argv
            handle.read(_read_u32(handle))
        _read_u32(handle)  # symArgvs
        _read_u32(handle)  # symArgvLen
        objects: dict[str, bytes] = {}
        for _ in range(_read_u32(handle)):
            name = handle.read(_read_u32(handle)).decode("utf-8", errors="replace")
            objects[name] = handle.read(_read_u32(handle))
    return objects


def _element_bytes(c_type: str) -> int:
    text = c_type.replace("const", "").replace("volatile", "").strip()
    for token, width in (
        ("int8", 1), ("uint8", 1), ("char", 1),
        ("int16", 2), ("uint16", 2), ("short", 2),
        ("int64", 8), ("uint64", 8), ("long long", 8), ("double", 8),
        ("int32", 4), ("uint32", 4), ("float", 4),
    ):
        if token in text:
            return width
    return 4  # plain int and anything unrecognized


def _is_unsigned(c_type: str) -> bool:
    text = c_type.lower()
    return "unsigned" in text or text.strip().startswith("uint") or "ap_uint" in text


def ktest_to_vector(objects: dict[str, bytes], analysis: AnalysisResult, origin: str) -> ExtraVector | None:
    """Turn one decoded ktest into an input vector for the testbench generators.

    KLEE stores the raw memory of each symbolic object, so an array object is decoded as
    ``length`` little-endian elements of the argument's own width and signedness. An
    object KLEE did not produce (an output buffer, say) is simply absent and the
    generators fall back to zero for it.
    """

    values: dict[str, Any] = {}
    for arg in analysis.function.args:
        blob = objects.get(arg.name)
        if blob is None:
            continue
        width = _element_bytes(arg.c_type)
        signed = not _is_unsigned(arg.c_type)
        if arg.is_pointer_like:
            length = arg.length or max(1, len(blob) // width)
            elements = []
            for index in range(length):
                chunk = blob[index * width : (index + 1) * width]
                if len(chunk) < width:
                    break
                elements.append(int.from_bytes(chunk, "little", signed=signed))
            if elements:
                values[arg.name] = elements
        else:
            chunk = blob[:width]
            if len(chunk) == width:
                value = int.from_bytes(chunk, "little", signed=signed)
                if arg.scalar_range:
                    lo, hi = arg.scalar_range
                    value = max(lo, min(hi, value))
                values[arg.name] = value
    if not values:
        return None
    return ExtraVector(values=values, origin=origin)


def collect_ktests(project_dir: Path) -> list[Path]:
    klee_out = project_dir / "coverage" / "klee-out"
    if not klee_out.exists():
        return []
    return sorted(klee_out.glob("*.ktest"))


# --------------------------------------------------------------------------- #
# Running the measurement targets
# --------------------------------------------------------------------------- #


def _run_target(project_dir: Path, target: str, timeout: int = 900) -> subprocess.CompletedProcess:
    # host_target(), not make: refinement has to work wherever verification works,
    # including native Windows.
    return subprocess.run(
        host_target(target),
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def measure_coverage(project_dir: Path) -> dict[str, Any]:
    """Run the gcov target and return its parsed report (empty when unavailable)."""

    try:
        _run_target(project_dir, "gcov-coverage")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "reason": str(exc)}
    return _read_json(project_dir / "coverage" / "gcov_report.json")


def run_symbolic(project_dir: Path) -> dict[str, Any]:
    """Run the KLEE target and return its parsed report (empty when unavailable)."""

    try:
        _run_target(project_dir, "klee-coverage")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "reason": str(exc)}
    return _read_json(project_dir / "coverage" / "klee_report.json")


def gate_coverage(report: dict[str, Any]) -> float | None:
    """The weaker of line and branch coverage — the number the loop drives upward."""

    observed = [
        value
        for value in (report.get("line_coverage"), report.get("branch_coverage"))
        if isinstance(value, (int, float))
    ]
    return min(observed) if observed else None


# --------------------------------------------------------------------------- #
# Regeneration
# --------------------------------------------------------------------------- #


def regenerate(project_dir: Path, analysis: AnalysisResult, config: AgentConfig) -> None:
    """Rewrite the project's testbenches with the current stimulus configuration.

    The design on disk is read back and written out unchanged: refinement adds test cases,
    it never touches ``src/hls_top.cpp``. A repaired or optimized design must survive a
    refinement round untouched, or the loop would quietly undo work the verifier accepted.
    """

    header_path = project_dir / "src" / "hls_top.hpp"
    source_path = project_dir / "src" / "hls_top.cpp"
    if not source_path.exists():
        raise RefinementError(f"{project_dir} has no src/hls_top.cpp to preserve")
    generated = GeneratedSource(
        header=header_path.read_text(encoding="utf-8") if header_path.exists() else "",
        source=source_path.read_text(encoding="utf-8"),
        transformations=["Preserved on disk across a coverage-refinement round."],
    )
    write_project(project_dir, analysis, generated, config)


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def refine_project(
    project_dir: Path,
    analysis: AnalysisResult,
    config: AgentConfig,
    target: float | None = None,
    max_rounds: int = MAX_ROUNDS,
    max_vectors: int = MAX_VECTORS,
    allow_widen: bool = True,
    verbose: bool = False,
) -> RefinementOutcome:
    """Drive structural coverage up by feeding what it misses back into the stimulus."""

    if not (project_dir / "Makefile").exists():
        raise RefinementError(f"{project_dir} does not look like a generated project (no Makefile)")

    outcome = RefinementOutcome(target=target)
    baseline = measure_coverage(project_dir)
    if baseline.get("status") == "skipped":
        outcome.status = "blocked"
        outcome.summary = (
            f"Coverage tooling is unavailable ({baseline.get('reason', 'unknown')}); "
            "run `c2hlsc-agent doctor --install` and retry."
        )
        _write_report(project_dir, outcome)
        return outcome

    outcome.baseline_coverage = gate_coverage(baseline)
    outcome.final_coverage = outcome.baseline_coverage
    outcome.uncovered_lines = list(baseline.get("uncovered_lines") or [])
    outcome.uncovered_branches = list(baseline.get("uncovered_branches") or [])
    if verbose:
        print(f"baseline gate coverage: {outcome.baseline_coverage}")

    if target is not None and outcome.baseline_coverage is not None and outcome.baseline_coverage >= target:
        outcome.status = "met"
        outcome.summary = f"Baseline coverage {outcome.baseline_coverage:.2f}% already meets the {target:.2f}% target."
        _write_report(project_dir, outcome)
        return outcome

    # A single flat round is not proof that refinement is finished -- widening is
    # probabilistic, and a schedule change can unlock code on the next pass. Stop after
    # two CONSECUTIVE rounds that fail to move the number.
    stall_limit = 2
    stall = 0
    seen_vectors: set[str] = {
        hashlib.sha256(json.dumps(vector.to_dict()["values"], sort_keys=True).encode()).hexdigest()
        for vector in outcome.vectors
    }
    vectors: list[ExtraVector] = list(getattr(config, "extra_vectors", None) or [])
    current = outcome.baseline_coverage

    for round_no in range(max(1, max_rounds)):
        strategy = "klee"
        new_vectors: list[ExtraVector] = []
        note = ""

        klee_report = run_symbolic(project_dir)
        if klee_report.get("status") in {"skipped", "error"} or not collect_ktests(project_dir):
            reason = klee_report.get("reason") or klee_report.get("status") or "no ktests produced"
            if not allow_widen:
                outcome.status = "blocked"
                outcome.summary = (
                    f"Symbolic exploration unavailable ({reason}); "
                    "run `c2hlsc-agent doctor --install`, or allow the widening fallback."
                )
                break
            strategy = "widen"
            note = f"KLEE unavailable ({reason}); widened the pseudo-random schedule instead"
        else:
            for path in collect_ktests(project_dir):
                try:
                    objects = parse_ktest(path)
                except (OSError, ValueError, struct.error):
                    continue
                vector = ktest_to_vector(objects, analysis, origin=f"klee:{path.name}")
                if vector is None:
                    continue
                digest = hashlib.sha256(
                    json.dumps(vector.values, sort_keys=True, default=str).encode()
                ).hexdigest()
                if digest in seen_vectors:
                    continue
                seen_vectors.add(digest)
                new_vectors.append(vector)
                if len(vectors) + len(new_vectors) >= max_vectors:
                    note = f"vector budget reached ({max_vectors})"
                    break

        if strategy == "klee" and not new_vectors:
            # KLEE re-runs the same driver, so a dry round stays dry. Fall through to
            # widening rather than stopping: a different schedule can still reach code
            # symbolic execution has already enumerated.
            if not allow_widen:
                outcome.status = "no_progress"
                outcome.summary = (
                    f"Round {round_no}: symbolic exploration produced no input the schedule "
                    "does not already cover, and widening is disabled."
                )
                break
            strategy = "widen"
            note = "symbolic exploration produced nothing new; widened the schedule instead"

        if strategy == "widen":
            if config.num_tests >= MAX_TESTS:
                outcome.status = "exhausted"
                outcome.summary = f"Random schedule is already at the {MAX_TESTS}-test cap."
                break
            config.num_tests = min(MAX_TESTS, config.num_tests * 2)
        else:
            vectors.extend(new_vectors)
            config.extra_vectors = list(vectors)

        regenerate(project_dir, analysis, config)
        report = measure_coverage(project_dir)
        gate = gate_coverage(report)
        outcome.rounds.append(
            RefinementRound(
                round=round_no,
                strategy=strategy,
                line_coverage=report.get("line_coverage"),
                branch_coverage=report.get("branch_coverage"),
                gate_coverage=gate,
                new_vectors=len(new_vectors),
                num_tests=config.num_tests,
                note=note,
            )
        )
        outcome.final_coverage = gate
        outcome.uncovered_lines = list(report.get("uncovered_lines") or [])
        outcome.uncovered_branches = list(report.get("uncovered_branches") or [])
        if verbose:
            print(f"round {round_no} [{strategy}]: gate coverage {gate} (+{len(new_vectors)} vector(s))")

        if target is not None and gate is not None and gate >= target:
            outcome.status = "met"
            outcome.summary = f"Reached {gate:.2f}% gate coverage in {round_no + 1} round(s) (target {target:.2f}%)."
            break
        improved = gate is not None and current is not None and gate > current
        if improved:
            stall = 0
            current = gate
            continue
        stall += 1
        if stall >= stall_limit:
            outcome.status = "no_progress"
            shown = f"{current:.2f}%" if current is not None else "n/a"
            reached = f"{gate:.2f}%" if gate is not None else "n/a"
            outcome.summary = (
                f"{stall} consecutive round(s) failed to improve gate coverage "
                f"({shown} -> {reached}); stopping."
            )
            break
    else:
        outcome.status = "exhausted"
        outcome.summary = f"Round budget ({max_rounds}) spent; gate coverage {outcome.final_coverage}."

    if not outcome.summary:
        outcome.summary = f"Refinement finished with status {outcome.status}."
    outcome.vectors = list(vectors)
    _write_report(project_dir, outcome)
    return outcome


def _write_report(project_dir: Path, outcome: RefinementOutcome) -> None:
    (project_dir / REFINEMENT_REPORT).write_text(
        json.dumps(outcome.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
