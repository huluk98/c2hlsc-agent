#!/usr/bin/env python3
"""Convert every HLS-Eval design and record how far each one gets.

This is the harness behind the sweep numbers in the memory slate (`E005`-`E007`).
It runs the conservative, offline path -- no LLM, no Vitis -- so what it measures is
host-side golden-vs-HLS equivalence: a floor, not a headline.

    python3 scripts/run_hls_eval_sweep.py --data-root ~/hls-eval/hls_eval_data
    python3 scripts/run_hls_eval_sweep.py --data-root ... --raw --workers 4

Two modes, because they answer different questions:

  preprocessed (default)  Macro-expand each design with `gcc -E -P` first. Array
                          bounds and loop bounds become literals, so the generated
                          config can name a correct `length:`. Comparable with the
                          published baseline.
  --raw                   Feed the source as written, macros and helpers intact.
                          Harder, and it exercises the file-scope carry-over -- but
                          array bounds naming a macro cannot be resolved into a
                          `length:` here, so some designs run with the default bound
                          and fail equivalence for that reason rather than a real one.

Designs are independent -- each gets its own project directory -- so `--workers`
scales nearly linearly. Results are sorted by design id before writing, so the
output file does not depend on completion order.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Codes the report may carry when a design is rejected before any code is generated.
STATIC_CODES = (
    "dynamic-allocation|unsupported-stdlib-call|system-call|file-io|function-pointer"
    "|unbounded-loop|recursion|pointer-arithmetic|variable-length-array"
    "|no-observable-output|unprovable-pointer-direction|unbounded-scalar-stimulus"
)


def _discover(data_root: Path) -> list[tuple[str, str, Path]]:
    designs = []
    for family in sorted(os.listdir(data_root)):
        family_dir = data_root / family
        if not family_dir.is_dir():
            continue
        for name in sorted(os.listdir(family_dir)):
            design_dir = family_dir / name
            if (design_dir / "top.txt").exists():
                designs.append((family, name, design_dir))
    return designs


def _argument_block(source: str, top: str) -> str:
    """Derive per-argument lengths from the top function's declared array bounds."""

    match = re.search(
        r"[A-Za-z_][\w \*]*?\b" + re.escape(top) + r"\s*\(([^;{]*)\)\s*\{", source, re.S
    )
    if not match:
        return ""
    parts, depth, current = [], 0, ""
    for char in match.group(1):
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
        if char in "([<":
            depth += 1
        elif char in ")]>":
            depth -= 1
    if current.strip():
        parts.append(current)

    lines = []
    for part in parts:
        part = part.strip()
        if not part or part == "void":
            continue
        dims = re.findall(r"\[([^\]]*)\]", part)
        core = re.sub(r"\[[^\]]*\]", "", part)
        name = re.sub(r"\*", " ", core).split()[-1]
        if dims:
            try:
                length = int(eval(dims[0], {}, {})) if dims[0].strip() else None
            except Exception:
                length = None  # a macro bound; only --raw hits this
            if length:
                lines.append(f"  {name}:\n    length: {length}")
        elif "*" in core:
            lines.append(f"  {name}:\n    length: 1")
    return "arguments:\n" + "\n".join(lines) + "\n" if lines else ""


