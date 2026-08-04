#!/usr/bin/env python3
"""Bucket an RTLLM run against the reference and empty-stub calibration runs.

A raw RTLLM score mixes four different things: designs whose testbench is vacuous (an
empty module passes), designs no RTL can pass, designs where the benchmark's own
reference RTL is wrong but the testbench is still satisfiable, and real results. Only
the last two say anything about the RTL generator.

This script reads three ``results.jsonl`` files -- your run, a ``--reference`` run and an
``--empty-baseline`` run -- and sorts every design into one of five buckets, then reports
the score over the designs that actually carry signal.

Usage:
    python3 scripts/triage_rtllm_run.py \\
        --run runs/agent_opus --reference runs/reference --empty runs/empty
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FREE = "free"
UNSCORABLE = "unscorable"
REFERENCE_WRONG = "reference_wrong"
PASSED = "passed"
FAILED = "failed"

BUCKET_LABELS = {
    FREE: "free (empty stub passes -- vacuous oracle)",
    UNSCORABLE: "unscorable (neither reference nor you pass)",
    REFERENCE_WRONG: "reference wrong (you pass a testbench the reference fails)",
    PASSED: "real signal -- passed",
    FAILED: "REAL FAILURE",
}

#: Buckets that carry information about the RTL generator.
SIGNAL_BUCKETS = (REFERENCE_WRONG, PASSED, FAILED)


def load_results(directory: Path) -> dict[str, dict]:
    """Read ``<directory>/results.jsonl`` into {design: row}."""

    path = directory / "results.jsonl"
    if not path.exists():
        raise SystemExit(f"no results.jsonl in {directory} (did that run finish?)")
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn final line from an interrupted sweep
        rows[row["design"]] = row
    if not rows:
        raise SystemExit(f"{path} has no usable rows")
    return rows


def passed(rows: dict[str, dict], design: str) -> bool:
    row = rows.get(design)
    return bool(row and row.get("func_success", 0) > 0)


def round0_passed(rows: dict[str, dict], design: str) -> bool:
    """Did the FIRST attempt pass, before any repair round?"""

    row = rows.get(design)
    if not row:
        return False
    for sample in row.get("samples", []):
        rounds = sample.get("rounds") or []
        if rounds and rounds[0].get("sim", {}).get("func_pass"):
            return True
    return False


def classify(design: str, run: dict, reference: dict, empty: dict) -> str:
    if passed(empty, design):
        return FREE
    ref_ok, run_ok = passed(reference, design), passed(run, design)
    if not ref_ok and not run_ok:
        return UNSCORABLE
    if not ref_ok and run_ok:
        return REFERENCE_WRONG
    return PASSED if run_ok else FAILED


def triage(run: dict, reference: dict, empty: dict) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {k: [] for k in BUCKET_LABELS}
    for design in sorted(run):
        buckets[classify(design, run, reference, empty)].append(design)
    return buckets


def render(buckets: dict[str, list[str]], run: dict, markdown: bool) -> str:
    signal = [d for b in SIGNAL_BUCKETS for d in buckets[b]]
    scored = len(signal)
    won = len(buckets[PASSED]) + len(buckets[REFERENCE_WRONG])
    r0 = sum(1 for d in signal if round0_passed(run, d))
    total = sum(len(v) for v in buckets.values())
    noise = len(buckets[FREE]) + len(buckets[UNSCORABLE])

    lines: list[str] = []
    add = lines.append
    if markdown:
        add("| bucket | n | designs |")
        add("| --- | :-: | --- |")
        for key, label in BUCKET_LABELS.items():
            names = ", ".join(f"`{d}`" for d in buckets[key]) or "-"
            add(f"| {label} | {len(buckets[key])} | {names} |")
        add("")
        add(f"**Signal basis: {scored}/{total} designs** ({noise} carry no information and are excluded).")
        add("")
        add("| metric | value |")
        add("| --- | :-: |")
        add(f"| functional pass, signal basis | **{won}/{scored}** ({100 * won / scored:.1f}%) |")
        add(f"| first-round pass (no repair), signal basis | {r0}/{scored} ({100 * r0 / scored:.1f}%) |")
        add(f"| lifted by the repair loop | {won - r0} |")
        if buckets[FAILED]:
            add("")
            add("Debug these: " + ", ".join(f"`{d}`" for d in buckets[FAILED]))
    else:
        for key, label in BUCKET_LABELS.items():
            names = ", ".join(buckets[key]) or "-"
            add(f"{label:<58} {len(buckets[key]):>2}  {names}")
        add("")
        add(f"signal basis: {scored}/{total} designs ({noise} excluded as uninformative)")
        add(f"functional pass, signal basis: {won}/{scored} ({100 * won / scored:.1f}%)")
        add(f"first-round pass (no repair):   {r0}/{scored} ({100 * r0 / scored:.1f}%)")
        add(f"lifted by the repair loop:      {won - r0}")
        if buckets[FAILED]:
            add(f"debug these: {', '.join(buckets[FAILED])}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage_rtllm_run.py",
        description="Bucket an RTLLM run against the reference and empty-stub calibration runs, "
        "and score it over the designs that actually carry signal.",
    )
    parser.add_argument("--run", required=True, type=Path, help="out-dir of the run to triage")
    parser.add_argument("--reference", required=True, type=Path, help="out-dir of a --reference run")
    parser.add_argument("--empty", required=True, type=Path, help="out-dir of an --empty-baseline run")
    parser.add_argument("--markdown", action="store_true", help="emit markdown tables instead of plain text")
    parser.add_argument("--json", action="store_true", help="emit the raw buckets as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run = load_results(args.run)
    reference = load_results(args.reference)
    empty = load_results(args.empty)

    missing = sorted(set(run) - set(reference))
    if missing:
        print(
            f"warning: {len(missing)} design(s) absent from the reference run, treated as "
            f"reference-fails: {', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}",
            file=sys.stderr,
        )

    buckets = triage(run, reference, empty)
    if args.json:
        print(json.dumps(buckets, indent=2, sort_keys=True))
        return 0
    print(render(buckets, run, args.markdown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
