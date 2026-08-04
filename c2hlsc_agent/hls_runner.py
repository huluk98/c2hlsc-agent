from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .equivalence import PhaseResult, VerificationState, parse_mismatches, run_command
from .remote import RemoteVitis
from .vitis_command import find_vitis_executable, vitis_tcl_command


SHIFT_LEFT_PHASES = ("shift_left_trace", "coverage_gcov", "symbolic_klee")
PHASE_ORDER = ("software_equivalence", *SHIFT_LEFT_PHASES, "csim", "csynth", "cosim")
PHASE_TIMEOUTS = {"csim": 600, "csynth": 1200, "cosim": 600}
# Runner-authored timeout summaries use this phrase.  ``agent_loop`` keys on the
# summary (not arbitrary tool output) so a killed process is treated as missing
# infrastructure evidence rather than proof that the current HLS-C is defective.
TOOL_TIMEOUT_MARKER = "timed out after"
_RELATIONAL_KLEE_SCHEMA = "c2hlsc-klee-report-v1"
_RELATIONAL_KLEE_SCOPE = "golden_hlsc_relational"
_RELATIONAL_KLEE_NAME_RE = re.compile(
    r"^C2HLSC_RELATIONAL_MISMATCH:(?:return|[A-Za-z_][A-Za-z0-9_]*)$"
)
_RELATIONAL_KLEE_ARTIFACTS = {
    "input.c",
    "src/hls_top.hpp",
    "src/hls_top.cpp",
    "tb/klee_driver.cpp",
    "tb/leveri_manifest.json",
}


def _has_exact_relational_model_metadata(metadata: dict[str, object]) -> bool:
    assumptions = metadata.get("assumptions")
    hashes = metadata.get("artifact_sha256")
    return (
        metadata.get("invocations") == 1
        and type(metadata.get("observable_count")) is int
        and metadata["observable_count"] > 0
        and isinstance(metadata.get("top"), str)
        and bool(metadata["top"])
        and isinstance(assumptions, dict)
        and assumptions.get("pointer_alias_model") == "distinct_pointer_arguments"
        and assumptions.get("hidden_state_model") == "no_mutable_hidden_state"
        and assumptions.get("comparison") == "return_and_complete_pointer_post_state"
        and isinstance(hashes, dict)
        and set(hashes) == _RELATIONAL_KLEE_ARTIFACTS
    )


def earliest_failing_phase(state: VerificationState, run_vitis_requested: bool) -> str | None:
    required = ["software_equivalence"]
    required.extend(phase for phase in SHIFT_LEFT_PHASES if state.status_for(phase) == "fail")
    if run_vitis_requested:
        required.extend(["csim", "csynth", "cosim"])
    for phase in required:
        if state.status_for(phase) != "pass":
            return phase
    return None


def _timeout_result(project_dir: Path, phase: str, exc: subprocess.TimeoutExpired, label: str) -> PhaseResult:
    """Build a timeout PhaseResult that keeps the partial evidence.

    ``run_command`` writes the partial log to ``<phase>.log`` before raising, so the
    repair agent still gets the log tail instead of a bare one-line summary.
    """

    log_path = project_dir / f"{phase}.log"
    return PhaseResult(
        phase,
        "fail",
        stdout=str(exc.output or ""),
        stderr=str(exc.stderr or ""),
        log_path=log_path if log_path.exists() else None,
        summary=(
            f"{label} {TOOL_TIMEOUT_MARKER} {exc.timeout}s "
            "(killed at the deadline; no equivalence verdict was reached)"
        ),
    )


def run_software_equivalence(project_dir: Path, verbose: bool = False) -> PhaseResult:
    try:
        result = run_command(["make", "test"], project_dir, "software_equivalence", timeout=120)
    except FileNotFoundError:
        return PhaseResult(
            "software_equivalence",
            "fail",
            summary="make not found on PATH — install the host build toolchain (make plus a C++17 compiler)",
        )
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(project_dir, "software_equivalence", exc, "host equivalence")
    if verbose and result.stdout:
        print(result.stdout)
    return _gate_equivalence_on_evidence(result)


def _run_make_phase(
    project_dir: Path,
    target: str,
    phase: str,
    timeout: int,
    verbose: bool,
) -> PhaseResult:
    try:
        result = run_command(["make", target], project_dir, phase, timeout=timeout)
    except FileNotFoundError:
        return PhaseResult(phase, "fail", summary="make not found")
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(project_dir, phase, exc, target)
    if verbose and result.stdout:
        print(result.stdout)
    return result