def _run_one(family: str, name: str, design_dir: Path, work: Path, raw: bool, timeout: int, repair: bool = False) -> dict:
    row = {"fam": family, "d": name, "top": "", "stage": "", "verdict": "FAIL", "why": ""}
    sources = [f for f in glob.glob(str(design_dir / "*.cpp")) if not f.endswith("_tb.cpp")]
    headers = glob.glob(str(design_dir / "*.h"))
    if not sources:
        return {**row, "stage": "no-source", "why": "no non-testbench .cpp"}
    top = (design_dir / "top.txt").read_text().strip()
    row["top"] = top

    text = (open(headers[0], errors="ignore").read() + "\n" if headers else "") + open(
        sources[0], errors="ignore"
    ).read()
    text = re.sub(r'^\s*#include\s+"[^"]*"', "", text, flags=re.M)
    system_includes = sorted(set(re.findall(r"^\s*#include\s+<[^>]*>", text, flags=re.M)))
    text = re.sub(r"^\s*#include\s+<[^>]*>", "", text, flags=re.M)
    text = re.sub(r"^\s*#pragma\s+once", "", text, flags=re.M)

    project = work / f"{family}__{name}"
    project.mkdir(parents=True, exist_ok=True)
    (project / "raw.c").write_text(text)

    if raw:
        body = text
    else:
        expanded = subprocess.run(
            ["gcc", "-E", "-P", "-x", "c", str(project / "raw.c")], capture_output=True, text=True
        )
        if expanded.returncode != 0:
            tail = expanded.stderr.strip().splitlines()
            return {**row, "stage": "preprocess", "why": (tail[-1][:90] if tail else "cpp error")}
        body = expanded.stdout
    (project / "input.c").write_text("\n".join(system_includes) + "\n" + body)

    source = (project / "input.c").read_text()
    (project / "config.yaml").write_text(
        f"input_files:\n  - input.c\ntop: {top}\nnum_tests: 4\nseed: 2\n" + _argument_block(source, top)
    )

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "c2hlsc_agent.cli", "convert",
                "--config", str(project / "config.yaml"),
                "--out", str(project / "proj"),
                "--no-llm", "--no-run-vitis", "--new-run",
            ] + (["--auto-repair"] if repair else []),
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {**row, "stage": "timeout", "why": f"exceeded {timeout}s"}

    report = project / "proj" / "conversion_report.md"
    log = project / "proj" / "software_equivalence.log"
    if not report.exists():
        tail = result.stderr.strip().splitlines()
        return {**row, "stage": "convert-crash", "why": (tail[-1][:90] if tail else "no report")}

    text = report.read_text()
    if "**PASS**" in text:
        return {**row, "stage": "host-equiv", "verdict": "PASS", "why": ""}
    if "static diagnostics contain errors" in text:
        codes = re.findall(rf"\|\s*({STATIC_CODES})\s*\|", text)
        return {**row, "stage": "static-analysis", "why": ",".join(sorted(set(codes))) or "diagnostics"}

    why = "unknown"
    if log.exists():
        content = log.read_text()
        errors = re.findall(r"error: ([^\n]{0,80})", content)
        if errors:
            why = errors[0]
        elif "Mismatch" in content:
            why = "golden-vs-hls mismatch"
        elif "Segmentation fault" in content:
            why = "testbench segfault"
    return {**row, "stage": "host-compile/equiv", "why": why}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", required=True, type=Path, help="hls_eval_data directory")
    parser.add_argument("--out", type=Path, help="write results JSON here")
    parser.add_argument("--work", type=Path, help="scratch directory (default: alongside --out)")
    parser.add_argument("--raw", action="store_true", help="skip gcc -E -P macro expansion")
    parser.add_argument(
        "--auto-repair",
        action="store_true",
        help="let the deterministic repair loop iterate on failures (off by default, matching the CLI)",
    )
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=int, default=180, help="per-design seconds")
    parser.add_argument("--limit", type=int, help="stop after this many designs")
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if not args.data_root.is_dir():
        parser.error(f"--data-root not a directory: {args.data_root}")

    work = args.work or (args.out.parent / "sweep_work" if args.out else REPO_ROOT / "sweep_work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    designs = _discover(args.data_root)[: args.limit]
    mode = "raw source" if args.raw else "preprocessed"
    repair = "repair loop on" if args.auto_repair else "single shot"
    print(f"{len(designs)} designs, {mode}, {repair}, {args.workers} worker(s)\n", flush=True)

    done = 0

    def run(entry):
        nonlocal done
        row = _run_one(*entry, work, args.raw, args.timeout, args.auto_repair)
        done += 1
        mark = "." if row["verdict"] == "PASS" else "x"
        print(mark, end="" if done % 60 else f" {done}/{len(designs)}\n", flush=True)
        return row

    if args.workers == 1:
        rows = [run(entry) for entry in designs]
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(run, designs))

    # Sort so the output never depends on completion order -- the file is evidence.
    rows.sort(key=lambda r: (r["fam"], r["d"]))
    passes = sum(1 for r in rows if r["verdict"] == "PASS")
    print(f"\n\nTOTAL {len(rows)} PASS {passes}\n")

    counts = Counter((r["fam"], r["verdict"]) for r in rows)
    print(f"{'family':11} {'n':>3} {'PASS':>5} {'FAIL':>5}")
    for family in sorted({r["fam"] for r in rows}):
        n = sum(1 for r in rows if r["fam"] == family)
        print(f"{family:11} {n:3d} {counts[(family, 'PASS')]:5d} {counts[(family, 'FAIL')]:5d}")

    print("\nfailure reasons:")
    for reason, count in Counter(r["why"][:46] for r in rows if r["verdict"] != "PASS").most_common(15):
        print(f"  {count:3d}  {reason}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
