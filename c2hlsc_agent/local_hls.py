"""Local, Vitis-free HLS co-simulation backend (PandA Bambu).

The remote/Vitis path synthesizes C to RTL (``csynth``) and co-simulates the RTL
against the C reference (``cosim``). Neither is available on a machine without
Vitis. This backend reproduces both **locally** using the open-source Bambu HLS
tool: it synthesizes the golden C reference to Verilog and runs Bambu's own
C/RTL co-simulation with Icarus Verilog.

Bambu ships as an x86_64 Linux AppImage; ``scripts/bambu.sh`` runs it inside a
``linux/amd64`` container so it works on Apple Silicon too. Only Bambu runs in
the container -- the C sources and generated RTL stay on the host.

Equivalence rationale: host ``software_equivalence`` already proves the generated
HLS-C matches the golden C. This backend proves the golden C matches synthesized
RTL. Composed, the generated design is functionally equivalent to real RTL --
without Vitis. The RTL is Bambu's, not Vitis's, so this is a correctness gate,
not a Vitis QoR sign-off (use the Vitis path on a licensed host for that).
"""
from __future__ import annotations

import os
import random
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .analyze import AnalysisResult, FunctionArg
from .config import AgentConfig
from .equivalence import PhaseResult

DEFAULT_SQUASHFS = Path.home() / "tools" / "eda" / "bambu" / "squashfs-root"
DEFAULT_BAMBU_TESTS = 16  # emulated x86 sim is slow; keep the default vector count modest
COSIM_TIMEOUT = 2400
# Marker written into every local-hls phase log; classify_failure keys on it to
# route a failure here to the "blocked" backend-limitation family (see agent_loop).
BACKEND_LOG_TAG = "[c2hlsc local-hls backend / Bambu]"


def _wrapper_cmd() -> list[str]:
    """The command prefix that runs Bambu. Override with C2HLSC_LOCAL_HLS_CMD
    (e.g. a native ``bambu`` if you are on Linux, or a custom docker wrapper)."""
    override = os.environ.get("C2HLSC_LOCAL_HLS_CMD")
    if override:
        return shlex.split(override)
    script = Path(__file__).resolve().parent.parent / "scripts" / "bambu.sh"
    return ["bash", str(script)]


def _squashfs() -> Path:
    return Path(os.environ.get("C2HLSC_BAMBU_SQUASHFS", str(DEFAULT_SQUASHFS)))


def resolve_cosim_backend(config: AgentConfig, remote: object | None) -> str:
    """Pick the csynth/cosim backend. Explicit config.cosim_backend wins; "auto"
    prefers a configured remote Vitis, then a local vitis_hls, then local Bambu,
    else "none" (skip the RTL ladder)."""
    choice = (getattr(config, "cosim_backend", "auto") or "auto").lower()
    if choice != "auto":
        return choice
    if remote is not None:
        return "vitis-ssh"
    if shutil.which(getattr(config, "vitis_bin", "vitis_hls") or "vitis_hls"):
        return "vitis"
    ok, _ = available()
    return "local-hls" if ok else "none"


def available() -> tuple[bool, str]:
    """Is the local HLS backend usable here? Returns (ok, reason-if-not)."""
    if os.environ.get("C2HLSC_LOCAL_HLS_CMD"):
        return True, ""
    if shutil.which("docker") is None:
        return False, "docker not found (needed to run the Bambu amd64 container)"
    bambu = _squashfs() / "usr" / "bin" / "bambu"
    if not bambu.exists():
        return False, (
            f"Bambu not found at {bambu}; extract the AppImage "
            "(./bambu-latest.AppImage --appimage-extract) or set C2HLSC_BAMBU_SQUASHFS"
        )
    return True, ""


# --- type helpers (kept local so this module has no cross-import churn) --------

_SIGNED = {"char", "signed char", "int8_t", "short", "int16_t", "int", "int32_t", "long", "int64_t"}


def _elem_bits(c_type: str) -> int:
    t = c_type.replace("const", "").replace("*", "").strip()
    for key, bits in (
        ("8", 8), ("16", 16), ("32", 32), ("64", 64),
        ("char", 8), ("short", 16), ("long", 64), ("int", 32),
    ):
        if key in t:
            return bits
    return 32


def _is_unsigned(c_type: str) -> bool:
    t = c_type.lower()
    return "unsigned" in t or "uint" in t or t.strip().startswith("u")


