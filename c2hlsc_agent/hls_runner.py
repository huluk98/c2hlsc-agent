from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .cosim_verdict import evaluate_cosim_verdict
from .equivalence import PhaseResult, VerificationState, parse_mismatches, run_command
from .remote import RemoteVitis


PHASE_ORDER = ("software_equivalence", "leveri_trace", "csim", "csynth", "cosim")
PHASE_TIMEOUTS = {"csim": 600, "csynth": 1200, "cosim": 600}
LEVERI_TRACE_TIMEOUT = 240

_LEVERI_TB_FILES = (
    "tb/leveri_golden_tb.cpp",
    "tb/leveri_hls_tb.cpp",
    "tb/leveri_compare.py",
)


def earliest_failing_phase(state: VerificationState, run_vitis_requested: bool) -> str | None:
    required = ["software_equivalence"]
    # The LeVeri paired-trace gate is optional (config/leveri files); a state that
    # never ran it reports "skipped" and must not surface as the earliest failure.
    if state.status_for("leveri_trace") != "skipped":
        required.append("leveri_trace")
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
        summary=f"{label} timed out after {exc.timeout}s",
    )


def run_software_equivalence(project_dir: Path, verbose: bool = False) -> PhaseResult:
    try:
        result = run_command(["make", "test"], project_dir, "software_equivalence", timeout=120)
    except FileNotFoundError:
        return PhaseResult("software_equivalence", "fail", summary="make not found")
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(project_dir, "software_equivalence", exc, "host equivalence")
    if verbose and result.stdout:
        print(result.stdout)
    return result


def run_leveri_trace(project_dir: Path, verbose: bool = False) -> PhaseResult:
    """Run the LeVeri paired-trace gate (golden vs HLS trace + dual-tier comparator).

    Uses the project's own ``make leveri-test`` target so the check stays identical to
    the manual flow. Projects generated without the LeVeri bundle report "skipped".
    """

    if any(not (project_dir / rel).exists() for rel in _LEVERI_TB_FILES):
        return PhaseResult("leveri_trace", "skipped", summary="leveri testbenches not present")
    try:
        # PYTHON=<current interpreter>: native Windows rarely has a "python3" alias, and
        # the gated check must not depend on one; the Makefile defaults to python3 for
        # the manual flow.
        result = run_command(
            ["make", "leveri-test", f"PYTHON={sys.executable}"],
            project_dir,
            "leveri_trace",
            timeout=LEVERI_TRACE_TIMEOUT,
        )
    except FileNotFoundError:
        return PhaseResult("leveri_trace", "fail", summary="make not found")
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(project_dir, "leveri_trace", exc, "leveri paired-trace check")
    if verbose and result.stdout:
        print(result.stdout)
    return result


def vitis_executable() -> str | None:
    """Resolve the local vitis_hls launcher.

    ``C2HLSC_VITIS_BIN`` overrides PATH lookup — required on native Windows, where the
    launcher is ``vitis_hls.bat`` (e.g. ``D:\\Xilinx\\Vivado\\2024.2\\bin\\vitis_hls.bat``)
    and CreateProcess will not resolve a bare ``vitis_hls`` to a batch file. The resolved
    absolute path is what gets executed, so the .bat works without PATH edits.
    """

    override = os.environ.get("C2HLSC_VITIS_BIN", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.exists():
            return str(candidate)
        return shutil.which(override)
    return shutil.which("vitis_hls")


def _run_vitis_phase(project_dir: Path, phase: str, remote: RemoteVitis | None) -> PhaseResult:
    timeout = PHASE_TIMEOUTS[phase]
    try:
        if remote is not None:
            return remote.run_phase(project_dir, phase, timeout)
        executable = vitis_executable() or "vitis_hls"
        return run_command([executable, "-f", f"run_{phase}.tcl"], project_dir, phase, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(project_dir, phase, exc, f"Vitis {phase}")


def run_vitis(
    project_dir: Path,
    run_requested: bool,
    remote: RemoteVitis | None = None,
    upto: str = "cosim",
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
    elif vitis_executable() is None:
        message = (
            "vitis_hls not found on PATH (set C2HLSC_VITIS_BIN to the vitis_hls launcher, "
            "e.g. D:\\Xilinx\\Vivado\\2024.2\\bin\\vitis_hls.bat on Windows, or use "
            "--vitis-ssh to run Vitis on a remote Linux host)"
        )
        return {
            "csim": PhaseResult("csim", "fail", summary=message),
            "csynth": PhaseResult("csynth", "blocked", summary=message),
            "cosim": PhaseResult("cosim", "blocked", summary=message),
        }

    try:
        phases["csim"] = _run_vitis_phase(project_dir, "csim", remote)
        if phases["csim"].status != "pass":
            message = "csim failed"
            phases["csynth"] = PhaseResult("csynth", "blocked", summary=message)
            phases["cosim"] = PhaseResult("cosim", "blocked", summary=message)
            return phases
        if upto == "csim":
            return phases

        phases["csynth"] = _run_vitis_phase(project_dir, "csynth", remote)
        if phases["csynth"].status != "pass":
            phases["cosim"] = PhaseResult("cosim", "blocked", summary="csynth failed")
            return phases
        if upto == "csynth":
            return phases

        phases["cosim"] = _run_vitis_phase(project_dir, "cosim", remote)
        phases["cosim"] = _gate_cosim_on_log(phases["cosim"])
        return phases
    finally:
        if remote is not None:
            try:
                remote.pull(project_dir)
            except (subprocess.TimeoutExpired, OSError):
                pass  # best-effort artifact pull; phase logs are already local


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
    verdict = evaluate_cosim_verdict(result.status, haystack)
    if verdict.status == "fail":
        return PhaseResult(
            result.name,
            "fail",
            result.returncode,
            result.stdout,
            result.stderr,
            result.log_path,
            summary=verdict.reason,
        )
    return result


def verify_project(
    project_dir: Path,
    run_vitis_requested: bool,
    verbose: bool = False,
    remote: RemoteVitis | None = None,
    leveri: bool = True,
) -> VerificationState:
    state = VerificationState()
    software = run_software_equivalence(project_dir, verbose=verbose)
    state.add_phase(software)
    state.mismatches.extend(parse_mismatches(software.stdout + "\n" + software.stderr))
    if software.status != "pass":
        state.add_phase(PhaseResult("leveri_trace", "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("csim", "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("csynth", "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("cosim", "blocked", summary="software equivalence failed"))
        return state
    if leveri:
        trace = run_leveri_trace(project_dir, verbose=verbose)
    else:
        trace = PhaseResult("leveri_trace", "skipped", summary="leveri gate disabled")
    state.add_phase(trace)
    if trace.status == "fail":
        state.add_phase(PhaseResult("csim", "blocked", summary="leveri trace check failed"))
        state.add_phase(PhaseResult("csynth", "blocked", summary="leveri trace check failed"))
        state.add_phase(PhaseResult("cosim", "blocked", summary="leveri trace check failed"))
        return state
    for result in run_vitis(project_dir, run_vitis_requested, remote=remote).values():
        state.add_phase(result)
    return state
