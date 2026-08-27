"""Shared helpers for tests that build and run a generated project.

Tests drive projects the way the agent does -- through ``tb/host_build.py`` -- rather
than through ``make``. That matters for two reasons: it exercises the path the agent
actually takes, and it means the suite genuinely runs on native Windows instead of
skipping most of itself because ``make`` is absent.

``make`` is still covered, by one test that checks the alias forwards correctly.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _first(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


#: Any GCC/Clang-style driver will do; the generated projects use GCC-style flags.
CXX = _first("g++", "clang++", "c++")
GCOV = _first("gcov", "llvm-cov")

HAVE_BUILD = CXX is not None
HAVE_GCOV = HAVE_BUILD and GCOV is not None
HAVE_KLEE = HAVE_GCOV and _first("klee") is not None and _first("clang++") is not None
HAVE_MAKE = _first("make") is not None

#: Windows needs Developer Mode or elevation to create symlinks; tests that build a
#: synthetic PATH out of them have to be skipped rather than failed there.
def _symlinks_available() -> bool:
    import os
    import tempfile

    if os.name != "nt":
        return True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_text("", encoding="utf-8")
            (Path(tmp) / "link").symlink_to(target)
        return True
    except (OSError, NotImplementedError):
        return False


HAVE_SYMLINKS = _symlinks_available()

BUILD_REASON = "a C++ compiler (g++, clang++ or c++) is required"
GCOV_REASON = "a C++ compiler and gcov are required"
KLEE_REASON = "a native klee and clang++ are required"


def run_target(project: Path, target: str, **kwargs) -> subprocess.CompletedProcess:
    """Run one generated-project target through the cross-platform driver."""

    command = [sys.executable, str(Path(project) / "tb" / "host_build.py"), target]
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(command, cwd=str(project), **kwargs)


def run_make(project: Path, *targets: str, **kwargs) -> subprocess.CompletedProcess:
    """Run targets through make. Only for the test that proves the alias still forwards."""

    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(["make", "-C", str(project), *targets], **kwargs)
