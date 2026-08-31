#!/usr/bin/env python3
"""pass@k table and a failure/input corpus for a run root.

Two outputs:

``passk.md``
    pass@k per arm using the unbiased estimator from Chen et al. 2021 (the HumanEval
    convention), reported on three bases so a reader can see what each number is made of.

``errors_and_inputs.jsonl`` / ``errors_and_inputs.md``
    One record per failing cell: the specification the model was given, the artifact it
    produced, the stage that rejected it, and the tool's own words. This is the corpus a
    repair-memory system would be built from, so it keeps the *input* alongside the error
    rather than the error alone.

On pass@k and sample counts
---------------------------

The unbiased estimator is

    pass@k = 1 - C(n - c, k) / C(n, k)

for ``n`` samples of which ``c`` pass. **It is only defined for k <= n.** With n=2 samples
you can report pass@1 and pass@2; pass@5 and pass@10 are not underdetermined-but-guessable,
they are undefined, and no amount of arithmetic on 2 samples yields them. This script prints
``n/a (needs n>=k)`` rather than a number in that case, because quoting an extrapolated
pass@10 is the single easiest way to have a benchmark table rejected.

To get pass@5 you need >=5 samples per design; for pass@10, >=10. Re-run the arm with
``--samples 10`` into a NEW output directory: resuming a 2-sample run at a higher count
merges both into one basis reported under the last invocation's settings, which is exactly
the silent-corruption case ``check_resume_compatible`` exists to refuse.

Bases
-----

``totals``      every design in the arm.
``adjusted``    drops designs a port-only module with no logic also passes (a vacuous
                oracle cannot discriminate, so its score means nothing). The vacuous set is
                read from the empty-baseline sweep in this same run root, not hardcoded.
``clean``       additionally drops designs where any sample lost its model call to a
                backend error. Those are infrastructure failures, not wrong RTL, and
                scoring them as zero understates the arm.
"""

from __future__ import annotations

import argparse
import json
from math import comb
from pathlib import Path

#: Bytes of tool output kept per failing cell. Enough to carry the first error and its
#: context; the full log stays on disk and is referenced by path.
EVIDENCE_CHARS = 1600
#: Bytes of generated artifact kept per failing cell.
ARTIFACT_CHARS = 4000


def pass_at_k(n: int, c: int, k: int) -> "float | None":
    """Unbiased pass@k for ``c`` passes out of ``n`` samples, or None when k > n."""

    if k > n or n <= 0:
        return None
    if c <= 0:
        return 0.0
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def load_rows(path: Path) -> "list[dict]":
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def row_contaminated(row: dict) -> bool:
    if row.get("llm_error"):
        return True
    for sample in row.get("samples") or ():
        if not isinstance(sample, dict):
            continue
        if sample.get("llm_error"):
            return True
        for attempt in sample.get("rounds") or ():
            if isinstance(attempt, dict) and attempt.get("llm_error"):
                return True
    return False


def arm_passk(rows: "list[dict]", vacuous: "set[str]", ks: "tuple[int, ...]") -> dict:
    """pass@k on each basis, plus the sample count that bounds what is reportable."""

    bases = {
        "totals": rows,
        "adjusted": [r for r in rows if r.get("design") not in vacuous],
        "clean": [r for r in rows if r.get("design") not in vacuous and not row_contaminated(r)],
    }
    out: dict = {"n_designs": {b: len(rs) for b, rs in bases.items()}}
    # The estimator is bounded by the SMALLEST per-design sample count in the basis: an arm
    # where one design has 2 samples cannot report pass@5 even if others have more.
    for basis, rs in bases.items():
        counts = [r.get("n_samples") or len(r.get("samples") or []) for r in rs]
        n_min = min(counts) if counts else 0
        out.setdefault("n_samples", {})[basis] = n_min
        scores = {}
        for k in ks:
            vals = []
            for r in rs:
                n = r.get("n_samples") or len(r.get("samples") or [])
                c = r.get("func_success")
                if c is None:
                    continue
                v = pass_at_k(n, c, k)
                if v is None:
                    vals = None
                    break
                vals.append(v)
            scores[f"pass@{k}"] = (sum(vals) / len(vals)) if vals else None
        out.setdefault("scores", {})[basis] = scores
    return out


