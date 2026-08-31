#!/usr/bin/env python3
"""Run the HLS-LeVeri 107-pair benchmark through the conversion agent.

The benchmark (https://github.com/cz-5f/HLS-LeVeri) ships 107 entries, each with the
original C (``c_src``), its testbench, a *reference* HLS-C implementation (``hls_src``),
a ``test.h`` of compile-time constants, and the Vitis tcl that names the top.

Unlike CHStone, every top here takes real arguments, which is what this agent's
stimulus-based oracle is built for. The reference ``hls_src`` also gives a QoR baseline:
the agent's design can be compared against a human-written HLS-C for the same function.

Outcomes are classified by first blocking cause so a run reports where the suite stops.
pass@k uses the unbiased estimator (Chen et al. 2021); ``--samples`` must be >= max(k).
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from math import comb
from pathlib import Path

DEFAULT_BENCH = Path("third_party/HLS-LeVeri/HLS_LeVeri_benchmark.json")

BLOCKERS: tuple[tuple[str, str, str], ...] = (
    ("variable-length-array", r"\bvariable-length-array\b",
     "array bound is not a literal (often a #define/const, i.e. a false rejection)"),
    ("pointer-arithmetic", r"\bpointer-arithmetic\b", "unrestricted pointer arithmetic on an argument"),
    ("dynamic-allocation", r"\bdynamic-allocation\b", "malloc/free in the top"),
    ("file-io", r"\bfile-io\b", "printf/scanf in the top"),
    ("unsupported-stdlib-call", r"\bunsupported-stdlib-call\b", "exit/abort/rand in the top"),
    ("recursion", r"\brecursion\b", "the top calls itself"),
    ("function-pointer", r"\bfunction-pointer\b", "indirect call through a function pointer"),
    ("unbounded-loop", r"\bunbounded-loop\b", "for(;;) or while(1)"),
    ("missing-pointer-bound", r"\bmissing-pointer-bound\b",
     "array length not resolved, so the testbench used the default 16"),
    ("compile-error", r"\berror:", "generated source did not compile"),
    ("top-not-found", r"top function .* not found", "the tcl's set_top is not in c_src"),
)


@dataclass
class Sample:
    index: int
    status: str
    blocker: str | None = None
    detail: str = ""
    trace: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass
class BenchResult:
    ident: str
    top: str
    samples: list[Sample] = field(default_factory=list)

    @property
    def passes(self) -> int:
        return sum(1 for s in self.samples if s.passed)

    @property
    def first_blocker(self) -> str | None:
        for s in self.samples:
            if s.blocker:
                return s.blocker
        return None


def classify(text: str) -> tuple[str | None, str]:
    for name, pattern, description in BLOCKERS:
        if re.search(pattern, text, re.I):
            return name, description
    return None, ""


def tcl_facts(tcl: str) -> dict[str, str]:
    """Top, part and clock period, taken from the benchmark's own Vitis script."""

    facts: dict[str, str] = {}
    top = re.search(r"set_top\s+(\S+)", tcl or "")
    if top:
        facts["top"] = top.group(1)
    part = re.search(r"set_part\s*\{?\s*\"?([\w\-]+)\"?\s*\}?", tcl or "")
    if part:
        facts["part"] = part.group(1)
    clock = re.search(r"create_clock\s+-period\s+([\d.]+)", tcl or "")
    if clock:
        facts["clock"] = clock.group(1)
    return facts


def prepare(entry: dict, work: Path) -> tuple[Path, dict[str, str]]:
    """Write one entry to disk as the agent expects: input.c beside its test.h."""

    work.mkdir(parents=True, exist_ok=True)
    (work / "test.h").write_text(entry.get("testh") or "", encoding="utf-8")
    (work / "input.c").write_text(entry.get("c_src") or "", encoding="utf-8")
    (work / "reference_hls.cpp").write_text(entry.get("hls_src") or "", encoding="utf-8")
    return work / "input.c", tcl_facts(entry.get("tcl") or "")


