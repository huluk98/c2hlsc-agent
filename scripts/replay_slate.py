#!/usr/bin/env python3
"""Re-verify every evidence entry in the memory slate.

The slate records what was decided and what was measured. A record that cannot be
re-checked decays into folklore, so each evidence entry names how to confirm it and
this script runs them: a test to execute, an artifact to look for, or a command to
run and compare.

    python3 scripts/replay_slate.py             # re-verify everything
    python3 scripts/replay_slate.py --id E003   # one entry
    python3 scripts/replay_slate.py --list      # show the slate without running it
    python3 scripts/replay_slate.py --workers 1 # serially, for a clean failure trace

Entries are independent, so they are checked concurrently by default. Results are
reported in slate order regardless of which finished first.

Exit status is 0 when every entry still holds, 1 when any drifted. Drift is the
point: it means the code and the record disagree, and one of them is now wrong.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SLATE_PATH = REPO_ROOT / "docs" / "memory" / "slate.yaml"

PASS, FAIL, SKIP = "hold", "DRIFT", "skip"


def _load_slate() -> dict:
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML is required: python3 -m pip install pyyaml")
    with SLATE_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _check_test(entry: dict) -> tuple[str, str]:
    target = entry["test"]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return PASS, target.split("::")[-1]
    tail = (result.stdout.strip().splitlines() or ["no output"])[-1]
    return FAIL, tail[:100]


def _check_artifact(entry: dict) -> tuple[str, str]:
    path = REPO_ROOT / entry["artifact"]
    if not path.exists():
        return FAIL, f"missing artifact {entry['artifact']}"
    if path.suffix == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(rows, list):
            passes = sum(1 for row in rows if row.get("verdict") == "PASS")
            return PASS, f"{passes}/{len(rows)} PASS recorded"
    return PASS, f"{path.stat().st_size} bytes"


def _check_command(entry: dict) -> tuple[str, str]:
    result = subprocess.run(
        entry["command"], cwd=REPO_ROOT, shell=True, capture_output=True, text=True
    )
    observed = result.stdout.strip()
    expected = str(entry.get("expect", "")).strip()
    if expected and expected not in observed:
        return FAIL, f"expected {expected!r}, observed {observed[:60]!r}"
    return PASS, observed.splitlines()[-1][:80] if observed else "ok"


CHECKERS = {"test": _check_test, "artifact": _check_artifact, "command": _check_command}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="re-verify a single evidence entry")
    parser.add_argument("--list", action="store_true", help="print the slate, run nothing")
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="entries to check concurrently (1 to serialise)",
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    slate = _load_slate()
    decisions = slate.get("decisions") or []
    evidence = slate.get("evidence") or []
    frontier = slate.get("frontier") or []

    if args.list:
        print(f"{len(decisions)} decisions, {len(evidence)} evidence, {len(frontier)} open\n")
        for item in decisions:
            print(f"  {item['id']}  {item['question'].strip().splitlines()[0][:88]}")
            print(f"        -> {item['answer'].strip().splitlines()[0][:88]}")
        print()
        for item in frontier:
            print(f"  {item['id']}  OPEN  {item['question'].strip().splitlines()[0][:82]}")
        return 0

    selected = [e for e in evidence if not args.id or e["id"] == args.id]
    if not selected:
        sys.exit(f"no evidence entry with id {args.id!r}")

    workers = min(args.workers, len(selected))
    concurrency = "" if workers == 1 else f", {workers} at a time"
    print(f"Replaying {len(selected)} evidence entr{'y' if len(selected) == 1 else 'ies'}"
          f" against {REPO_ROOT.name}{concurrency}\n")

    def check(entry: dict) -> tuple[str, str]:
        checker = CHECKERS.get(entry.get("verified_by"))
        if checker is None:
            return SKIP, f"no checker for verified_by={entry.get('verified_by')!r}"
        return checker(entry)

    if workers == 1:
        results = [check(entry) for entry in selected]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(check, selected))

    # Reported in slate order, never completion order, so two runs read the same.
    drifted = []
    for entry, (status, detail) in zip(selected, results):
        if status == FAIL:
            drifted.append(entry["id"])
        marker = {PASS: "  ok  ", FAIL: " DRIFT", SKIP: " skip "}[status]
        print(f"[{marker}] {entry['id']}  {entry['claim'].strip().splitlines()[0][:70]}")
        print(f"           {detail}")

    print()
    if drifted:
        print(f"{len(drifted)} entr{'y' if len(drifted) == 1 else 'ies'} drifted: {', '.join(drifted)}")
        print("The code and the record disagree. Update whichever is wrong.")
        return 1
    print(f"All {len(selected)} evidence entries still hold.")
    if frontier:
        print(f"{len(frontier)} question(s) still open: {', '.join(q['id'] for q in frontier)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