def _value_range(arg: FunctionArg) -> tuple[int, int]:
    """A safe value range that exercises the kernel without overflowing a plain
    C add/multiply (we deliberately stay well inside the type's full range)."""
    bits = min(_elem_bits(arg.c_type), 32)
    if _is_unsigned(arg.c_type):
        hi = min(2 ** bits - 1, 2 ** 16 - 1)
        return 0, hi
    hi = min(2 ** (bits - 1) - 1, 2 ** 15 - 1)
    return -hi, hi


def _array_length(arg: FunctionArg) -> int:
    return arg.length or 16


# --- test-vector generation (Bambu --generate-tb XML) -------------------------

def _looks_like_length(name: str) -> bool:
    return re.fullmatch(r"(?i)(n|m|k|len|size|count|length|num|elems?|elements)\w*", name or "") is not None


def _testbench_xml(function_args: list[FunctionArg], num_tests: int, seed: int) -> str:
    rng = random.Random(seed)
    max_len = max((_array_length(a) for a in function_args if a.is_pointer_like), default=1)
    lines = ['<?xml version="1.0"?>', "<function>"]
    for test_index in range(num_tests):
        attrs = []
        for arg in function_args:
            if arg.is_pointer_like:
                length = _array_length(arg)
                if (arg.direction or "input") == "output":
                    values = [0] * length
                else:
                    lo, hi = _value_range(arg)
                    values = [rng.randint(lo, hi) for _ in range(length)]
                attrs.append(f'{arg.name}="{{{",".join(str(v) for v in values)}}}"')
            else:
                if arg.scalar_range is not None:
                    lo, hi = arg.scalar_range
                    # A length-like scalar bounds the active region of the arrays.
                    # Drive it to the full length first (so the kernel actually
                    # processes every element), then vary it so partial-length
                    # cases are exercised too. Never emit a degenerate 0-length.
                    if _looks_like_length(arg.name):
                        value = min(hi, max_len) if test_index == 0 else rng.randint(max(lo, 1), hi)
                    else:
                        value = rng.randint(lo, hi)
                else:
                    lo, hi = _value_range(arg)
                    value = rng.randint(lo, hi)
                attrs.append(f'{arg.name}="{value}"')
        lines.append(f"  <testbench {' '.join(attrs)}/>")
    lines.append("</function>")
    return "\n".join(lines) + "\n"


# --- result parsing -----------------------------------------------------------

def _parse_cosim(stdout: str, returncode: int) -> tuple[bool, str]:
    """Bambu returns a non-zero exit code on any failure, including a C/RTL
    mismatch (its generated testbench self-checks each vector against the C
    reference). A clean exit with an "executions" summary is a real cosim pass."""
    if returncode != 0:
        for line in stdout.splitlines():
            if "error ->" in line.lower():  # Bambu's fatal-error prefix
                return False, line.strip()[:200]
        return False, f"Bambu exited {returncode} during synthesis/co-simulation"
    # Require POSITIVE evidence that vectors actually ran. Exit 0 alone is not a cosim
    # pass: if --simulate degrades (rejected testbench XML, a simulator Bambu accepts but
    # does not run, a front-end path that stops after synthesis) the log carries no
    # verdict at all, and returning True here reported "cosim: pass" for a run in which
    # no C/RTL comparison happened. Zero executions is likewise not a pass.
    match = re.search(r"Number of executions\s*:\s*(\d+)", stdout)
    if match:
        executions = int(match.group(1))
        if executions < 1:
            return False, (
                f"{BACKEND_LOG_TAG} Bambu exited 0 but ran 0 co-simulation vectors — "
                "no C/RTL comparison was performed"
            )
        return True, f"Bambu C/RTL co-simulation passed ({executions} vectors, Verilator)"
    return False, (
        f"{BACKEND_LOG_TAG} Bambu exited 0 but produced no co-simulation summary "
        "('Number of executions' absent) — cannot confirm any C/RTL comparison ran"
    )


