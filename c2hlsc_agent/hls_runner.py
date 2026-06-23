from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .equivalence import PhaseResult, VerificationState, parse_mismatches, run_command


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
        result = run_command(["vitis_hls", "-f", "run_hls.tcl"], project_dir, "vitis_hls", timeout=3600)
    except subprocess.TimeoutExpired as exc:
        result = PhaseResult("vitis_hls", "fail", summary=f"Vitis HLS timed out: {exc}")
    text = (result.stdout + "\n" + result.stderr).lower()
    phases["csim"] = PhaseResult("csim", "pass" if "csim_design" in text and result.status == "pass" else result.status, result.returncode, result.stdout, result.stderr, result.log_path)
    phases["csynth"] = PhaseResult("csynth", "pass" if "csynth_design" in text and result.status == "pass" else result.status, result.returncode, result.stdout, result.stderr, result.log_path)
    phases["cosim"] = PhaseResult("cosim", "pass" if "cosim_design" in text and result.status == "pass" else result.status, result.returncode, result.stdout, result.stderr, result.log_path)
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