def run_sample(ident: str, source: Path, facts: dict[str, str], out: Path,
               index: int, extra: list[str], timeout: int) -> Sample:
    project = out / f"{ident}_s{index}"
    command = [
        sys.executable, "-m", "c2hlsc_agent", "convert",
        "--input", str(source),
        "--top", facts.get("top", ""),
        "--out", str(project),
        "--seed", str(1000 + index),
        "--new-run",
        *extra,
    ]
    if facts.get("part"):
        command += ["--part", facts["part"]]
    if facts.get("clock"):
        command += ["--clock", facts["clock"]]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Sample(index, "error", "timeout", f"exceeded {timeout}s")

    combined = (run.stdout or "") + (run.stderr or "")
    report = project / "conversion_report.json"
    if report.exists():
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        status = str(data.get("status", "error"))
        blob = combined + json.dumps(data)[:20000]
        if status == "pass":
            blocker, detail = None, ""
        else:
            blocker, detail = classify(blob)
            if blocker is None:
                # A failure that matches no known pattern still has to say something.
                # Reporting it as "-" hid an oracle mismatch behind the same dash a pass
                # uses, which is the one thing a blocker column must never do.
                phase = next(
                    (name for name in ("software_equivalence", "trace_consistency", "csim", "csynth", "cosim")
                     if str(data.get(name, "")) not in ("pass", "skipped", "")),
                    "unknown",
                )
                blocker = f"{phase}-{data.get(phase, status)}"
                mismatches = data.get("mismatches") or []
                detail = json.dumps(mismatches[0])[:200] if mismatches else f"convert status {status}"
        trace = str(data.get("trace_consistency", ""))
        return Sample(index, status, blocker, detail, trace)

    blocker, detail = classify(combined)
    return Sample(index, "error", blocker or "no-report", detail or combined.strip()[-300:])


def pass_at_k(n: int, c: int, k: int) -> float:
    if k > n:
        raise ValueError(f"pass@{k} needs at least {k} samples, got {n}")
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--benchmark", default=str(DEFAULT_BENCH), help="HLS_LeVeri_benchmark.json")
    parser.add_argument("--out", default="build/leveri", help="where to write projects and the report")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N entries")
    parser.add_argument("--ids", default="", help="comma-separated entry ids")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--k", default="1")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--convert-arg", action="append", default=[])
    parser.add_argument("--json", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    path = Path(args.benchmark)
    if not path.exists():
        print(f"{path} not found; fetch it with:\n"
              f"  git clone --depth 1 https://github.com/cz-5f/HLS-LeVeri third_party/HLS-LeVeri",
              file=sys.stderr)
        return 1

    entries = json.loads(path.read_text(encoding="utf-8"))
    wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
    if wanted:
        entries = [e for e in entries if str(e.get("id")) in wanted]
    if args.limit:
        entries = entries[: args.limit]

    ks = sorted({int(k) for k in args.k.split(",") if k.strip()})
    if max(ks) > args.samples:
        print(f"pass@{max(ks)} needs --samples >= {max(ks)} (got {args.samples})", file=sys.stderr)
        return 2

    out = Path(args.out)
    prepared_root = out / "prepared"
    results: list[BenchResult] = []

    for entry in entries:
        ident = str(entry.get("id"))
        source, facts = prepare(entry, prepared_root / ident)
        result = BenchResult(ident, facts.get("top", "?"))
        if not facts.get("top"):
            result.samples.append(Sample(0, "error", "top-not-found", "no set_top in tcl"))
        else:
            for index in range(args.samples):
                result.samples.append(
                    run_sample(ident, source, facts, out, index, args.convert_arg, args.timeout)
                )
        results.append(result)
        if not args.quiet:
            print(f"{ident:7} {result.top:28.28} {result.passes}/{args.samples} "
                  f"blocker={result.first_blocker or '-'}")

    total = len(results)
    histogram: collections.Counter[str] = collections.Counter()
    for r in results:
        histogram[r.first_blocker or ("none" if r.passes else "unclassified")] += 1

    report = {
        "benchmark": str(path),
        "entries": total,
        "samples_per_entry": args.samples,
        "pass_at_k": {
            f"pass@{k}": round(sum(pass_at_k(len(r.samples), r.passes, k) for r in results) / total, 4)
            for k in ks
        } if total else {},
        "blocker_histogram": dict(histogram.most_common()),
        "lines": {
            r.ident: {
                "top": r.top,
                "passes": r.passes,
                "samples": len(r.samples),
                "blocker": r.first_blocker,
                "statuses": [s.status for s in r.samples],
                "trace_consistency": [s.trace for s in r.samples],
            }
            for r in results
        },
    }

    print()
    for label, value in report["pass_at_k"].items():
        print(f"{label:8} = {value:.4f}  ({value * total:.1f}/{total})")
    print("\nblockers:")
    for key, count in histogram.most_common():
        print(f"  {count:3d}  {key}")

    destination = Path(args.json) if args.json else out / "leveri_report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
