#!/usr/bin/env python3
"""Verify every sweep in a run root was generated under the same configuration.

An ablation only measures its factor if nothing else moved. When two agents drive sweeps
into one run root, "we agreed to use the same settings" is not a guarantee -- it is a hope.
This turns it into a check: read the ``run_config`` each sweep stamps into its
``results.jsonl`` and refuse the run root if an *invariant* field disagrees across arms.

Fields are in one of three classes:

``INVARIANT``
    Must be identical everywhere. Model, backend, simulator timeouts, whether testbench
    shims were applied, benchmark path. A difference here means two arms are not
    comparable and any delta between them is confounded.

``ARM_FACTOR``
    Exactly what an arm is allowed to vary, and the reason it exists: ``plan``,
    ``evidence_policy``, ``max_repair_rounds``. A difference is expected; what is NOT
    expected is two arms differing in more than one of them at once, which would make the
    delta unattributable.

``SAMPLING``
    ``samples`` may differ between a headline arm and a deep-sampling arm, but pass@k
    comparisons across arms with different n are only valid at k <= min(n). Reported, not
    refused.

Exit codes: 0 all sweeps agree, 1 an invariant field disagrees, 2 nothing to check.

Usage:
    python scripts/check_generation_parity.py runs/paper_20260831
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

INVARIANT = (
    "model",
    "backend",
    "benchmark",
    "apply_shims",
    "sim_timeout",
    "compile_timeout",
    "llm_retries",
)

ARM_FACTOR = ("plan", "evidence_policy", "max_repair_rounds")

SAMPLING = ("samples",)

#: The arm each factor combination is supposed to be. Used only to name what an arm looks
#: like in the report; an unrecognised combination is described rather than rejected.
BASELINE_FACTORS = {"plan": True, "evidence_policy": "logs", "max_repair_rounds": 2}


def first_run_config(results: Path) -> "dict | None":
    """The run_config stamped on the first complete row, or None."""

    try:
        text = results.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        config = row.get("run_config")
        if isinstance(config, dict):
            return config
    return None


def describe_arm(config: dict) -> str:
    diffs = [f"{k}={config.get(k)!r}" for k in ARM_FACTOR
             if config.get(k) != BASELINE_FACTORS.get(k)]
    return "baseline" if not diffs else ", ".join(diffs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_root", type=Path)
    ap.add_argument("--quiet", action="store_true", help="only print problems")
    args = ap.parse_args()

    configs: "dict[str, dict]" = {}
    for results in sorted(args.run_root.glob("*/results.jsonl")):
        config = first_run_config(results)
        if config is not None:
            configs[results.parent.name] = config

    # Baselines and external-RTL sweeps construct no model at all, so their model/backend
    # fields are legitimately null. Comparing them against model-backed arms would report a
    # difference that is a property of the mode, not a configuration drift.
    model_backed = {
        name: c for name, c in configs.items()
        if c.get("backend") not in (None, "") and c.get("model") not in (None, "")
    }

    if not model_backed:
        print("no model-backed sweeps with a run_config found -- nothing to check")
        return 2

    problems: "list[str]" = []
    for field in INVARIANT:
        seen: "dict[object, list[str]]" = defaultdict(list)
        for name, config in model_backed.items():
            seen[json.dumps(config.get(field), sort_keys=True)].append(name)
        if len(seen) > 1:
            detail = "; ".join(
                f"{value} <- {', '.join(sorted(arms))}" for value, arms in sorted(seen.items())
            )
            problems.append(f"INVARIANT `{field}` disagrees across arms: {detail}")

    if not args.quiet:
        print(f"model-backed sweeps: {len(model_backed)}")
        width = max(len(n) for n in model_backed)
        for name, config in sorted(model_backed.items()):
            n = config.get("samples")
            print(f"  {name:<{width}}  n={n}  {describe_arm(config)}")
        others = sorted(set(configs) - set(model_backed))
        if others:
            print(f"not model-backed (no config to compare): {', '.join(others)}")

        ns = {c.get("samples") for c in model_backed.values()}
        if len(ns) > 1:
            print(
                f"\nnote: sample counts differ across arms ({sorted(ns)}). That is allowed, "
                f"but a cross-arm pass@k comparison is only valid at k <= {min(x for x in ns if x)}."
            )

        multi = [
            name for name, config in model_backed.items()
            if sum(1 for k in ARM_FACTOR if config.get(k) != BASELINE_FACTORS.get(k)) > 1
        ]
        if multi:
            print(
                "\nnote: these arms vary more than one factor from the baseline, so a delta "
                "against the baseline is not attributable to a single cause: "
                + ", ".join(sorted(multi))
            )

    if problems:
        print("\nGENERATION PARITY FAILED")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nArms generated under different invariant settings are not comparable, and no "
            "amount of downstream analysis recovers the comparison. Regenerate the odd arm "
            "into a fresh --out-dir under the agreed configuration."
        )
        return 1

    print("\ngeneration parity OK: every model-backed arm shares the invariant configuration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
