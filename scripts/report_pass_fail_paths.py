#!/usr/bin/env python3
"""Emit a per-design correct/wrong report across every sweep under a run root.

``consolidate_paper_results.py`` answers "how many passed"; this answers "which ones, and
what did the wrong ones do". For each benchmark it writes the verdict per arm, the named
wall a failure stopped at, and the path to the artifact that proves it, so a failure can be
opened directly instead of being re-derived.

Three things it deliberately does NOT do:

* It never turns a missing row into a failure. A sweep still running, or one that skipped a
  benchmark, is ``-``. Counting "not yet run" as "wrong" is how a partial sweep becomes a
  false negative result.
* It never counts a pass whose mutation check did not go red. Those are listed separately
  as suspect, because a pass from an equivalence test that a deliberately wrong candidate
  also passes is not evidence.
* It does not merge the official and strict RTLLM oracles. Both are reported; a design that
  passes one and not the other is exactly the case worth seeing.

Usage:
    python scripts/report_pass_fail_paths.py runs/paper_20260831 [-o report_pass_fail.md]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

PASS_MARK = "PASS"
FAIL_MARK = "FAIL"
ABSENT_MARK = "-"


def load(root: Path) -> "list[dict]":
    consolidated = root / "consolidated.json"
    if not consolidated.exists():
        raise SystemExit(
            f"{consolidated} not found -- run scripts/consolidate_paper_results.py {root} first"
        )
    return json.loads(consolidated.read_text(encoding="utf-8"))["records"]


def build(records: "list[dict]") -> str:
    by_suite: "dict[str, dict[str, dict[str, dict]]]" = defaultdict(lambda: defaultdict(dict))
    sweeps_by_suite: "dict[str, set]" = defaultdict(set)
    for rec in records:
        suite, sweep, ident = rec["suite"], rec["sweep"], rec["id"]
        if ident is None:
            continue
        by_suite[suite][ident][sweep] = rec
        sweeps_by_suite[suite].add(sweep)

    out: "list[str]" = ["# Correct / wrong per design", ""]
    out.append(
        "`PASS` / `FAIL` are the runner's own verdicts. `-` means the sweep has no row for "
        "that design -- still running, or not applicable to that arm -- and is never counted "
        "as a failure."
    )
    out.append("")

    suspect: "list[dict]" = []

    for suite in sorted(by_suite):
        sweeps = sorted(sweeps_by_suite[suite])
        out.append(f"## {suite}")
        out.append("")
        out.append("| design | " + " | ".join(f"`{s}`" for s in sweeps) + " |")
        out.append("| --- | " + " | ".join(":-:" for _ in sweeps) + " |")
        for ident in sorted(by_suite[suite]):
            cells = []
            for sweep in sweeps:
                rec = by_suite[suite][ident].get(sweep)
                if rec is None:
                    cells.append(ABSENT_MARK)
                    continue
                if rec["verdict"] == "pass":
                    if rec.get("mutation_check") not in (None, "red"):
                        cells.append(f"{PASS_MARK}?")
                        suspect.append(rec)
                    elif rec.get("samples_total"):
                        cells.append(f"{rec['samples_passed']}/{rec['samples_total']}")
                    else:
                        cells.append(PASS_MARK)
                elif rec["verdict"] == "fail":
                    cells.append(FAIL_MARK)
                else:
                    cells.append("?")
            out.append(f"| `{ident}` | " + " | ".join(cells) + " |")
        out.append("")

        # The wrong paths, with the named wall and where to look.
        out.append(f"### {suite} — what the failures did")
        out.append("")
        out.append("| design | sweep | wall | evidence |")
        out.append("| --- | --- | --- | --- |")
        wrote_any = False
        for ident in sorted(by_suite[suite]):
            for sweep in sweeps:
                rec = by_suite[suite][ident].get(sweep)
                if rec is None or rec["verdict"] != "fail":
                    continue
                wall = rec.get("failure_family") or "(none recorded)"
                artifacts = rec.get("artifacts") or {}
                where = artifacts.get("project_dir") or artifacts.get("results_jsonl") or ""
                out.append(f"| `{ident}` | `{sweep}` | `{wall}` | `{where}` |")
                wrote_any = True
        if not wrote_any:
            out.append("| — | — | no failures recorded | — |")
        out.append("")

    if suspect:
        out.append("## Suspect passes — mutation check did not go red")
        out.append("")
        out.append(
            "A pass that a deliberately wrong candidate also passes is not evidence. "
            "These must not be counted."
        )
        out.append("")
        out.append("| design | sweep | mutation check |")
        out.append("| --- | --- | --- |")
        for rec in suspect:
            out.append(
                f"| `{rec['id']}` | `{rec['sweep']}` | `{rec.get('mutation_check')}` |"
            )
        out.append("")

    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_root", type=Path)
    ap.add_argument("-o", "--out", default="report_pass_fail.md",
                    help="output filename inside run_root (default report_pass_fail.md)")
    args = ap.parse_args()

    records = load(args.run_root)
    text = build(records)
    target = args.run_root / args.out
    target.write_text(text, encoding="utf-8")
    passes = sum(1 for r in records if r["verdict"] == "pass")
    fails = sum(1 for r in records if r["verdict"] == "fail")
    print(f"{len(records)} rows: {passes} pass, {fails} fail -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