def _report_backed_phase(
    project_dir: Path,
    target: str,
    phase: str,
    report_name: str,
    timeout: int,
    verbose: bool,
) -> PhaseResult:
    """Run a generated coverage target and take its JSON verdict as authority.

    The generated runners deliberately exit zero for an unavailable optional tool so
    normal conversion can continue. Preserve that as an explicit ``skipped`` phase;
    never convert it into a false pass. A missing or malformed report after exit zero is
    a harness failure because there is no evidence supporting the command's outcome.
    """

    report_path = project_dir / "coverage" / report_name
    # A prior run's PASS/SKIP must never certify this run. The generated target owns
    # this report and must recreate it every time before its verdict can be parsed.
    report_path.unlink(missing_ok=True)
    result = _run_make_phase(project_dir, target, phase, timeout, verbose)
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if result.status != "pass":
            return result
        return PhaseResult(
            phase,
            "fail",
            result.returncode,
            result.stdout,
            result.stderr,
            result.log_path,
            summary=f"{target} exited 0 but {report_path.relative_to(project_dir)} is unavailable or invalid: {exc}",
        )
    if not isinstance(payload, dict):
        return PhaseResult(
            phase,
            "fail",
            result.returncode,
            result.stdout,
            result.stderr,
            result.log_path,
            summary=f"{report_name} must contain a JSON object",
        )
    status = str(payload.get("status", "")).lower()
    reason = str(payload.get("reason") or payload.get("stage") or "")
    counterexample_names = payload.get("counterexample_names")
    if not isinstance(counterexample_names, list):
        counterexample_names = []
    counterexample_names = sorted(
        {
            name
            for name in counterexample_names
            if isinstance(name, str) and _RELATIONAL_KLEE_NAME_RE.fullmatch(name)
        }
    )
    metadata = {
        key: payload[key]
        for key in (
            "schema",
            "scope",
            "outcome",
            "failure_kind",
            "completed_paths",
            "generated_tests",
            "timed_out",
            "invocations",
            "observable_count",
            "top",
        )
        if key in payload and isinstance(payload[key], (str, int, bool, type(None)))
    }
    bounded_lengths = payload.get("bounded_lengths")
    if isinstance(bounded_lengths, dict):
        metadata["bounded_lengths"] = {
            str(name): value
            for name, value in bounded_lengths.items()
            if isinstance(name, str) and type(value) is int and value > 0
        }
    scalar_ranges = payload.get("scalar_ranges")
    if isinstance(scalar_ranges, dict):
        metadata["scalar_ranges"] = {
            str(name): list(value)
            for name, value in scalar_ranges.items()
            if isinstance(name, str)
            and isinstance(value, list)
            and len(value) == 2
            and all(type(bound) is int for bound in value)
        }
    assumptions = payload.get("assumptions")
    if isinstance(assumptions, dict):
        metadata["assumptions"] = {
            key: assumptions[key]
            for key in (
                "pointer_alias_model",
                "hidden_state_model",
                "comparison",
            )
            if isinstance(assumptions.get(key), str)
        }
    artifact_sha256 = payload.get("artifact_sha256")
    if isinstance(artifact_sha256, dict):
        metadata["artifact_sha256"] = {
            path: digest.lower()
            for path, digest in artifact_sha256.items()
            if path in _RELATIONAL_KLEE_ARTIFACTS
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
        }
    metadata["counterexample_names"] = counterexample_names
    metadata["counterexample_count"] = len(counterexample_names)
    if status not in {"pass", "fail", "skipped", "blocked"}:
        return PhaseResult(
            phase,
            "fail",
            result.returncode,
            result.stdout,
            result.stderr,
            result.log_path,
            summary=f"{report_name} has unknown status {status!r}",
        )
    if phase == "symbolic_klee" and (
        metadata.get("schema") != _RELATIONAL_KLEE_SCHEMA
        or metadata.get("scope") != _RELATIONAL_KLEE_SCOPE
    ):
        return PhaseResult(
            phase,
            "fail",
            result.returncode,
            result.stdout,
            result.stderr,
            result.log_path,
            summary=f"{report_name} is not exact-schema relational KLEE evidence",
            metadata=metadata,
        )
    if phase == "symbolic_klee" and status == "pass" and not (
        metadata.get("outcome") == "no_counterexample"
        and metadata.get("failure_kind") is None
        and type(metadata.get("completed_paths")) is int
        and metadata["completed_paths"] > 0
        and type(metadata.get("generated_tests")) is int
        and metadata["generated_tests"] > 0
        and metadata.get("timed_out") is False
        and not counterexample_names
        and _has_exact_relational_model_metadata(metadata)
    ):
        return PhaseResult(
            phase,
            "fail",
            result.returncode,
            result.stdout,
            result.stderr,
            result.log_path,
            summary=f"{report_name} PASS lacks complete non-vacuous relational evidence",
            metadata=metadata,
        )
    if result.status != "pass" and status in {"pass", "skipped"}:
        return PhaseResult(
            phase,
            "fail",
            result.returncode,
            result.stdout,
            result.stderr,
            result.log_path,
            summary=f"{target} command failed but {report_name} claimed {status}",
            metadata=metadata,
        )
    detail = " ".join(
        f"{key}={metadata[key]}"
        for key in ("scope", "outcome", "failure_kind")
        if metadata.get(key)
    )
    return PhaseResult(
        phase,
        status,
        result.returncode,
        result.stdout,
        result.stderr,
        result.log_path,
        summary=(
            f"{target}: {status}"
            + (f" — {reason}" if reason else "")
            + (f" [{detail}]" if detail else "")
        ),
        metadata=metadata,
    )