@dataclass
class LocalHlsCosim:
    golden_c: Path
    top: str
    function_args: list[FunctionArg]
    num_tests: int
    seed: int
    timeout: int = COSIM_TIMEOUT

    @classmethod
    def from_config(
        cls, config: AgentConfig, analysis: AnalysisResult, project_dir: Path
    ) -> "LocalHlsCosim | None":
        ok, _ = available()
        if not ok:
            return None
        golden = config.input_files[0] if config.input_files else None
        if golden is None or not Path(golden).exists():
            return None
        bambu_tests = int(os.environ.get("C2HLSC_BAMBU_TESTS", DEFAULT_BAMBU_TESTS))
        return cls(
            golden_c=Path(golden),
            top=config.top or analysis.function.name,
            function_args=list(analysis.function.args),
            num_tests=max(1, min(config.num_tests, bambu_tests)),
            seed=config.seed,
        )

    def _write_report(self, project_dir: Path, phase: str, text: str) -> Path:
        log_path = project_dir / f"{phase}.log"
        log_path.write_text(text, encoding="utf-8")
        return log_path

    def run(self, project_dir: Path) -> dict[str, PhaseResult]:
        """Synthesize the golden C to RTL and co-simulate it locally with Bambu.

        Returns csim/csynth/cosim PhaseResults so it drops into the same
        verification ladder the Vitis path fills.
        """
        work = project_dir / ".bambu"
        work.mkdir(parents=True, exist_ok=True)
        spec = work / "spec.c"
        spec.write_text(Path(self.golden_c).read_text(encoding="utf-8"), encoding="utf-8")
        (work / "test.xml").write_text(
            _testbench_xml(self.function_args, self.num_tests, self.seed), encoding="utf-8"
        )

        # Optimization knobs (Bambu's default is the BAMBU-BALANCED-MP setup, i.e. -O2):
        #   C2HLSC_BAMBU_SETUP  -> --experimental-setup=<setup> (e.g. BAMBU-AREA, BAMBU-PERFORMANCE)
        #   C2HLSC_BAMBU_FLAGS  -> any extra bambu flags, shlex-split
        extra: list[str] = []
        setup = os.environ.get("C2HLSC_BAMBU_SETUP")
        if setup:
            extra.append(f"--experimental-setup={setup}")
        extra += shlex.split(os.environ.get("C2HLSC_BAMBU_FLAGS", ""))
        cmd = _wrapper_cmd() + [
            str(work),
            "spec.c",
            f"--top-fname={self.top}",
            "--generate-tb=test.xml",
            "--simulate",
            f"--simulator={os.environ.get('C2HLSC_BAMBU_SIMULATOR', 'VERILATOR')}",
            "--no-clean",
            *extra,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
            output = (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or "")
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.output or "") + f"\n[timed out after {self.timeout}s]"
            log = self._write_report(project_dir, "cosim", output)
            fail = PhaseResult("cosim", "fail", log_path=log, summary=f"{BACKEND_LOG_TAG} Bambu timed out after {self.timeout}s")
            return {
                "csim": PhaseResult("csim", "pass", summary="covered by host software_equivalence (local-hls)"),
                "csynth": PhaseResult("csynth", "fail", summary=f"{BACKEND_LOG_TAG} Bambu synthesis timed out"),
                "cosim": fail,
            }

        # Collect the synthesized RTL into the project's rtl/ dir for inspection/reuse.
        produced = sorted(work.glob("*.v"))
        synth_ok = bool(produced)
        if synth_ok:
            rtl_dir = project_dir / "rtl"
            rtl_dir.mkdir(exist_ok=True)
            for verilog in produced:
                shutil.copy2(verilog, rtl_dir / verilog.name)

        csynth_log = self._write_report(project_dir, "csynth", output)
        if not synth_ok:
            return {
                "csim": PhaseResult("csim", "pass", summary="covered by host software_equivalence (local-hls)"),
                "csynth": PhaseResult(
                    "csynth", "fail", returncode, output[-4000:], "", csynth_log,
                    f"{BACKEND_LOG_TAG} Bambu produced no Verilog (synthesis failed)",
                ),
                "cosim": PhaseResult("cosim", "blocked", summary="csynth failed"),
            }

        cosim_ok, cosim_summary = _parse_cosim(output, returncode)
        if not cosim_ok:
            cosim_summary = f"{BACKEND_LOG_TAG} {cosim_summary}"
        cosim_log = self._write_report(project_dir, "cosim", output)
        return {
            "csim": PhaseResult("csim", "pass", summary="covered by host software_equivalence (local-hls)"),
            "csynth": PhaseResult(
                "csynth", "pass", returncode, output[-2000:], "", csynth_log,
                f"Bambu synthesized {', '.join(v.name for v in produced)}",
            ),
            "cosim": PhaseResult(
                "cosim", "pass" if cosim_ok else "fail", returncode,
                output[-4000:], "", cosim_log, cosim_summary,
            ),
        }
