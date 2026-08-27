"""Detect the external tools each verification tier needs, and install the ones we can.

The agent degrades honestly when a tool is missing — a phase reports `blocked`, a coverage
target reports `skipped` — but "honestly blocked" is still blocked. This module turns that
into something actionable: it knows which tool each tier needs, whether it is on this
machine, and the exact command that would install it here (Homebrew on macOS, the system
package manager on Linux).

Three deliberate rules:

* **Nothing installs silently.** ``check()`` only looks. ``install()`` runs package-manager
  commands and is reached from ``c2hlsc-agent doctor --install``, which the user types.
* **A formula is verified before it is used.** Package names drift between platforms and
  releases; a command that does not exist is worse than a message saying so, so Homebrew
  formulae are confirmed with ``brew info`` before being offered.
* **Some tools genuinely cannot be installed this way** — KLEE has no macOS formula, and
  Vitis HLS is a licensed vendor download. Those carry instructions instead of a command,
  and are never reported as installable.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Iterable


#: Which part of the flow stops working when a tool is absent.
TIERS = ("core", "coverage", "symbolic", "ppa", "rtl", "vendor")

TIER_PURPOSE = {
    "core": "host equivalence and the shift-left trace tier (tb/host_build.py test / leveri-test)",
    "coverage": "concrete structural coverage (make gcov-coverage) and the refinement loop",
    "symbolic": "symbolic exploration for corner-case stimulus (make klee-coverage)",
    "ppa": "local synthesis, gate-level simulation and STA (optimize --local-ppa)",
    "rtl": "the standalone RTL testbench flow (make rtl-cosim)",
    "vendor": "Vitis CSim / CSynth / C-RTL CoSim",
}


@dataclass(frozen=True)
class Tool:
    name: str
    tier: str
    purpose: str
    brew: str | None = None
    apt: str | None = None
    dnf: str | None = None
    pacman: str | None = None
    winget: str | None = None
    #: Absent-but-optional tools never fail `doctor`; the flow works without them.
    optional: bool = False
    #: Environment variables that may point at the binary instead of PATH.
    env_overrides: tuple[str, ...] = ()
    #: Alternative binary names that satisfy the same need.
    aliases: tuple[str, ...] = ()
    #: Shown when there is no package for this platform.
    manual: str = ""

    def package_for(self, manager: str | None) -> str | None:
        return {
            "brew": self.brew,
            "apt": self.apt,
            "dnf": self.dnf,
            "pacman": self.pacman,
            "winget": self.winget,
        }.get(manager or "")


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="g++",
        tier="core",
        purpose="compiles the oracle and paired-trace testbenches",
        brew="gcc",
        apt="g++",
        dnf="gcc-c++",
        pacman="gcc",
        aliases=("clang++", "c++"),
        manual=(
            "macOS: the Xcode command line tools already provide a g++ shim (xcode-select --install). "
            "Windows: install a GCC/Clang-style compiler natively -- winget install LLVM.LLVM, or "
            "MSYS2 mingw-w64-gcc. MSVC (cl.exe) is not usable: its flag syntax is incompatible."
        ),
    ),
    Tool(
        name="make",
        tier="core",
        purpose="convenience alias over tb/host_build.py; NOT required -- the agent runs the driver directly",
        brew="make",
        apt="make",
        dnf="make",
        pacman="make",
        winget="GnuWin32.Make",
        optional=True,
        manual=(
            "Nothing breaks without make: every recipe lives in the generated tb/host_build.py "
            "and the agent invokes it directly, which is what makes native Windows work."
        ),
    ),
    Tool(
        name="python3",
        tier="core",
        purpose="runs the dual-tier comparator and the coverage scripts",
        brew="python",
        apt="python3",
        dnf="python3",
        pacman="python",
        winget="Python.Python.3.12",
        aliases=("python",),
    ),
    Tool(
        name="gcov",
        tier="coverage",
        purpose="line and branch coverage of the golden C and the generated HLS-C",
        brew="gcc",
        apt="gcc",
        dnf="gcc",
        pacman="gcc",
        env_overrides=("GCOV",),
        aliases=("llvm-cov",),
        manual=(
            "macOS ships a gcov-compatible llvm-cov with the Xcode command line tools; "
            "Homebrew gcc also installs a versioned gcov (set GCOV=gcov-14)."
        ),
    ),
    Tool(
        name="klee",
        tier="symbolic",
        purpose="symbolic execution that finds corner-case inputs the random schedule misses",
        apt="klee",
        env_overrides=("KLEE",),
        manual=(
            "KLEE is packaged on Debian but not on Ubuntu or macOS. On Debian/Ubuntu build it "
            "with: sudo bash scripts/install_klee.sh. Everywhere else the generated "
            "tb/run_klee.py falls back to the official klee/klee container automatically once "
            "Docker is running (macOS: brew install --cask docker); force or disable that with "
            "C2HLSC_KLEE_DOCKER=1 / =0."
        ),
    ),
    Tool(
        name="docker",
        tier="symbolic",
        purpose="runs the official KLEE container where KLEE is not native (macOS, Ubuntu)",
        brew="docker",
        apt="docker.io",
        dnf="moby-engine",
        pacman="docker",
        winget="Docker.DockerDesktop",
        manual="On macOS install Docker Desktop: brew install --cask docker",
    ),
    Tool(
        name="clang++",
        tier="symbolic",
        purpose="compiles the KLEE driver to LLVM bitcode",
        brew="llvm",
        apt="clang",
        dnf="clang",
        pacman="clang",
        winget="LLVM.LLVM",
        env_overrides=("KLEE_CXX",),
    ),
    Tool(
        name="yosys",
        tier="ppa",
        purpose="maps the synthesized RTL to a standard-cell netlist for area",
        brew="yosys",
        apt="yosys",
        dnf="yosys",
        pacman="yosys",
    ),
    Tool(
        name="sta",
        tier="ppa",
        purpose="OpenSTA: worst setup slack and power on the mapped netlist",
        brew="opensta",
        apt=None,
        env_overrides=("STA_BIN", "C2HLSC_STA"),
        manual=(
            "OpenSTA may not be packaged for your platform; build from source: "
            "https://github.com/parallaxsw/OpenSTA (then set STA_BIN to the binary)"
        ),
    ),
    Tool(
        name="iverilog",
        tier="rtl",
        purpose="simulates the standalone RTL testbench",
        brew="icarus-verilog",
        apt="iverilog",
        dnf="iverilog",
        pacman="iverilog",
    ),
    Tool(
        name="verilator",
        tier="rtl",
        purpose="alternative RTL simulator",
        brew="verilator",
        apt="verilator",
        dnf="verilator",
        pacman="verilator",
    ),
    Tool(
        name="vitis_hls",
        tier="vendor",
        purpose="CSim, CSynth and C/RTL CoSim",
        env_overrides=("VITIS_HLS_BIN",),
        manual=(
            "Vitis HLS is a licensed AMD/Xilinx download and cannot be installed by a package "
            "manager. It is Linux-only: on a Mac, run the Vitis phases remotely with "
            "--vitis-ssh user@linux-host."
        ),
    ),
)


@dataclass
class ToolStatus:
    tool: Tool
    path: str | None
    source: str = "PATH"
    installable: bool = False
    command: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return self.path is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.tool.name,
            "tier": self.tool.tier,
            "purpose": self.tool.purpose,
            "present": self.present,
            "path": self.path,
            "source": self.source,
            "installable": self.installable,
            "command": list(self.command),
            "manual": self.tool.manual,
        }


def package_manager() -> str | None:
    """The package manager to use on this machine, or ``None`` when there is none."""

    if platform.system() == "Darwin":
        return "brew" if shutil.which("brew") else None
    if os.name == "nt":
        return "winget" if shutil.which("winget") else None
    for manager, binary in (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman")):
        if shutil.which(binary):
            return manager
    return None


def _brew_has_formula(formula: str) -> bool:
    """Confirm a Homebrew formula exists before offering to install it."""

    try:
        result = subprocess.run(
            ["brew", "info", "--json=v2", formula],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return False
    return bool(payload.get("formulae") or payload.get("casks"))


def _linux_has_package(manager: str, package: str) -> bool:
    """Confirm a Linux package exists in the configured repositories.

    Package availability is distro- and release-specific: ``klee`` is a real Debian
    package but on Ubuntu 24.04 the only match for that name is a *font*. Offering
    `apt-get install klee` there hands the user a command that fails, or worse installs
    something unrelated -- so every name is confirmed against the local index first, the
    same way Homebrew formulae are.
    """

    probe = {
        "apt": ["apt-cache", "policy", package],
        "dnf": ["dnf", "--quiet", "list", "--available", package],
        "pacman": ["pacman", "-Si", package],
        "winget": ["winget", "show", "--id", package, "--exact"],
    }.get(manager)
    if probe is None:
        return False
    try:
        result = subprocess.run(probe, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    if manager == "apt":
        # A name with no candidate prints nothing at all; an unknown name also exits 0.
        return "Candidate:" in result.stdout and "Candidate: (none)" not in result.stdout
    return bool(result.stdout.strip())


def package_exists(manager: str | None, package: str) -> bool:
    if not manager or not package:
        return False
    if manager == "brew":
        return _brew_has_formula(package)
    return _linux_has_package(manager, package)


def _install_command(manager: str, package: str) -> list[str]:
    return {
        "brew": ["brew", "install", package],
        "apt": ["sudo", "apt-get", "install", "-y", package],
        "dnf": ["sudo", "dnf", "install", "-y", package],
        "pacman": ["sudo", "pacman", "-S", "--noconfirm", package],
        "winget": [
            "winget", "install", "--id", package, "--exact",
            "--accept-package-agreements", "--accept-source-agreements",
        ],
    }[manager]


def locate(tool: Tool) -> tuple[str | None, str]:
    """Find ``tool``, honouring its environment overrides and alternative names."""

    for variable in tool.env_overrides:
        value = os.environ.get(variable)
        if value and (os.path.isfile(value) or shutil.which(value)):
            return value, f"${variable}"
    found = shutil.which(tool.name)
    if found:
        return found, "PATH"
    for alias in tool.aliases:
        found = shutil.which(alias)
        if found:
            return found, f"PATH ({alias})"
    return None, "PATH"


def check(tiers: Iterable[str] | None = None, manager: str | None = None) -> list[ToolStatus]:
    """Report every tool's presence and, when missing, how to get it here."""

    wanted = set(tiers) if tiers else set(TIERS)
    if manager is None:
        manager = package_manager()
    statuses: list[ToolStatus] = []
    for tool in TOOLS:
        if tool.tier not in wanted:
            continue
        path, source = locate(tool)
        status = ToolStatus(tool=tool, path=path, source=source)
        if path is None:
            package = tool.package_for(manager)
            if package and not package_exists(manager, package):
                package = None  # the package moved, was renamed, or never existed here
            if package:
                status.installable = True
                status.command = _install_command(manager or "", package)
        statuses.append(status)
    return statuses


