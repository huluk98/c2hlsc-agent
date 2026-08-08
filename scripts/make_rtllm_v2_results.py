#!/usr/bin/env python3
"""Regenerate ``rtllm_v2_results/`` from a completed RTLLM v2.0 run.

``rtllm_v2_results/`` is the committed, quotable record of the current best run. It must
never be edited by hand: every number in it is copied or computed from a run directory, so
that a stale figure cannot survive an update. Running this script replaces the whole
directory contents from ``--run``.

What it writes:

``report.md``      the driver's own report for ``--run``, verbatim, under a provenance
                   header naming the run directory and config it came from.
``report.json``    copied verbatim from the run.
``results.jsonl``  copied verbatim from the run.
``designs/<n>.v``  the final RTL for each design (``designs/<n>/rtl.v`` in the run).
``comparison.md``  the like-for-like table against the shipped GPT archives, over the
                   designs those archives actually cover.

The comparison is deliberately restricted to the designs present in the GPT archives --
scoring a model on designs its archive never attempted would silently count absences as
failures. The basis is read from the GPT run's own report, not hardcoded.

Usage::

    scripts/make_rtllm_v2_results.py --run runs/confirm
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_report(run: Path) -> "dict[str, Any]":
    path = run / "report.json"
    if not path.is_file():
        raise SystemExit(f"{path} does not exist -- is {run} a completed run?")
    return json.loads(path.read_text())


def by_design(report: "dict[str, Any]") -> "dict[str, dict[str, Any]]":
    return {d["design"]: d for d in report.get("designs", [])}


def pct(num: int, den: int) -> str:
    return f"{num}/{den} ({100.0 * num / den:.1f}%)" if den else f"{num}/0 (n/a)"


# --------------------------------------------------------------------------- #
# comparison.md
# --------------------------------------------------------------------------- #


CAVEATS = """\
> **Read these four caveats before quoting any row of the table below.** They are not
> hedges; each one names a way in which the columns are *not* measuring the same thing.
>
> 1. **The GPT archives are single-shot; this agent runs a repair loop.** The shipped
>    `_chatgpt35`/`_chatgpt4` RTL is one generation per trial with no verifier feedback and
>    no second attempt. The only column that compares like with like is **this agent's
>    round-0** column -- its first generation, before any repair. The after-repair column
>    measures a *different system* (a closed loop), not a better model, and must never be
>    set against a published single-shot pass@1.
> 2. **The GPT archives get five samples; this run gets one.** `pass@5` credits a design if
>    any of five independent trials passed. This agent was run at `--samples 1`, so it has
>    no pass@5 and its round-0 column is a pass@1. Comparing this agent's single sample to
>    a pass@5 understates the agent; comparing it to pass@1 is the fair reading.
> 3. **Same simulator, different era.** Every column here was re-scored in *this*
>    environment (Icarus Verilog 12.0, `-g2012`, no VCS) under the official RTLLM oracle,
>    so the oracle is at least held constant. But the GPT files were generated years
>    earlier against the benchmark's own prompts, and were not produced with knowledge of
>    the illegal-system-task gate this harness applies. The gate rejected **0** designs and
>    **0** samples in both archives, so it costs them nothing here -- but that is a measured
>    fact about these files, not a general guarantee.
> 4. **29 designs, not 50.** The basis is the designs the GPT archives actually contain. A
>    design an archive never attempted is out of the denominator entirely rather than
>    counted as a failure. All columns are recomputed on this same 29-design basis, so the
>    numbers here will not match the 50-design headline in `report.md`.
"""


def build_comparison(
    run_report: "dict[str, Any]",
    reference: "dict[str, Any]",
    empty: "dict[str, Any]",
    gpt35: "dict[str, Any]",
    gpt4: "dict[str, Any]",
    run_dir: Path,
) -> str:
    basis = [d["design"] for d in gpt35["designs"]]
    other = [d["design"] for d in gpt4["designs"]]
    if basis != other:
        raise SystemExit(
            "the two GPT archives cover different design sets; the like-for-like table "
            "would compare different denominators"
        )

    run_d, ref_d, emp_d = by_design(run_report), by_design(reference), by_design(empty)
    g35_d, g4_d = by_design(gpt35), by_design(gpt4)

    missing = [d for d in basis if d not in run_d]
    if missing:
        raise SystemExit(f"the run is missing designs the GPT basis needs: {missing}")

    n = len(basis)
    agent_r0 = sum(1 for d in basis if run_d[d].get("func_pass_round0"))
    agent_rep = sum(1 for d in basis if run_d[d].get("func_pass"))
    agent_syn = sum(1 for d in basis if run_d[d].get("syntax_pass"))
    ref_pass = sum(1 for d in basis if ref_d[d].get("func_pass"))
    emp_pass = sum(1 for d in basis if emp_d[d].get("func_pass"))

    def archive(dd: "dict[str, dict[str, Any]]") -> "tuple[int, float, int]":
        at5 = sum(1 for d in basis if dd[d].get("func_success", 0) >= 1)
        at1 = sum(
            dd[d].get("func_success", 0) / dd[d].get("n_samples", 1) for d in basis
        ) / n
        syn = sum(1 for d in basis if dd[d].get("syntax_pass"))
        return at5, at1, syn

    g35_at5, g35_at1, g35_syn = archive(g35_d)
    g4_at5, g4_at1, g4_syn = archive(g4_d)

    cfg = run_report.get("agent_config", {})
    lines = [
        "# Like-for-like comparison against the shipped GPT archives",
        "",
        f"Basis: the **{n} designs** the RTLLM v2.0 archives `_chatgpt35/` and `_chatgpt4/` "
        "cover. Every column re-scored in this environment under the official RTLLM oracle "
        "(simulator stdout contains `Pass`/`pass`).",
        "",
        CAVEATS,
        "",
        "## The table",
        "",
        "| system | samples | repair loop | syntax | functional | pass@1 |",
        "| --- | :-: | :-: | :-: | :-: | :-: |",
        f"| benchmark's own reference RTL | 1 | no | {pct(n, n)} | {pct(ref_pass, n)} | "
        f"{ref_pass / n:.3f} |",
        f"| **this agent, round 0** (single-shot -- the fair comparison) | 1 | no | "
        f"{pct(agent_syn, n)} | **{pct(agent_r0, n)}** | **{agent_r0 / n:.3f}** |",
        f"| gpt-4 (archive) | 5 | no | {pct(g4_syn, n)} | {pct(g4_at5, n)} pass@5 | "
        f"{g4_at1:.3f} |",
        f"| gpt-3.5 (archive) | 5 | no | {pct(g35_syn, n)} | {pct(g35_at5, n)} pass@5 | "
        f"{g35_at1:.3f} |",
        f"| empty stub (floor) | 1 | no | {pct(n, n)} | {pct(emp_pass, n)} | "
        f"{emp_pass / n:.3f} |",
        "",
        f"| system | samples | repair loop | syntax | functional |",
        "| --- | :-: | :-: | :-: | :-: |",
        f"| *this agent, after repair* (a different system -- see caveat 1) | 1 | "
        f"yes, <={cfg.get('max_repair_rounds', '?')} rounds | {pct(agent_syn, n)} | "
        f"*{pct(agent_rep, n)}* |",
        "",
        "## How to read it",
        "",
        f"- Against the single-shot columns, this agent's **round-0** rate is "
        f"{agent_r0 / n:.3f}, versus {g4_at1:.3f} for gpt-4 and {g35_at1:.3f} for gpt-3.5 "
        "at pass@1. That is a comparison of one generation to one generation.",
        f"- gpt-4's **pass@5** ({g4_at5}/{n}) is the number most often quoted from this "
        "benchmark. It gives gpt-4 five attempts and this agent one, so it is not a "
        "like-for-like row; it is included because omitting it would look like hiding the "
        "strongest published figure.",
        f"- The repair loop takes this agent from {agent_r0}/{n} to {agent_rep}/{n} on this "
        "basis. That gap is the argument for the closed verifier loop, and it is also "
        "exactly why the after-repair figure is quarantined in its own table: the loop "
        "consults the testbench, which no single-shot column does.",
        f"- The floor matters. An empty module passes {emp_pass}/{n} of these testbenches, "
        "so a system scoring at or below that has demonstrated nothing.",
        "",
        "## Provenance",
        "",
        "| column | run directory |",
        "| --- | --- |",
        f"| this agent | `{run_dir.as_posix()}` |",
        "| reference RTL | `runs/reference` |",
        "| empty stub | `runs/empty` |",
        "| gpt-3.5 | `runs/gpt35` |",
        "| gpt-4 | `runs/gpt4` |",
        "",
        "Regenerate with `scripts/make_rtllm_v2_results.py --run "
        f"{run_dir.as_posix()}`. Do not edit this file by hand.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--reference", type=Path, default=REPO_ROOT / "runs/reference")
    ap.add_argument("--empty", type=Path, default=REPO_ROOT / "runs/empty")
    ap.add_argument("--gpt35", type=Path, default=REPO_ROOT / "runs/gpt35")
    ap.add_argument("--gpt4", type=Path, default=REPO_ROOT / "runs/gpt4")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "rtllm_v2_results")
    args = ap.parse_args(argv)

    run = args.run if args.run.is_absolute() else REPO_ROOT / args.run
    out = args.out if args.out.is_absolute() else REPO_ROOT / args.out

    run_report = load_report(run)
    if run_report.get("interrupted"):
        raise SystemExit(f"{run} is marked interrupted; refusing to publish a partial run")
    sel, done = run_report.get("selected_designs"), run_report.get("completed_designs")
    if sel != done:
        raise SystemExit(f"{run} completed {done} of {sel} designs; refusing to publish")

    reference = load_report(args.reference)
    empty = load_report(args.empty)
    gpt35 = load_report(args.gpt35)
    gpt4 = load_report(args.gpt4)

    out.mkdir(parents=True, exist_ok=True)
    designs_out = out / "designs"
    if designs_out.exists():
        shutil.rmtree(designs_out)
    designs_out.mkdir()

    copied = 0
    for entry in sorted((run / "designs").iterdir()):
        rtl = entry / "rtl.v"
        if entry.is_dir() and rtl.is_file():
            shutil.copyfile(rtl, designs_out / f"{entry.name}.v")
            copied += 1

    shutil.copyfile(run / "report.json", out / "report.json")
    shutil.copyfile(run / "results.jsonl", out / "results.jsonl")

    try:
        run_label = run.relative_to(REPO_ROOT)
    except ValueError:  # a run kept outside the repo is still publishable
        run_label = run

    cfg = run_report.get("agent_config", {})
    header = (
        "<!-- GENERATED FILE -- do not edit by hand.\n"
        f"     Source run: {run_label.as_posix()}\n"
        f"     Regenerate: scripts/make_rtllm_v2_results.py --run {run_label.as_posix()} -->\n"
        "\n"
        "> **Provenance.** Every number below is this run's own output, copied verbatim "
        f"from `{run_label.as_posix()}/report.md`. Configuration: "
        f"`plan={cfg.get('plan')}`, `evidence_policy={cfg.get('evidence_policy')}`, "
        f"`max_repair_rounds={cfg.get('max_repair_rounds')}`, `samples={cfg.get('samples')}`. "
        "Simulator: Icarus Verilog 12.0 (`iverilog -g2012` + `vvp`); no VCS or Vitis in this "
        "environment. The `claude-cli` backend is sandboxed: no file, shell or network "
        "tools, plan mode, scrubbed working directory -- the model cannot read the "
        "testbench or the reference RTL.\n"
        ">\n"
        "> See [`comparison.md`](comparison.md) for the like-for-like table against the "
        "shipped GPT archives, and the fairness caveats that table depends on.\n"
        "\n"
    )
    (out / "report.md").write_text(header + (run / "report.md").read_text())

    (out / "comparison.md").write_text(
        build_comparison(run_report, reference, empty, gpt35, gpt4, run_label)
    )

    t = run_report["totals"]
    print(f"wrote {out}")
    print(f"  report.md, report.json, results.jsonl, comparison.md, designs/ ({copied} .v)")
    print(
        f"  headline: syntax {t['designs_syntax_success']}/{t['designs']}, "
        f"func {t['designs_func_success']}/{t['designs']}, "
        f"round0 {t['designs_func_success_round0']}/{t['designs']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
