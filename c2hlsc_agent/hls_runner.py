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
        phases["csim"] = run_command(["vitis_hls", "-f", "run_csim.tcl"], project_dir, "csim", timeout=1800)
    except subprocess.TimeoutExpired as exc:
        phases["csim"] = PhaseResult("csim", "fail", summary=f"Vitis CSim timed out: {exc}")
    if phases["csim"].status != "pass":
        message = "csim failed"
        phases["csynth"] = PhaseResult("csynth", "blocked", summary=message)
        phases["cosim"] = PhaseResult("cosim", "blocked", summary=message)
        return phases

    try:
        phases["csynth"] = run_command(["vitis_hls", "-f", "run_csynth.tcl"], project_dir, "csynth", timeout=3600)
    except subprocess.TimeoutExpired as exc:
        phases["csynth"] = PhaseResult("csynth", "fail", summary=f"Vitis synthesis timed out: {exc}")
    if phases["csynth"].status != "pass":
        phases["cosim"] = PhaseResult("cosim", "blocked", summary="csynth failed")
        return phases

    try:
        phases["cosim"] = run_command(["vitis_hls", "-f", "run_cosim.tcl"], project_dir, "cosim", timeout=3600)
    except subprocess.TimeoutExpired as exc:
        phases["cosim"] = PhaseResult("cosim", "fail", summary=f"Vitis CoSim timed out: {exc}")
    return phases


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
