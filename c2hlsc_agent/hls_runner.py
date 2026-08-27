from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .cosim_verdict import evaluate_cosim_verdict
from .equivalence import PhaseResult, VerificationState, parse_mismatches, run_command
from .remote import RemoteVitis


PHASE_ORDER = ("software_equivalence", "csim", "csynth", "cosim")
PHASE_TIMEOUTS = {"csim": 600, "csynth": 1200, "cosim": 600}

# The exact string classify_log_family greps for to reach the toolchain_unavailable
# family, which routes to blocked and stops the repair agent mutating good source.
_NOT_FOUND_MARKER = "vitis_hls not found"

# On Windows the launcher is a batch file. CreateProcess cannot execute one directly.
_SHELL_LAUNCHER_SUFFIXES = (".bat", ".cmd")


def resolve_hls_bin(explicit: str | None = None) -> str | None:
    """Resolve the local Vitis launcher to a concrete path, or ``None``.

    Returning the RESOLVED path matters on Windows, where the launcher is
    ``vitis_hls.bat``: :func:`shutil.which` finds it through ``PATHEXT``, but the
    ``CreateProcess`` call behind :mod:`subprocess` appends only ``.exe``. Launching the
    bare name therefore raises ``FileNotFoundError`` on the very machine where Vitis IS
    installed -- the guard says "found" and the run then dies.

    Order: an explicit path or name, then ``C2HLSC_VITIS_BIN``, then ``PATH``.
    """

    candidate = explicit or os.environ.get("C2HLSC_VITIS_BIN") or "vitis_hls"
    if os.path.isabs(candidate):
        return candidate if os.path.exists(candidate) else None
    return shutil.which(candidate)


def hls_launch_argv(binary: str, tcl: str, *, windows: bool | None = None) -> list[str]:
    """Argv that actually launches one Vitis phase.

    A ``.bat``/``.cmd`` launcher has to go through ``cmd.exe``; passing it straight to
    ``CreateProcess`` fails even with a fully qualified path.
    """

    if windows is None:
        windows = os.name == "nt"
    if windows and binary.lower().endswith(_SHELL_LAUNCHER_SUFFIXES):
        return ["cmd", "/c", binary, "-f", tcl]
    return [binary, "-f", tcl]


def earliest_failing_phase(state: VerificationState, run_vitis_requested: bool) -> str | None:
    required = ["software_equivalence"]
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


def _run_vitis_phase(
    project_dir: Path,
    phase: str,
    remote: RemoteVitis | None,
    hls_bin: str | None = None,
) -> PhaseResult:
    timeout = PHASE_TIMEOUTS[phase]
    try:
        if remote is not None:
            return remote.run_phase(project_dir, phase, timeout)
        binary = resolve_hls_bin(hls_bin) or "vitis_hls"
        argv = hls_launch_argv(binary, f"run_{phase}.tcl")
        return run_command(argv, project_dir, phase, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(project_dir, phase, exc, f"Vitis {phase}")
    except OSError as exc:
        # The launcher resolved but could not be started -- a Windows .bat reached
        # through CreateProcess, a broken symlink, a missing interpreter. This is an
        # environment problem, so report it in the toolchain_unavailable family rather
        # than letting it escape as an uncaught exception out of the whole conversion.
        return PhaseResult(
            phase,
            "fail",
            summary=f"{_NOT_FOUND_MARKER}: could not launch it for {phase} ({type(exc).__name__}: {exc})",
        )


def run_vitis(
    project_dir: Path,
    run_requested: bool,
    remote: RemoteVitis | None = None,
    upto: str = "cosim",
    hls_bin: str | None = None,
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
    elif resolve_hls_bin(hls_bin) is None:
        message = (
            f"{_NOT_FOUND_MARKER} on PATH (pass --vitis-bin with the full path, set "
            "C2HLSC_VITIS_BIN, or use --vitis-ssh to run Vitis on a remote host)"
        )
        return {
            "csim": PhaseResult("csim", "fail", summary=message),
            "csynth": PhaseResult("csynth", "blocked", summary=message),
            "cosim": PhaseResult("cosim", "blocked", summary=message),
        }

    try:
        phases["csim"] = _run_vitis_phase(project_dir, "csim", remote, hls_bin=hls_bin)
        if phases["csim"].status != "pass":
            message = "csim failed"
            phases["csynth"] = PhaseResult("csynth", "blocked", summary=message)
            phases["cosim"] = PhaseResult("cosim", "blocked", summary=message)
            return phases
        if upto == "csim":
            return phases

        phases["csynth"] = _run_vitis_phase(project_dir, "csynth", remote, hls_bin=hls_bin)
        if phases["csynth"].status != "pass":
            phases["cosim"] = PhaseResult("cosim", "blocked", summary="csynth failed")
            return phases
        if upto == "csynth":
            return phases

        phases["cosim"] = _run_vitis_phase(project_dir, "cosim", remote, hls_bin=hls_bin)
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
    hls_bin: str | None = None,
) -> VerificationState:
    state = VerificationState()
    software = run_software_equivalence(project_dir, verbose=verbose)
    state.add_phase(software)
    state.mismatches.extend(parse_mismatches(software.stdout + "\n" + software.stderr))
    if software.status != "pass":
        state.add_phase(PhaseResult("csim", "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("csynth", "blocked", summary="software equivalence failed"))
        state.add_phase(PhaseResult("cosim", "blocked", summary="software equivalence failed"))
        return state
    for result in run_vitis(project_dir, run_vitis_requested, remote=remote, hls_bin=hls_bin).values():
        state.add_phase(result)
    return state
