#!/usr/bin/env python3
"""Capture everything needed to write a Bambu backend for c2hlsc-agent.

Run on the machine where Bambu is installed (read-only apart from a temp dir):

    python3 scripts/probe_bambu.py            # or: python3 probe_bambu.py

It records Bambu's version, the flags this project would need, and — most importantly —
what Bambu actually PRODUCES for a trivial design: the file tree, where the Verilog lands,
and what a simulation run prints. Paste the whole output back.

Nothing is installed and nothing outside a temporary directory is written.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BAMBU = os.environ.get("BAMBU_BIN", "bambu")
SEP = "=" * 78


def run(argv: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        print(f"  !! {argv[0]} not found")
    except subprocess.TimeoutExpired:
        print(f"  !! timed out after {timeout}s: {' '.join(argv)}")
    except OSError as exc:
        print(f"  !! {type(exc).__name__}: {exc}")
    return None


def section(title: str) -> None:
    print(f"\n{SEP}\n{title}\n{SEP}")


def main() -> int:
    section("1. IDENTITY")
    where = shutil.which(BAMBU)
    print(f"bambu on PATH : {where or 'NOT FOUND — set BAMBU_BIN=/path/to/bambu'}")
    print(f"platform      : {sys.platform}")
    if not where:
        return 1
    proc = run([BAMBU, "--version"], timeout=60)
    if proc:
        print((proc.stdout + proc.stderr).strip()[:1200])

    section("2. FLAGS THIS PROJECT WOULD USE (grepped from --help)")
    proc = run([BAMBU, "--help"], timeout=60)
    help_text = (proc.stdout + proc.stderr) if proc else ""
    print(f"(--help is {len(help_text)} chars; showing only the relevant flags)")
    wanted = [
        "top-fname", "simulate", "simulator", "generate-tb", "testbench",
        "device-name", "clock-period", "evaluation", "output-directory",
        "no-clean", "std=", "compiler", "print-dot", "C-no-parse", "benchmark-name",
        "generate-interface", "channels", "memory-allocation", "experimental-setup",
    ]
    for line in help_text.splitlines():
        if any(w in line for w in wanted):
            print("  " + line.rstrip()[:160])

    section("3. SIMULATORS / DEVICES ADVERTISED")
    for pat in (r"--simulator[^\n]*", r"VERILATOR|ICARUS|MODELSIM|XSIM", r"--device-name[^\n]*"):
        hits = re.findall(pat, help_text)
        print(f"  {pat}: {sorted(set(hits))[:8]}")

    section("4. LIVE RUN ON A TRIVIAL DESIGN")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        src = work / "dut.c"
        src.write_text(
            "int add_scalars(int a, int b) {\n    return a + b;\n}\n", encoding="utf-8"
        )
        # Bambu wants a testbench for simulation; supply the simplest possible one so we
        # learn whether --generate-tb / --simulate work and what they emit.
        tb = work / "tb.xml"
        tb.write_text(
            '<?xml version="1.0"?>\n<function>\n'
            '  <testbench a="3" b="4"/>\n'
            '  <testbench a="-1" b="1"/>\n'
            '</function>\n',
            encoding="utf-8",
        )
        for label, argv in (
            ("synthesis only", [BAMBU, "--top-fname=add_scalars", "dut.c", "--no-clean"]),
            ("with simulation", [BAMBU, "--top-fname=add_scalars", "dut.c",
                                 "--generate-tb=tb.xml", "--simulate", "--no-clean"]),
        ):
            print(f"\n--- {label} ---")
            print(f"    cmd: {' '.join(argv)}")
            proc = run(argv, cwd=work, timeout=900)
            if proc is None:
                continue
            print(f"    exit: {proc.returncode}")
            out = (proc.stdout + proc.stderr).strip().splitlines()
            print("    last 25 lines of output:")
            for line in out[-25:]:
                print("      " + line[:150])

        section("5. WHAT BAMBU PRODUCED (file tree)")
        files = sorted(p for p in work.rglob("*") if p.is_file())
        print(f"  {len(files)} files under the work dir:")
        for p in files[:60]:
            rel = p.relative_to(work)
            print(f"    {str(rel):<52} {p.stat().st_size:>9} bytes")
        if len(files) > 60:
            print(f"    ... and {len(files) - 60} more")

        section("6. GENERATED VERILOG — module header (this is the RTL contract)")
        vs = [p for p in files if p.suffix in (".v", ".sv", ".vhd")]
        print(f"  HDL files: {[str(p.relative_to(work)) for p in vs][:10]}")
        for p in vs:
            text = p.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"\bmodule\s+add_scalars\b", text)
            if m:
                print(f"\n  --- top module in {p.relative_to(work)} ---")
                print("\n".join("    " + l for l in text[m.start():m.start() + 1400].splitlines()[:45]))
                break
        else:
            print("  (no module named add_scalars found — paste any HDL file names above)")

        section("7. REPORT / RESULT FILES (QoR source)")
        for p in files:
            if p.suffix in (".xml", ".json", ".csv") or "result" in p.name.lower():
                print(f"\n  --- {p.relative_to(work)} (first 30 lines) ---")
                try:
                    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[:30]:
                        print("    " + line[:150])
                except OSError as exc:
                    print(f"    (unreadable: {exc})")
    print(f"\n{SEP}\nEND — paste everything above.\n{SEP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
