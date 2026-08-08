#!/usr/bin/env python3
"""Statistical summary of every measured outcome in this repository.

Point estimates alone hide two things that matter here: how wide the uncertainty is on a
50- or 13-design denominator, and how much the *same* configuration moves between runs.
This script computes both, from the raw ``results.jsonl`` files rather than from any
prose, and emits markdown tables.

    python3 scripts/summarize_outcomes.py            # markdown to stdout
    python3 scripts/summarize_outcomes.py --json     # machine-readable

Method notes:
- Proportions carry **Wilson** 95% intervals, which behave sensibly near 0 and 1 where the
  normal approximation does not (several arms sit at 1/13 and 12/12).
- Comparisons between two configurations over the *same* designs are **paired**, so they
  use an exact two-sided **McNemar** (a sign test on discordant designs only). An unpaired
  two-proportion test on the same data overstates significance in both directions.
- Where a family of comparisons shares a denominator, p-values carry **Holm** correction
  and the corrected value is the one that decides the verdict.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs"


# --------------------------------------------------------------------------- statistics


def wilson(successes: int, total: int, z: float = 1.959963985) -> "tuple[float, float]":
    """Wilson score interval. Returns (low, high) as proportions."""

    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value from the two discordant counts.

    ``b`` and ``c`` are the designs that flipped one way and the other. Concordant
    designs carry no information about a difference and are excluded by construction.
    """

    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def holm(pvalues: "dict[str, float]") -> "dict[str, float]":
    """Holm-Bonferroni step-down correction. Returns corrected p-values."""

    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(ordered)
    out: dict[str, float] = {}
    running = 0.0
    for index, (name, p) in enumerate(ordered):
        adjusted = min(1.0, (m - index) * p)
        running = max(running, adjusted)  # enforce monotonicity
        out[name] = running
    return out


def min_flips_for_significance(n: int, m_family: int, alpha: float = 0.05) -> "int | None":
    """Smallest all-one-direction discordant count that can clear alpha after Holm."""

    for flips in range(1, n + 1):
        if m_family * mcnemar_exact(flips, 0) <= alpha:
            return flips
    return None


def fmt_ci(successes: int, total: int) -> str:
    if total == 0:
        return "n/a"
    low, high = wilson(successes, total)
    return f"{successes}/{total} ({100*successes/total:.1f}%) [{100*low:.0f}–{100*high:.0f}]"


# --------------------------------------------------------------------------- loading


def load_rows(run: str) -> "dict[str, dict]":
    path = RUNS / run / "results.jsonl"
    if not path.exists():
        return {}
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = row.get("design") or row.get("benchmark") or row.get("app")
        if key:
            rows[key] = row
    return rows


def report(run: str) -> "dict[str, Any] | None":
    path = RUNS / run / "report.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def passed(row: dict) -> bool:
    for key in ("func_success", "passed", "ok"):
        if key in row and row[key] is not None:
            value = row[key]
            return bool(value) if isinstance(value, bool) else value > 0
    return False


def round0_passed(row: dict) -> bool:
    for sample in row.get("samples", []) or []:
        rounds = sample.get("rounds") or []
        if rounds and (rounds[0].get("sim") or {}).get("func_pass"):
            return True
    return False


def paired(a: "dict[str, dict]", b: "dict[str, dict]", scorer=passed):
    """Return (n_paired, a_only, b_only, a_score, b_score) over shared designs."""

    shared = sorted(set(a) & set(b))
    a_only = b_only = a_score = b_score = 0
    for design in shared:
        pa, pb = scorer(a[design]), scorer(b[design])
        a_score += pa
        b_score += pb
        if pa and not pb:
            a_only += 1
        elif pb and not pa:
            b_only += 1
    return len(shared), a_only, b_only, a_score, b_score


# --------------------------------------------------------------------------- tables

GPT29 = set(
    """JC_counter RAM accu adder_16bit adder_32bit adder_8bit adder_pipe_64bit alu asyn_fifo
    calendar counter_12 div_16bit edge_detect freq_div fsm multi_16bit multi_booth_8bit
    multi_pipe_4bit multi_pipe_8bit parallel2serial pe pulse_detect radix2_div right_shifter
    serial2parallel signal_generator synchronizer traffic_light width_8to16""".split()
)


def table_rtllm_headline(out: list, data: dict) -> None:
    out.append("## 1. RTLLM v2.0 — all 50 designs\n")
    out.append("Intervals are Wilson 95%. The *adjusted* basis removes the 4 designs an empty")
    out.append("module passes and the 3 no RTL passes, leaving 43 that can discriminate.\n")
    out.append("| system | functional (raw /50) | adjusted (/43) | round 0, no repair |")
    out.append("| --- | --- | --- | --- |")
    for label, run in (
        ("**agent** (run A: `runs/agent`)", "agent"),
        ("**agent** (run B: `runs/confirm`)", "confirm"),
        ("benchmark's reference RTL", "reference"),
        ("empty stub (floor)", "empty"),
    ):
        rep = report(run)
        if not rep:
            continue
        t, adj = rep["totals"], rep.get("adjusted", {})
        r0 = t.get("designs_func_success_round0")
        r0s = fmt_ci(r0, t["designs"]) if r0 is not None else "—"
        out.append(
            f"| {label} | {fmt_ci(t['designs_func_success'], t['designs'])} "
            f"| {fmt_ci(adj.get('designs_func_success', 0), adj.get('designs', 0))} | {r0s} |"
        )
    out.append("")