def missing(statuses: Iterable[ToolStatus]) -> list[ToolStatus]:
    return [status for status in statuses if not status.present]


def install(statuses: Iterable[ToolStatus], dry_run: bool = False, timeout: int = 1800) -> list[dict[str, object]]:
    """Run the install command for every missing, installable tool.

    Returns one result row per attempt. Tools with no package for this platform are
    reported as ``skipped`` with their manual instructions rather than being retried.
    """

    results: list[dict[str, object]] = []
    for status in statuses:
        if status.present:
            continue
        if not status.installable:
            results.append(
                {
                    "name": status.tool.name,
                    "status": "manual",
                    "reason": status.tool.manual or "no package available for this platform",
                }
            )
            continue
        if dry_run:
            results.append({"name": status.tool.name, "status": "would_run", "command": status.command})
            continue
        try:
            proc = subprocess.run(status.command, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            results.append({"name": status.tool.name, "status": "error", "command": status.command, "error": str(exc)})
            continue
        # Re-probe rather than trusting the exit code: a formula can install under a
        # different binary name, and a "success" that leaves nothing on PATH is a failure.
        path, _ = locate(status.tool)
        results.append(
            {
                "name": status.tool.name,
                "status": "installed" if path else "failed",
                "command": status.command,
                "returncode": proc.returncode,
                "path": path,
                "stderr": proc.stderr[-2000:],
            }
        )
    return results


#: The image the generated tb/run_klee.py falls back to when KLEE is not native.
KLEE_IMAGE = "klee/klee:latest"


def container_diagnostics(image: str = KLEE_IMAGE) -> dict[str, object]:
    """Probe the three things the KLEE container route depends on.

    `doctor` reports these because the route has three independent preconditions and a
    failure in any one of them looks the same from outside: the CLI must exist, a daemon
    must answer, that daemon must run LINUX containers, and the image must already be
    local (the route never pulls on its own). Surfacing them separately is what makes a
    "why did it not use the container" question answerable on a machine that is not this
    one -- notably Windows, where the daemon may be in Windows-container mode.
    """

    diagnostics: dict[str, object] = {"image": image}
    cli = shutil.which("docker")
    diagnostics["cli"] = cli
    if cli is None:
        diagnostics["daemon"] = "not installed"
        return diagnostics

    try:
        info = subprocess.run(
            ["docker", "info", "--format", "{{.OSType}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostics["daemon"] = f"unusable: {exc}"
        return diagnostics

    diagnostics["daemon"] = "ok" if info.returncode == 0 else "not running"
    diagnostics["info_returncode"] = info.returncode
    diagnostics["os_type"] = info.stdout.strip()
    diagnostics["info_stderr"] = info.stderr.strip()[-400:]
    if info.returncode != 0:
        return diagnostics

    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostics["image_present"] = f"unusable: {exc}"
        return diagnostics
    diagnostics["image_present"] = inspect.returncode == 0
    diagnostics["inspect_returncode"] = inspect.returncode
    diagnostics["inspect_stderr"] = inspect.stderr.strip()[-400:]
    return diagnostics


def summary_line(status: ToolStatus) -> str:
    if status.present:
        return f"  ok       {status.tool.name:<12} {status.path}  [{status.source}]"
    if status.installable:
        return f"  MISSING  {status.tool.name:<12} install: {' '.join(status.command)}"
    hint = status.tool.manual or "no package available for this platform"
    return f"  MISSING  {status.tool.name:<12} {hint}"
