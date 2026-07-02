from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .equivalence import PhaseResult, VerificationState, parse_mismatches, run_command


PHASE_ORDER = ("software_equivalence", "csim", "csynth", "cosim")


def earliest_failing_phase(state: VerificationState, run_vitis_requested: bool) -> str | None:
    required = ["software_equivalence"]
    if run_vitis_requested:
        required.extend(["csim", "csynth", "cosim"])
    for phase in required:
        if state.status_for(phase) != "pass":
            return phase
    return None


def run_software_equivalence(project_dir: Path, verbose: bool = False) -> PhaseResult:
    try:
        result = run_command(["make", "test"], project_dir, "software_equivalence", timeout=120)
    except FileNotFoundError:
        return PhaseResult("software_equivalence", "fail", summary="make not found")
    except subprocess.TimeoutExpired as exc:
        return PhaseResult("software_equivalence", "fail", summary=f"host equivalence timed out: {exc}")
    if verbose and result.stdout:
        print(result.stdout)
    return result


def run_vitis(project_dir: Path, run_requested: bool) -> dict[str, PhaseResult]:
    phases = {
        "csim": PhaseResult("csim", "skipped"),
        "csynth": PhaseResult("csynth", "skipped"),
        "cosim": PhaseResult("cosim", "skipped"),
    }
    if not run_requested:
        return phases
    if shutil.which("vitis_hls") is None:
        message = "vitis_hls not found on PATH"
        return {
            "csim": PhaseResult("csim", "fail", summary=message),
            "csynth": PhaseResult("csynth", "blocked", summary=message),
            "cosim": PhaseResult("cosim", "blocked", summary=message),
        }
    try:
        phases["csim"] = run_command(["vitis_hls", "-f", "run_csim.tcl"], project_dir, "csim", timeout=600)
    except subprocess.TimeoutExpired as exc:
        phases["csim"] = PhaseResult("csim", "fail", summary=f"Vitis CSim timed out: {exc}")
    if phases["csim"].status != "pass":
        message = "csim failed"
        phases["csynth"] = PhaseResult("csynth", "blocked", summary=message)
        phases["cosim"] = PhaseResult("cosim", "blocked", summary=message)
        return phases

    try:
        phases["csynth"] = run_command(["vitis_hls", "-f", "run_csynth.tcl"], project_dir, "csynth", timeout=1200)
    except subprocess.TimeoutExpired as exc:
        phases["csynth"] = PhaseResult("csynth", "fail", summary=f"Vitis synthesis timed out: {exc}")
    if phases["csynth"].status != "pass":
        phases["cosim"] = PhaseResult("cosim", "blocked", summary="csynth failed")
        return phases

    try:
        phases["cosim"] = run_command(["vitis_hls", "-f", "run_cosim.tcl"], project_dir, "cosim", timeout=600)
    except subprocess.TimeoutExpired as exc:
        phases["cosim"] = PhaseResult("cosim", "fail", summary=f"Vitis CoSim timed out: {exc}")
    else:
        phases["cosim"] = _gate_cosim_on_log(phases["cosim"])
    return phases


_COSIM_FAILURE_MARKERS = (
    "co-simulation finished: fail",
    "cosim design failed",
    "co-simulation failed",
    "aborting cosim",
)


def _gate_cosim_on_log(result: PhaseResult) -> PhaseResult:
    """Vitis can exit 0 while the CoSim log reports a mismatch. Downgrade pass->fail when
    the log carries an explicit co-simulation failure marker, so a zero exit code cannot
    silently defeat the C/RTL equivalence gate."""
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
    return result


def verify_project(project_dir: Path, run_vitis_requested: bool, verbose: bool = False) -> VerificationState:
    state = VerificationState()
    software = run_software_equivalence(project_dir, verbose=verbose)
    state.add_phase(software)
    state.mismatches.extend(parse_mismatches(software.stdout + "\n" + software.stderr))
    if software.status != "pass":
        state.add_phase(PhaseResult("csim", "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("csynth", "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("cosim", "blocked", summary="software equivalence failed"))
        return state
    for result in run_vitis(project_dir, run_vitis_requested).values():
        state.add_phase(result)
    return state