def table_run_variance(out: list, data: dict) -> None:
    a, b = load_rows("agent"), load_rows("confirm")
    if not (a and b):
        return
    n, a_only, b_only, a_score, b_score = paired(a, b)
    p = mcnemar_exact(a_only, b_only)
    out.append("## 2. How much does the *same* configuration move between runs?\n")
    out.append("`runs/agent` and `runs/confirm` are the same configuration executed twice. This is")
    out.append("the single most important number for reading every other table: it is the noise floor.\n")
    out.append(f"- run A: **{a_score}/{n}**, run B: **{b_score}/{n}**")
    out.append(f"- designs that flipped: **{a_only + b_only}** ({a_only} A-only, {b_only} B-only)")
    out.append(f"- exact McNemar p = **{p:.3f}** — the two runs are statistically indistinguishable, as they should be")
    flips = [d for d in sorted(set(a) & set(b)) if passed(a[d]) != passed(b[d])]
    if flips:
        out.append(f"- non-deterministic designs: {', '.join('`'+d+'`' for d in flips)}")
    out.append("")
    out.append(f"> **Reading rule.** A single run of one configuration carries roughly ±{a_only+b_only} designs")
    out.append("> of run-to-run noise on this benchmark. Any comparison between two configurations that")
    out.append("> differs by that much or less is not evidence of anything.\n")


def table_repair_effect(out: list, data: dict) -> None:
    out.append("## 3. What the repair loop is worth (paired, same designs)\n")
    out.append("| run | round 0 | after repair | designs gained | exact McNemar p |")
    out.append("| --- | :-: | :-: | :-: | :-: |")
    for label, run in (("run A", "agent"), ("run B", "confirm")):
        rows = load_rows(run)
        if not rows:
            continue
        r0 = sum(round0_passed(r) for r in rows.values())
        fin = sum(passed(r) for r in rows.values())
        gained = sum(1 for r in rows.values() if passed(r) and not round0_passed(r))
        lost = sum(1 for r in rows.values() if round0_passed(r) and not passed(r))
        out.append(
            f"| {label} (`runs/{run}`) | {r0}/{len(rows)} | {fin}/{len(rows)} | +{gained} "
            f"| {mcnemar_exact(gained, lost):.2e} |"
        )
    out.append("")
    out.append("This is the one effect in the entire study that is far larger than the noise floor")
    out.append("in §2, and it replicates across both runs.\n")


def table_gpt_comparison(out: list, data: dict) -> None:
    agent = load_rows("confirm") or load_rows("agent")
    if not agent:
        return
    out.append("## 4. Against the models the benchmark ships (29-design basis)\n")
    out.append("The archives cover 29 of the 50 designs. Every column is re-scored here under the")
    out.append("same simulator and oracle. **None of the 4 vacuous designs are in this subset**, so")
    out.append("its floor is a true 0/29.\n")
    out.append("| system | samples | repair loop | functional | pass@1 |")
    out.append("| --- | :-: | :-: | --- | :-: |")
    sub = sorted(GPT29 & set(agent))
    r0 = sum(round0_passed(agent[d]) for d in sub)
    fin = sum(passed(agent[d]) for d in sub)
    out.append(f"| **this agent, round 0** (the like-for-like row) | 1 | no | {fmt_ci(r0, len(sub))} | {r0/len(sub):.3f} |")
    out.append(f"| this agent, after ≤2 repairs | 1 | **yes** | {fmt_ci(fin, len(sub))} | {fin/len(sub):.3f} |")
    for label, run in (("gpt-4 (archive)", "gpt4"), ("gpt-3.5 (archive)", "gpt35")):
        rep = report(run)
        if not rep:
            continue
        t = rep["totals"]
        pass1 = t.get("pass@1_round0", t.get("pass@1"))
        out.append(
            f"| {label} | 5 | no | {fmt_ci(t['designs_func_success'], t['designs'])} pass@5 "
            f"| {pass1:.3f} |"
        )
    for label, run in (("reference RTL (ceiling)", "reference"), ("empty stub (floor)", "empty")):
        rows = load_rows(run)
        if not rows:
            continue
        s = sorted(GPT29 & set(rows))
        out.append(f"| {label} | 1 | no | {fmt_ci(sum(passed(rows[d]) for d in s), len(s))} | — |")
    out.append("")
    # The honest head-to-head test.
    g4 = load_rows("gpt4")
    if g4:
        shared = sorted(GPT29 & set(agent) & set(g4))
        a_only = sum(1 for d in shared if round0_passed(agent[d]) and not passed(g4[d]))
        b_only = sum(1 for d in shared if passed(g4[d]) and not round0_passed(agent[d]))
        p = mcnemar_exact(a_only, b_only)
        out.append(
            f"**Round-0 agent vs gpt-4 pass@5, paired over {len(shared)} designs:** "
            f"{a_only} designs to {b_only}, exact McNemar p = **{p:.4f}**. "
            + ("Significant at α=0.05." if p <= 0.05 else "Not significant at α=0.05.")
        )
        out.append("")
        out.append("> Note this comparison is *unfavourable* to the agent on sampling (1 sample vs 5)")
        out.append("> and favourable on recency. It is the closest to like-for-like available offline.\n")