def _is_relational_klee_counterexample(result: PhaseResult) -> bool:
    names = result.metadata.get("counterexample_names")
    return (
        result.status == "fail"
        and result.metadata.get("schema") == _RELATIONAL_KLEE_SCHEMA
        and result.metadata.get("scope") == _RELATIONAL_KLEE_SCOPE
        and result.metadata.get("outcome") == "counterexample"
        and result.metadata.get("failure_kind") == "relational_counterexample"
        and _has_exact_relational_model_metadata(result.metadata)
        and isinstance(names, list)
        and bool(names)
        and all(
            isinstance(name, str) and _RELATIONAL_KLEE_NAME_RE.fullmatch(name)
            for name in names
        )
    )


def run_shift_left_checks(
    project_dir: Path,
    enabled: bool = True,
    verbose: bool = False,
) -> dict[str, PhaseResult]:
    """Run cheap, pre-synthesis evidence in increasing cost order.

    Paired golden/HLS trace consistency is always a gate when shift-left checks are
    enabled. gcov is evidence-producing; missing symbolic tools report SKIPPED and KLEE
    infrastructure/incomplete exploration reports BLOCKED. Only an exact-schema,
    exact-scope, named golden/HLS-C relational counterexample remains FAIL and gates
    downstream HLS.
    """

    if not enabled:
        return {
            phase: PhaseResult(phase, "skipped", summary="shift-left verification disabled")
            for phase in SHIFT_LEFT_PHASES
        }
    trace = _run_make_phase(project_dir, "leveri-test", "shift_left_trace", 180, verbose)
    if trace.status == "pass" and "HLS-LeVeri consistency check passed" not in trace.stdout:
        trace = PhaseResult(
            "shift_left_trace",
            "fail",
            trace.returncode,
            trace.stdout,
            trace.stderr,
            trace.log_path,
            summary="paired-trace check exited 0 without its comparison success marker",
        )
    phases = {"shift_left_trace": trace}
    if trace.status != "pass":
        message = "paired shift-left trace verification failed"
        phases["coverage_gcov"] = PhaseResult("coverage_gcov", "blocked", summary=message)
        phases["symbolic_klee"] = PhaseResult("symbolic_klee", "blocked", summary=message)
        return phases
    coverage = _report_backed_phase(
        project_dir, "gcov-coverage", "coverage_gcov", "gcov_report.json", 240, verbose
    )
    if coverage.status == "fail":
        coverage = PhaseResult(
            coverage.name,
            "blocked",
            coverage.returncode,
            coverage.stdout,
            coverage.stderr,
            coverage.log_path,
            summary=f"gcov evidence unavailable (not a correctness verdict): {coverage.summary}",
        )
    phases["coverage_gcov"] = coverage
    symbolic = _report_backed_phase(
        project_dir, "klee-coverage", "symbolic_klee", "klee_report.json", 180, verbose
    )
    if symbolic.status == "fail" and not _is_relational_klee_counterexample(symbolic):
        symbolic = PhaseResult(
            symbolic.name,
            "blocked",
            symbolic.returncode,
            symbolic.stdout,
            symbolic.stderr,
            symbolic.log_path,
            summary=(
                "KLEE evidence is not a validated relational counterexample: "
                f"{symbolic.summary}"
            ),
            metadata=symbolic.metadata,
        )
    phases["symbolic_klee"] = symbolic
    return phases


