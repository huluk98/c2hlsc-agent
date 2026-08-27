#!/usr/bin/env python3
"""c2hlsc-agent readiness probe. Read-only: nothing is installed, nothing is committed.

Run from the repository root on the machine you intend to do real runs on:

    python scripts/check_readiness.py            # or: python check_readiness.py

It reports which tiers of evidence that machine can actually produce, and checks the
two Windows-specific failure modes that shutil.which() alone cannot detect.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OK, WARN, BAD, SKIP = "PASS", "WARN", "FAIL", "n/a"
rows: list[tuple[str, str, str, str]] = []


def row(tier: str, name: str, status: str, detail: str = "") -> None:
    rows.append((tier, name, status, detail))


def probe(name: str) -> str | None:
    return shutil.which(name)


def can_launch(argv: list[str], timeout: int = 25) -> tuple[bool, str]:
    """Actually start the process. On Windows, CreateProcess resolves .exe but NOT .bat,
    so a tool that shutil.which() finds can still fail to launch from a bare name."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return True, f"exit {proc.returncode}"
    except FileNotFoundError as exc:
        return False, f"FileNotFoundError: {exc}"
    except subprocess.TimeoutExpired:
        return True, "started (timed out, which is fine here)"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    print("=" * 78)
    print("c2hlsc-agent readiness probe")
    print("=" * 78)
    print(f"platform : {platform.platform()}")
    print(f"python   : {sys.version.split()[0]}  ({sys.executable})")
    print(f"cwd      : {Path.cwd()}")
    print()

    # ---- tier 1: host equivalence (rung 1) -------------------------------------
    cxx = os.environ.get("CXX", "g++")
    p = probe(cxx)
    row("rung 1", f"C++ compiler ({cxx})", OK if p else BAD, p or "not on PATH")
    p = probe("make")
    row("rung 1", "make", OK if p else BAD, p or "not on PATH — 'make test' cannot run")

    # The generated Makefile hardcodes `python3`; on Windows that is usually absent.
    p3, py = probe("python3"), probe("python")
    if p3:
        row("lanes", "python3 (Makefile hardcodes this)", OK, p3)
    elif py:
        row("lanes", "python3 (Makefile hardcodes this)", BAD,
            f"absent; only 'python' exists at {py} -> leveri/gcov/rtl targets will fail")
    else:
        row("lanes", "python3 (Makefile hardcodes this)", BAD, "neither python3 nor python on PATH")

    # The generated Makefile uses POSIX shell utilities.
    for tool, why in (("rm", "make clean"), ("mkdir", "make rtl-vectors")):
        p = probe(tool)
        row("lanes", f"{tool} (POSIX util used by the Makefile)", OK if p else WARN,
            p or f"absent -> `{why}` will fail outside a POSIX shell")

    # ---- tier 2: HLS (rungs 2-4) ------------------------------------------------
    found_hls = False
    for tool in ("vitis_hls", "vivado_hls"):
        p = probe(tool)
        if not p:
            row("rungs 2-4", tool, SKIP, "not on PATH")
            continue
        found_hls = True
        launched, detail = can_launch([tool, "-version"])
        if launched:
            row("rungs 2-4", tool, OK, f"{p}  (launch ok: {detail})")
        else:
            row("rungs 2-4", tool, BAD,
                f"which() FOUND it at {p} but launching the bare name FAILED -> {detail}. "
                "This is the Windows .bat/.exe asymmetry; the agent will crash rather than "
                "report a missing toolchain.")
    if not found_hls:
        row("rungs 2-4", "HLS toolchain", BAD, "no vitis_hls or vivado_hls -> rungs 2-4 unavailable")

    # ---- tier 3: ASIC backend (local PPA) ---------------------------------------
    p = probe("yosys")
    row("ASIC PPA", "yosys (synthesis + area)", OK if p else BAD, p or "not on PATH -> no area numbers")
    sta = (os.environ.get("STA_BIN") or os.environ.get("C2HLSC_STA")
           or probe("sta") or str(Path.home() / "tools/eda/opensta/bin/sta"))
    sta_ok = bool(sta) and Path(sta).exists()
    row("ASIC PPA", "OpenSTA (slack + power)", OK if sta_ok else BAD,
        sta if sta_ok else "not found -> no slack/power, so --target-slack/-power cannot be met")
    lib = os.environ.get("C2HLSC_LIBERTY")
    lib_ok = bool(lib) and Path(lib).expanduser().exists()
    local_libs = sorted(Path("syn/lib").glob("*.lib")) if Path("syn/lib").is_dir() else []
    row("ASIC PPA", "liberty file", OK if (lib_ok or local_libs) else BAD,
        (lib if lib_ok else str(local_libs[0]) if local_libs
         else "set C2HLSC_LIBERTY or place syn/lib/*.lib (e.g. Nangate45)"))

    # ---- tier 4: RTL simulation --------------------------------------------------
    iv, vvp, xsim = probe("iverilog"), probe("vvp"), probe("xsim")
    if xsim:
        row("RTL sim", "simulator", OK, f"xsim at {xsim}")
    elif iv and vvp:
        row("RTL sim", "simulator", OK, f"iverilog at {iv}")
    else:
        row("RTL sim", "simulator", WARN, "no xsim and no iverilog+vvp -> lane C and gate sim skip")

    # ---- tier 5: model backend ----------------------------------------------------
    p = probe("claude")
    if p:
        launched, detail = can_launch([p, "--version"], timeout=60)
        row("LLM", "claude CLI (subscription auth)", OK if launched else WARN, f"{p} ({detail})")
    else:
        row("LLM", "claude CLI (subscription auth)", WARN, "not on PATH")
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "C2HLSC_LLM_BASE_URL"):
        if os.environ.get(var):
            row("LLM", var, OK, "set")

    # ---- tier 6: remote Vitis ------------------------------------------------------
    for tool in ("ssh", "rsync"):
        p = probe(tool)
        row("remote", tool, OK if p else WARN, p or "absent -> --vitis-ssh unavailable")

    # ---- coverage ------------------------------------------------------------------
    for tool, tier in (("gcov", "coverage"), ("klee", "coverage")):
        p = probe(tool)
        row(tier, tool, OK if p else SKIP, p or "absent -> lane skips cleanly")

    # ---- live end-to-end: generate a project and run rung 1 -------------------------
    print("Running a live end-to-end deterministic conversion...")
    cfg = Path("examples/vector_add/config.yaml")
    if not cfg.exists():
        row("end-to-end", "deterministic convert", SKIP, "run me from the repository root")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "probe_out"
            argv = [sys.executable, "-m", "c2hlsc_agent.cli", "convert",
                    "--config", str(cfg), "--out", str(out)]
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
                tail = (proc.stdout + proc.stderr).strip().splitlines()
                detail = tail[-1][:110] if tail else f"exit {proc.returncode}"
                row("end-to-end", "convert + rung 1", OK if proc.returncode == 0 else BAD, detail)
            except Exception as exc:  # noqa: BLE001
                row("end-to-end", "convert + rung 1", BAD, f"{type(exc).__name__}: {exc}")
    print()

    # ---- report ----------------------------------------------------------------------
    w1 = max(len(r[0]) for r in rows)
    w2 = max(len(r[1]) for r in rows)
    print(f"{'TIER'.ljust(w1)}  {'COMPONENT'.ljust(w2)}  STATUS  DETAIL")
    print("-" * 78)
    for tier, name, status, detail in rows:
        print(f"{tier.ljust(w1)}  {name.ljust(w2)}  {status:6}  {detail}")
    print("-" * 78)

    bad = [r for r in rows if r[2] == BAD]
    warn = [r for r in rows if r[2] == WARN]
    print(f"\n{len(bad)} blocking, {len(warn)} degraded.\n")
    if bad:
        print("Evidence tiers this machine CANNOT produce today:")
        for tier, name, _s, detail in bad:
            print(f"  - [{tier}] {name}: {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
