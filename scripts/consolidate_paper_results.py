#!/usr/bin/env python3
"""Consolidate every sweep under a run root into one structured pass/fail record.

Walks the per-suite ``results.jsonl`` files that ``run_chstone.py``, ``run_rosetta.py``
and ``run_rtllm_v2.py`` each write, and emits a single ``consolidated.json`` plus a
``consolidated.md`` summary. Every row carries the artifact paths for its evidence, so a
failure can be opened without hunting for which sweep directory it came from.

The point is that a reader can answer three questions from one file:

  * which (suite, arm, sample, benchmark) cells passed, and on what oracle;
  * for the failures, which named wall each one stopped at;
  * where the logs for any single cell live on disk.

Nothing here re-judges a run. It reads the verdicts the runners already wrote and
refuses to invent one where a runner recorded none -- an absent verdict is reported as
``unknown``, never as a failure, because "the sweep has not reached this row yet" and
"this row failed" are different facts and only the runner knows which applies.

Usage:
    python scripts/consolidate_paper_results.py runs/paper_20260831
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

#: How each runner spells the fields this script needs. Keeping the differences in one
#: table means a schema change in a runner is a one-line edit here rather than a new
#: branch in the walker below.
SUITE_SCHEMAS = {
    "chstone": {
        "id_field": "benchmark",
        "ok_field": "ok",
        "family_field": "failure_family",
        "rung_field": "rung",
        "artifact_fields": ("project_dir",),
    },
    "rosetta": {
        "id_field": "app",
        "ok_field": "ok",
        "family_field": "failure_family",
        "rung_field": "rung",
        "artifact_fields": ("project_dir",),
    },
    # rtllm rows are per-design aggregates over ``samples``, not one row per attempt, so
    # they go through ``normalise_rtllm`` below rather than this table.
}


#: Every suite this script understands. ``rtllm`` is here but not in SUITE_SCHEMAS
#: because its rows take a different shape (see ``normalise_rtllm``).
KNOWN_SUITES = ("chstone", "rosetta", "rtllm")


def classify_suite(sweep_dir: Path) -> str:
    name = sweep_dir.name.lower()
    for suite in KNOWN_SUITES:
        if name.startswith(suite):
            return suite
    return "unknown"


def read_rows(results_path: Path) -> "list[dict]":
    rows = []
    for line in results_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A sweep killed mid-write leaves a torn final line. Skipping it loses one
            # row; aborting would lose the whole sweep.
            continue
    return rows


def normalise_rtllm(row: dict, sweep: str, results_path: Path) -> dict:
    """One design's aggregate over its samples.

    ``func_success`` counts how many of ``n_samples`` passed, so the design-level verdict
    is pass@k. Both the official and the strict oracle are carried: the strict count is
    the guard against a testbench that prints a failure line and a pass banner in the same
    run, and the two must be reported together or not at all.
    """

    n = row.get("n_samples") or len(row.get("samples") or [])
    passed = row.get("func_success")
    samples = row.get("samples") or []
    strict = sum(1 for s in samples if s.get("func_pass_strict"))

    if passed is None:
        verdict = "unknown"
    elif passed > 0:
        verdict = "pass"
    else:
        verdict = "fail"

    # A design fails at possibly different walls on different samples; report the set
    # rather than collapsing to one, and use the first for the wall histogram.
    families = [s.get("failure_family") for s in samples if s.get("failure_family")]

    return {
        "suite": "rtllm",
        "sweep": sweep,
        "id": row.get("design"),
        "verdict": verdict,
        "failure_family": families[0] if families else None,
        "failure_families_all_samples": families or None,
        "samples_passed": passed,
        "samples_total": n,
        "samples_passed_strict": strict,
        "round0_passes": sum(1 for s in samples if s.get("func_pass_round") == 0),
        "syntax_success": row.get("syntax_success"),
        "evidence_policy": row.get("evidence_policy"),
        "oracle_derived_evidence": row.get("oracle_derived_evidence"),
        "llm_errors": sum(1 for s in samples if s.get("llm_error")),
        "duration_s": row.get("wall_s"),
        "artifacts": {"results_jsonl": str(results_path)},
    }


def normalise(row: dict, suite: str, sweep: str, results_path: Path) -> dict:
    if suite == "rtllm":
        return normalise_rtllm(row, sweep, results_path)
    schema = SUITE_SCHEMAS.get(suite)
    if schema is None:
        return {"suite": suite, "sweep": sweep, "id": None, "verdict": "unknown"}

    ok = row.get(schema["ok_field"])
    if ok is True:
        verdict = "pass"
    elif ok is False:
        verdict = "fail"
    else:
        verdict = "unknown"

    artifacts = {}
    for field in schema["artifact_fields"]:
        if row.get(field):
            artifacts[field] = str(row[field])
    artifacts["results_jsonl"] = str(results_path)

    record = {
        "suite": suite,
        "sweep": sweep,
        "id": row.get(schema["id_field"]),
        "verdict": verdict,
        "failure_family": row.get(schema["family_field"]),
        "artifacts": artifacts,
    }
    if schema["rung_field"]:
        record["rung_reached"] = row.get(schema["rung_field"])

    # Oracle-strength and integrity fields, carried through when a runner records them so
    # that a pass can be read together with the evidence that it is not vacuous.
    for field in (
        "mutation_check",
        "stimulus_count",
        "vitis_available",
        "rungs_not_attempted",
        "func_pass_strict",
        "shim_applied",
        "repair_rounds_run",
        "duration_s",
    ):
        if field in row:
            record[field] = row[field]
    return record


def main(argv: "list[str]") -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 2

    records: "list[dict]" = []
    for results_path in sorted(root.glob("*/results.jsonl")):
        sweep_dir = results_path.parent
        suite = classify_suite(sweep_dir)
        for row in read_rows(results_path):
            records.append(normalise(row, suite, sweep_dir.name, results_path))

    by_sweep: "dict[str, Counter]" = defaultdict(Counter)
    walls: "dict[str, Counter]" = defaultdict(Counter)
    for rec in records:
        by_sweep[rec["sweep"]][rec["verdict"]] += 1
        if rec["verdict"] == "fail" and rec.get("failure_family"):
            walls[rec["sweep"]][rec["failure_family"]] += 1

    # A pass whose mutation check did not go red is not evidence of anything; surface it
    # separately rather than letting it sit inside the pass count.
    false_greens = [
        r for r in records
        if r["verdict"] == "pass" and r.get("mutation_check") not in (None, "red")
    ]

    summary = {
        "run_root": str(root),
        "sweeps": {
            sweep: {
                "pass": counts["pass"],
                "fail": counts["fail"],
                "unknown": counts["unknown"],
                "total": sum(counts.values()),
                "walls": dict(walls[sweep]),
            }
            for sweep, counts in sorted(by_sweep.items())
        },
        "records": records,
        "suspect_passes": false_greens,
    }

    (root / "consolidated.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    lines = [f"# Consolidated results — `{root}`", ""]
    lines.append("| sweep | pass | fail | unknown | total |")
    lines.append("| --- | --: | --: | --: | --: |")
    for sweep, s in summary["sweeps"].items():
        lines.append(
            f"| `{sweep}` | {s['pass']} | {s['fail']} | {s['unknown']} | {s['total']} |"
        )
    lines.append("")
    if false_greens:
        lines.append(
            f"> **{len(false_greens)} pass(es) without a red mutation check.** "
            "These prove nothing and must not be counted."
        )
        lines.append("")
    lines.append("## Failure walls")
    lines.append("")
    for sweep, s in summary["sweeps"].items():
        if not s["walls"]:
            continue
        lines.append(f"### `{sweep}`")
        lines.append("")
        lines.append("| wall | n |")
        lines.append("| --- | :-: |")
        for wall, n in sorted(s["walls"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{wall}` | {n} |")
        lines.append("")
    (root / "consolidated.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"{len(records)} rows from {len(by_sweep)} sweep(s)")
    for sweep, s in summary["sweeps"].items():
        print(f"  {sweep:<24} pass={s['pass']:>3} fail={s['fail']:>3} unknown={s['unknown']:>3}")
    if false_greens:
        print(f"  !! {len(false_greens)} pass(es) without a red mutation check")
    print(f"wrote {root / 'consolidated.json'} and {root / 'consolidated.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