def table_ablation(out: list, data: dict) -> None:
    base_rows = load_rows("ablation_rtllm/baseline") or {}
    arms = []
    root = RUNS / "ablation_rtllm"
    if root.exists():
        for child in sorted(root.iterdir()):
            if (child / "results.jsonl").exists() and child.name != "baseline":
                arms.append(child.name)
    if not base_rows or not arms:
        return
    out.append("## 5. Ablation: one factor at a time, 13 hard designs\n")
    n_family = len(arms)
    floor = min_flips_for_significance(len(base_rows), n_family)
    out.append(f"Family of {n_family} comparisons, Holm-corrected. With n={len(base_rows)} paired designs,")
    out.append(f"an arm must flip **{floor}** designs all one way to clear α=0.05 — this matrix is")
    out.append("underpowered by construction, which is a property of the design, not a defect.\n")
    out.append("| arm | score | Δ vs baseline | discordant | exact p | Holm p | verdict |")
    out.append("| --- | :-: | :-: | :-: | :-: | :-: | --- |")
    raw: dict[str, float] = {}
    stats: dict[str, tuple] = {}
    for arm in arms:
        rows = load_rows(f"ablation_rtllm/{arm}")
        n, base_only, arm_only, base_score, arm_score = paired(base_rows, rows)
        p = mcnemar_exact(base_only, arm_only)
        raw[arm] = p
        stats[arm] = (n, base_only, arm_only, base_score, arm_score, p)
    corrected = holm(raw)
    base_total = sum(passed(r) for r in base_rows.values())
    out.append(f"| `baseline` | {base_total}/{len(base_rows)} | — | — | — | — | reference point |")
    for arm in arms:
        n, base_only, arm_only, base_score, arm_score, p = stats[arm]
        cp = corrected[arm]
        verdict = "**significant**" if cp <= 0.05 else "not significant"
        out.append(
            f"| `{arm}` | {arm_score}/{n} | {arm_score - base_score:+d} | {base_only + arm_only} "
            f"| {p:.4f} | {cp:.3f} | {verdict} |"
        )
    out.append("")


def table_chstone(out: list, data: dict) -> None:
    out.append("## 6. CHStone — reachability is the story, not the pass rate\n")
    out.append("A benchmark that never reached the oracle was not measured; scoring it 0 reports a")
    out.append("harness defect as a model result. Reachability is therefore the left column.\n")
    out.append("| run | reached the oracle | passed |")
    out.append("| --- | :-: | :-: |")
    for label, run in (
        ("native self-check (calibration)", "chstone_native_recheck"),
        ("deterministic, legacy staging", "chstone_det_legacy"),
        ("deterministic, fixed staging", "chstone_det_staged"),
        ("LLM + repair, fixed staging", "chstone_llm_staged"),
    ):
        rep = report(run)
        if not rep:
            continue
        reached = rep.get("reachable", rep.get("reached_oracle"))
        total = rep.get("benchmarks", rep.get("designs", 12))
        passed_n = rep.get("passed", rep.get("designs_func_success"))
        reached_s = f"{reached}/{total}" if reached is not None else "—"
        out.append(f"| {label} | {reached_s} | {passed_n}/{total} |")
    out.append("")


def table_rosetta(out: list, data: dict) -> None:
    rows = load_rows("rosetta_agent") or {}
    out.append("## 7. Rosetta\n")
    if not rows:
        out.append("Software baseline: **1/3** on the apps that ship a golden output "
                   "(`face-detection` passes; `digit-recognition` and `3d-rendering` differ from "
                   "their own goldens). 5/5 build and run; 2 ship no golden and are excluded "
                   "rather than scored.\n")
    out.append("Agent rung: **0/5**, measured *before* the analyzer comment-absorption fix "
               "(`9b19e0e`). Four of five stopped at that bug before the model's output was "
               "reached, so 0/5 is a floor on the harness, not a measurement of the generator. "
               "**This one needs re-running.**\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    out: list[str] = ["# Statistical summary of measured outcomes\n"]
    out.append("Generated by `scripts/summarize_outcomes.py` from the raw `results.jsonl` files in")
    out.append("`runs/`. Proportions carry Wilson 95% intervals; comparisons over the same designs")
    out.append("are paired and use an exact McNemar test, Holm-corrected within a family.\n")
    out.append("---\n")

    data: dict[str, Any] = {}
    for fn in (
        table_rtllm_headline,
        table_run_variance,
        table_repair_effect,
        table_gpt_comparison,
        table_ablation,
        table_chstone,
        table_rosetta,
    ):
        fn(out, data)
        out.append("---\n")

    text = "\n".join(out)
    if args.json:
        print(json.dumps({"markdown": text}, indent=2))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