# The generated oracle prints this once it has compared at least one value. Requiring it
# means a testbench that exits 0 without proving anything cannot pass -- symmetric with
# _gate_cosim_on_log, and backend-agnostic (it guards the Bambu and Vitis paths alike,
# since host equivalence is the first rung for both).
_EQUIV_SUCCESS_MARKER = "c2hlsc_agent: all"


def _gate_equivalence_on_evidence(result: PhaseResult) -> PhaseResult:
    """Downgrade a passing host-equivalence phase that shows no comparison evidence."""
    if result.status != "pass":
        return result
    text = f"{result.stdout or ''}\n{result.summary or ''}"
    if _EQUIV_SUCCESS_MARKER in text:
        return result
    return PhaseResult(
        result.name,
        "fail",
        summary=(
            "host equivalence exited 0 but the testbench printed no success marker "
            f"({_EQUIV_SUCCESS_MARKER!r}) — no evidence any value was compared"
        ),
        stdout=result.stdout,
        stderr=result.stderr,
        log_path=result.log_path,
    )


def _run_vitis_phase(
    project_dir: Path,
    phase: str,
    remote: RemoteVitis | None,
    vitis_bin: str,
) -> PhaseResult:
    timeout = PHASE_TIMEOUTS[phase]
    try:
        if remote is not None:
            return remote.run_phase(project_dir, phase, timeout)
        return run_command(
            vitis_tcl_command(vitis_bin, f"run_{phase}.tcl"),
            project_dir,
            phase,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(project_dir, phase, exc, f"Vitis {phase}")
    except OSError as exc:
        # Missing/unexecutable local launchers and SSH invocation failures are
        # infrastructure faults. Return a phase result so the report and repair loop can
        # block safely instead of losing the run to an uncaught exception.
        if remote is not None:
            message = f"remote vitis unavailable: running {phase} on {remote.host} failed: {exc}"
        else:
            message = (
                f"Vitis toolchain unavailable: launcher {vitis_bin!r} is not runnable "
                f"for {phase}: {exc}"
            )
        return PhaseResult(phase, "fail", summary=message)


def run_vitis(
    project_dir: Path,
    run_requested: bool,
    remote: RemoteVitis | None = None,
    upto: str = "cosim",
    vitis_bin: str = "vitis_hls",
) -> dict[str, PhaseResult]:
    """Run the Vitis ladder, optionally stopping early.

    ``upto`` limits the ladder ("csim" | "csynth" | "cosim"): the QoR optimizer scores
    candidates at ``upto="csynth"`` (the synthesis report is the score) without paying
    for a CoSim per candidate; only the accepted winner runs the full ladder.
    """

    if upto not in ("csim", "csynth", "cosim"):
        raise ValueError(f"upto must be csim|csynth|cosim, got {upto!r}")
    phases = {
        "csim": PhaseResult("csim", "skipped"),
        "csynth": PhaseResult("csynth", "skipped"),
        "cosim": PhaseResult("cosim", "skipped"),
    }
    if not run_requested:
        return phases

    if remote is not None:
        try:
            push = remote.push(project_dir)
        except (subprocess.TimeoutExpired, OSError) as exc:
            push = PhaseResult("vitis_push", "fail", summary=f"project sync to {remote.host} failed: {exc}")
        if push.status != "pass":
            # Infrastructure failure, not a code defect: mark it "remote vitis unavailable"
            # so classify_failure treats it as toolchain_unavailable (blocked) and the
            # auto-repair loop does NOT mutate correct source over a transient network fault.
            message = f"remote vitis unavailable: project sync to {remote.host} failed: {push.summary or push.stderr.strip()[-400:]}"
            return {
                "csim": PhaseResult("csim", "fail", summary=message),
                "csynth": PhaseResult("csynth", "blocked", summary=message),
                "cosim": PhaseResult("cosim", "blocked", summary=message),
            }
    else:
        resolved_vitis = find_vitis_executable(vitis_bin)
    if remote is None and resolved_vitis is None:
        message = (
            f"Vitis HLS launcher {vitis_bin!r} not found "
            "(pass --vitis-bin, source the AMD settings script, or use --vitis-ssh)"
        )
        return {
            "csim": PhaseResult("csim", "fail", summary=message),
            "csynth": PhaseResult("csynth", "blocked", summary=message),
            "cosim": PhaseResult("cosim", "blocked", summary=message),
        }
    if remote is None:
        vitis_bin = resolved_vitis

    try:
        phases["csim"] = _run_vitis_phase(project_dir, "csim", remote, vitis_bin)
        if phases["csim"].status != "pass":
            message = "csim failed"
            phases["csynth"] = PhaseResult("csynth", "blocked", summary=message)
            phases["cosim"] = PhaseResult("cosim", "blocked", summary=message)
            return phases
        if upto == "csim":
            return phases

        phases["csynth"] = _run_vitis_phase(project_dir, "csynth", remote, vitis_bin)
        if phases["csynth"].status != "pass":
            phases["cosim"] = PhaseResult("cosim", "blocked", summary="csynth failed")
            return phases
        if upto == "csynth":
            return phases

        phases["cosim"] = _run_vitis_phase(project_dir, "cosim", remote, vitis_bin)
        phases["cosim"] = _gate_cosim_on_log(phases["cosim"])
        return phases
    finally:
        if remote is not None:
            try:
                remote.pull(project_dir)
            except (subprocess.TimeoutExpired, OSError):
                pass  # best-effort artifact pull; phase logs are already local


_COSIM_FAILURE_MARKERS = (
    "co-simulation finished: fail",
    "cosim design failed",
    "co-simulation failed",
    "aborting cosim",
    "aborting co-simulation",
    "error: [cosim",
    "error: [sim",
    "mismatch test=",
)

VITIS_COSIM_SUCCESS_MARKERS = (
    "c/rtl co-simulation finished: pass",
    "co-simulation finished: pass",
)


def _gate_cosim_on_log(result: PhaseResult) -> PhaseResult:
    """Vitis can exit 0 while the CoSim log reports a mismatch. Downgrade pass->fail when
    the log carries an explicit co-simulation failure marker, so a zero exit code cannot
    silently defeat the C/RTL equivalence gate. Works for the remote path too: the local
    <phase>.log holds the ssh console output, which carries the co-simulation verdict."""
    if result.status != "pass":
        return result
    haystack = f"{result.stdout}\n{result.stderr}".lower()
    if result.log_path and result.log_path.exists():
        try:
            haystack += "\n" + result.log_path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            pass
    if any(marker in haystack for marker in _COSIM_FAILURE_MARKERS):
        return PhaseResult(
            result.name,
            "fail",
            result.returncode,
            result.stdout,
            result.stderr,
            result.log_path,
            summary="Vitis exited 0 but the CoSim log reports a co-simulation failure",
        )
    if not any(marker in haystack for marker in VITIS_COSIM_SUCCESS_MARKERS):
        return PhaseResult(
            result.name,
            "fail",
            result.returncode,
            result.stdout,
            result.stderr,
            result.log_path,
            summary=(
                "Vitis CoSim exited 0 but emitted no positive C/RTL co-simulation "
                "PASS marker — cannot confirm that an RTL comparison ran"
            ),
        )
    return result


def verify_project(
    project_dir: Path,
    run_vitis_requested: bool,
    verbose: bool = False,
    remote: RemoteVitis | None = None,
    local: "object | None" = None,
    run_shift_left: bool = True,
    vitis_bin: str = "vitis_hls",
) -> VerificationState:
    state = VerificationState()
    software = run_software_equivalence(project_dir, verbose=verbose)
    state.add_phase(software)
    state.mismatches.extend(parse_mismatches(software.stdout + "\n" + software.stderr))
    if software.status != "pass":
        for phase in SHIFT_LEFT_PHASES:
            state.add_phase(PhaseResult(phase, "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("csim", "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("csynth", "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("cosim", "blocked", summary="software equivalence failed"))
        return state
    shift_left = run_shift_left_checks(project_dir, enabled=run_shift_left, verbose=verbose)
    for result in shift_left.values():
        state.add_phase(result)
    failed_shift_left = next((result for result in shift_left.values() if result.status == "fail"), None)
    if failed_shift_left is not None:
        message = f"{failed_shift_left.name} failed"
        state.add_phase(PhaseResult("csim", "blocked", summary=message))
        state.add_phase(PhaseResult("csynth", "blocked", summary=message))
        state.add_phase(PhaseResult("cosim", "blocked", summary=message))
        return state
    # local-hls backend: run the whole csynth/cosim ladder locally (Bambu), no Vitis.
    # It is only constructed when selected, so `local is not None` means "use it".
    if local is not None and run_vitis_requested:
        for result in local.run(project_dir).values():
            state.add_phase(result)
        return state
    for result in run_vitis(
        project_dir,
        run_vitis_requested,
        remote=remote,
        vitis_bin=vitis_bin,
    ).values():
        state.add_phase(result)
    return state
