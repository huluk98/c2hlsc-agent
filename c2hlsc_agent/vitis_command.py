"""Build native AMD Vitis HLS Tcl launcher commands.

Vitis installations expose one of two supported command-line front ends:

* legacy/classic ``vitis_hls -f script.tcl``;
* Unified IDE ``vitis-run --mode hls --tcl script.tcl``.

Keep this translation in one place so local verification, remote verification, and
generated project helpers cannot silently use different launch semantics.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def is_vitis_run(executable: str) -> bool:
    """Return whether *executable* is the Unified IDE ``vitis-run`` launcher."""

    return Path(executable).name.lower() in {"vitis-run", "vitis-run.bat", "vitis-run.exe"}


def vitis_tcl_command(executable: str, script: str) -> list[str]:
    """Return the native argv for evaluating an HLS Tcl script."""

    if not executable:
        raise ValueError("Vitis executable must not be empty")
    if is_vitis_run(executable):
        return [executable, "--mode", "hls", "--tcl", script]
    return [executable, "-f", script]


def find_vitis_executable(preferred: str = "vitis_hls") -> str | None:
    """Resolve a local HLS launcher, including legacy Windows Vivado HLS."""

    resolved = shutil.which(preferred)
    if resolved:
        return resolved
    if preferred == "vitis_hls":
        return shutil.which("vitis-run") or shutil.which("vivado_hls")
    return None