def collect_failures(root: Path, rtllm_root: "Path | None") -> "list[dict]":
    """One record per failing cell, carrying the input that produced it."""

    records: "list[dict]" = []
    for results in sorted(root.glob("*/results.jsonl")):
        sweep = results.parent.name
        for row in load_rows(results):
            if "design" in row:  # RTLLM
                design = row["design"]
                spec = ""
                if rtllm_root:
                    hits = list(rtllm_root.rglob(f"{design}/design_description.txt"))
                    if hits:
                        spec = hits[0].read_text(encoding="utf-8", errors="replace")
                for index, sample in enumerate(row.get("samples") or []):
                    if sample.get("func_pass"):
                        continue
                    # The verdict and the tool output live on the LAST round's `sim`, not
                    # on the sample: a sample is a sequence of generate/repair attempts and
                    # only the final one produced the result being scored.
                    rounds = [r for r in (sample.get("rounds") or []) if isinstance(r, dict)]
                    last_sim = {}
                    for attempt in reversed(rounds):
                        if isinstance(attempt.get("sim"), dict):
                            last_sim = attempt["sim"]
                            break
                    records.append({
                        "suite": "rtllm",
                        "sweep": sweep,
                        "id": design,
                        "sample": index,
                        "llm_error": sample.get("llm_error") or next(
                            (a.get("llm_error") for a in rounds if a.get("llm_error")), None),
                        "failure_family": last_sim.get("failure_family"),
                        "syntax_pass": sample.get("syntax_pass"),
                        "func_pass": sample.get("func_pass"),
                        "func_pass_strict": sample.get("func_pass_strict"),
                        "evidence_policy": sample.get("evidence_policy"),
                        "repair_rounds": sample.get("repair_rounds"),
                        "rounds_run": len(rounds),
                        "last_role": rounds[-1].get("role") if rounds else None,
                        "compile_log": (last_sim.get("compile_log") or "")[:EVIDENCE_CHARS],
                        "sim_log": (last_sim.get("sim_log") or "")[:EVIDENCE_CHARS],
                        "timed_out": last_sim.get("timed_out"),
                        "shim_applied": last_sim.get("shim_applied"),
                        "input_spec": spec,
                        "generated_artifact": (sample.get("final_rtl") or "")[:ARTIFACT_CHARS],
                        "results_jsonl": str(results),
                    })
            else:  # CHStone / Rosetta
                ident = row.get("benchmark") or row.get("app")
                if ident is None or row.get("ok"):
                    continue
                records.append({
                    "suite": "chstone" if "benchmark" in row else "rosetta",
                    "sweep": sweep,
                    "id": ident,
                    "sample": 0,
                    "failure_family": row.get("failure_family"),
                    "rung_reached": row.get("rung"),
                    "diagnostics": row.get("diagnostics") or [],
                    "evidence": (row.get("evidence") or "")[:EVIDENCE_CHARS],
                    "repair_rounds_run": row.get("repair_rounds_run"),
                    "input_source": row.get("project_dir"),
                    "results_jsonl": str(results),
                })
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_root", type=Path)
    ap.add_argument("--rtllm-root", type=Path, default=Path(r"C:\Users\luke\RTLLM"),
                    help="RTLLM checkout, to attach each design's specification to its failures")
    ap.add_argument("--k", type=int, nargs="+", default=[1, 2, 5, 10])
    args = ap.parse_args()
    root, ks = args.run_root, tuple(sorted(set(args.k)))

    empty_rows = load_rows(root / "rtllm_empty" / "results.jsonl")
    vacuous = {r["design"] for r in empty_rows if (r.get("func_success") or 0) > 0}

    arms: "dict[str, dict]" = {}
    for results in sorted(root.glob("rtllm_*/results.jsonl")):
        name = results.parent.name
        rows = load_rows(results)
        if not rows or "design" not in rows[0]:
            continue
        arms[name] = arm_passk(rows, vacuous, ks)

    lines = [f"# pass@k — `{root}`", ""]
    lines.append(
        "Unbiased estimator, Chen et al. 2021: `pass@k = 1 - C(n-c, k) / C(n, k)`. "
        "**Defined only for k <= n.** Cells reading `n/a` are not missing data that could be "
        "filled in by arithmetic; the arm does not have enough samples to estimate that k."
    )
    lines.append("")
    lines.append(f"Vacuous designs excluded from `adjusted`/`clean` ({len(vacuous)}): "
                 + ", ".join(f"`{d}`" for d in sorted(vacuous)))
    lines.append("")
    for basis in ("totals", "adjusted", "clean"):
        lines.append(f"## Basis: `{basis}`")
        lines.append("")
        lines.append("| arm | designs | n | " + " | ".join(f"pass@{k}" for k in ks) + " |")
        lines.append("| --- | --: | --: | " + " | ".join("--:" for _ in ks) + " |")
        for name, data in sorted(arms.items()):
            cells = []
            for k in ks:
                v = data["scores"][basis].get(f"pass@{k}")
                cells.append(f"{v:.3f}" if v is not None else "n/a")
            lines.append(
                f"| `{name}` | {data['n_designs'][basis]} | {data['n_samples'][basis]} | "
                + " | ".join(cells) + " |"
            )
        lines.append("")

    n_max = max((d["n_samples"]["clean"] for d in arms.values()), default=0)
    lines.append("## What is needed for the missing k")
    lines.append("")
    lines.append(
        f"The largest per-design sample count on the `clean` basis is **n={n_max}**. "
        "pass@5 needs `--samples 5`, pass@10 needs `--samples 10`, and both must go to a "
        "NEW `--out-dir`: resuming a lower-sample run at a higher count merges both into a "
        "single basis reported under the last invocation's settings."
    )
    lines.append("")
    (root / "passk.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    failures = collect_failures(root, args.rtllm_root if args.rtllm_root.exists() else None)
    with (root / "errors_and_inputs.jsonl").open("w", encoding="utf-8") as fh:
        for rec in failures:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    md = ["# Failure corpus — inputs, artifacts and the tool's own words", "",
          "One record per failing cell. The specification the model was given is kept "
          "alongside the error so this can seed a repair-memory store; full logs stay on "
          "disk and are referenced by `results_jsonl`.", ""]
    by_family: "dict[str, int]" = {}
    for rec in failures:
        fam = rec.get("failure_family") or ("llm_error" if rec.get("llm_error") else "(none)")
        by_family[fam] = by_family.get(fam, 0) + 1
    md.append("| failure family | n |")
    md.append("| --- | :-: |")
    for fam, n in sorted(by_family.items(), key=lambda kv: -kv[1]):
        md.append(f"| `{fam}` | {n} |")
    md.append("")
    md.append(f"Full records: `errors_and_inputs.jsonl` ({len(failures)} rows).")
    md.append("")
    (root / "errors_and_inputs.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"wrote {root/'passk.md'}")
    print(f"wrote {root/'errors_and_inputs.jsonl'} ({len(failures)} failing cells)")
    print(f"wrote {root/'errors_and_inputs.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
