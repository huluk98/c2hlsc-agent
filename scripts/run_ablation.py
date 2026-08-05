#!/usr/bin/env python3
"""Ablation matrix over the RTLLM v2.0 agent loop.

The question this script exists to answer is *what each ingredient of the loop is worth*,
not *how high can the score go*. It runs one arm per ingredient, each differing from a fixed
baseline in exactly one factor, over a reproducible subset of designs, and reports the
differences **with an honest statement of how little a small difference means**.

Design notes, in the order they matter:

**The subset is the informative designs, chosen reproducibly.**
    A design that passes in every arm contributes no information about any ingredient -- it
    only inflates every denominator and shrinks every apparent effect. ``--hard-subset-from``
    reads a prior run's ``results.jsonl`` and selects the designs that *failed at round 0*
    there: the ones where the loop actually had to do something. The selected list is written
    into the report so the subset is auditable, and ``--designs`` overrides it outright.

**Designs whose oracle is broken are excluded, and said so.**
    Ablating on a design that nothing can pass (``rtllm_bench.KNOWN_ORACLE_ISSUES``) or that
    an empty module passes (``rtllm_bench.VACUOUS_ORACLE_DESIGNS``) measures nothing. Both
    lists are excluded by default and both are printed in the report.

**Two evidence tracks, never conflated.**
    ``--evidence-policy logs`` and ``oracle`` feed the repair agent output produced by the
    *benchmark's own testbench*. That is oracle-derived feedback: those numbers are an upper
    bound and are not comparable to published single-shot pass@1. ``none`` and ``self`` are
    self-derived -- the agent never sees the oracle. The report separates the two and marks
    every cross-track delta.

**Uncertainty is the point, not a footnote.**
    On a ~17-design subset one design is ~6 percentage points. Nothing in this report is
    allowed to imply that a one-design difference is a real effect. Concretely:

    * Each arm's pass rate carries a **Wilson 95% interval** (successes/n stated in the cell).
    * Arms are **paired** -- the same designs under a different config -- so the delta is
      tested with an **exact McNemar test**: a two-sided binomial sign test on the discordant
      designs (baseline passed & arm failed, vs arm passed & baseline failed). Concordant
      designs carry no information about the difference and are correctly ignored.
    * Seven non-baseline arms means seven tests, so p-values are **Holm-corrected** across the
      family, and the *corrected* p decides the verdict.
    * A **paired bootstrap** (seeded, deterministic) gives a CI on the delta itself.
    * The report states, computed rather than asserted, the **minimum number of discordant
      designs** any arm would need in order to reach significance at all. At alpha=0.05 that
      is 6 -- an arm that flips 5 designs one way and 0 the other is still not significant.
      Every arm below that bar is labelled ``NOT SIGNIFICANT`` and no direction word
      ("better", "worse", "helps", "hurts") is emitted for it anywhere.

**Each arm is a subprocess of ``scripts/run_rtllm_v2.py``** with its own ``--out-dir`` under
the ablation out-dir and ``--resume`` always on, so a crashed matrix continues where it
stopped. The driver is never asked to share an out-dir between arms, and the out-dir of an
unrelated in-flight run is refused outright (see ``PROTECTED_DIRS``).

Examples::

    # what will run, without running it
    scripts/run_ablation.py --benchmark $RTLLM_ROOT --out-dir runs/ablation_hard \\
        --hard-subset-from runs/agent/results.jsonl --dry-run

    # the matrix
    scripts/run_ablation.py --benchmark $RTLLM_ROOT --out-dir runs/ablation_hard \\
        --hard-subset-from runs/agent/results.jsonl

    # confirm the winner over everything
    scripts/run_ablation.py --benchmark $RTLLM_ROOT --out-dir runs/ablation_full \\
        --full-suite --arms baseline no-plan
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import math
import os
import random
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
DRIVER_PATH = SCRIPTS_DIR / "run_rtllm_v2.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from c2hlsc_agent import rtllm_bench  # noqa: E402


RESULTS_FILE = "results.jsonl"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"
PLAN_JSON = "plan.json"
SCHEMA = "c2hlsc-ablation/1"

#: Out-dirs this script must never write into, relative to the repo root. ``runs/agent`` holds
#: the baseline agent sweep that supplies the hard subset; an arm writing there would corrupt
#: the very run the subset is derived from. Checked in both directions -- an ablation out-dir
#: may neither be inside a protected directory nor contain one.
PROTECTED_DIRS = ("runs/agent",)


# --------------------------------------------------------------------------- #
# the driver under test
# --------------------------------------------------------------------------- #


def load_driver(path: Path = DRIVER_PATH) -> Any:
    """Import ``scripts/run_rtllm_v2.py`` as a module.

    The ablation reuses the driver's own definitions of "passed at round 0" and of a report
    row instead of restating them. A second implementation of those rules that drifted from
    the driver's would silently select a different subset than the one it claims to.
    """

    existing = sys.modules.get("run_rtllm_v2")
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location("run_rtllm_v2", path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable with a real file
        raise SystemExit(f"cannot import the RTLLM driver from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = load_driver()


def driver_evidence_choices(path: Path = DRIVER_PATH) -> "tuple[str, ...]":
    """The ``--evidence-policy`` values ``run_rtllm_v2.py`` currently accepts.

    Read out of the driver's own parser rather than hardcoded, so an arm naming a policy the
    driver does not have yet is reported as blocked instead of dying in argparse thirty
    seconds into the matrix -- and starts working by itself the moment the policy lands.
    """

    try:
        parser = load_driver(path).build_parser()
    except Exception:  # noqa: BLE001 - a driver that will not import is reported, not crashed on
        return ()
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public accessor
        if "--evidence-policy" in (action.option_strings or ()):
            return tuple(action.choices or ())
    return ()


# --------------------------------------------------------------------------- #
# arms
# --------------------------------------------------------------------------- #

FACTOR_PLAN = "plan"
FACTOR_EVIDENCE = "evidence_policy"
FACTOR_ROUNDS = "max_repair_rounds"
FACTOR_SAMPLES = "samples"

#: The fixed reference configuration. Every arm below overrides exactly one of these.
BASELINE_FACTORS: "dict[str, Any]" = {
    FACTOR_PLAN: True,
    FACTOR_EVIDENCE: "logs",
    FACTOR_ROUNDS: 2,
    FACTOR_SAMPLES: 1,
}

BASELINE_ARM = "baseline"


@dataclass(frozen=True)
class ArmSpec:
    """One row of the matrix: the baseline with at most one factor changed."""

    name: str
    override: "dict[str, Any]"
    rationale: str

    def factors(self) -> "dict[str, Any]":
        merged = dict(BASELINE_FACTORS)
        merged.update(self.override)
        return merged

    @property
    def factor(self) -> "str | None":
        """The single factor this arm changes, or ``None`` for the baseline."""

        return next(iter(self.override), None)

    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", self.name).strip("-") or "arm"


#: The matrix. Data-driven so arms can be added, selected with ``--arms`` or dropped with
#: ``--skip-arms`` without touching any other code. Every entry other than the baseline must
#: override exactly one key of ``BASELINE_FACTORS`` -- that is what makes the delta
#: attributable to that ingredient, and ``validate_arms`` enforces it.
ARM_SPECS: "tuple[ArmSpec, ...]" = (
    ArmSpec(
        BASELINE_ARM,
        {},
        "plan on, evidence=logs, 2 repair rounds, 1 sample. The fixed reference; every delta "
        "in the table is measured against this arm.",
    ),
    ArmSpec(
        "no-plan",
        {FACTOR_PLAN: False},
        "Drops the rtl_planner contract agent, so the generator sees the raw specification. "
        "Measures what the planning step is worth.",
    ),
    ArmSpec(
        "evidence=none",
        {FACTOR_EVIDENCE: "none"},
        "Repair sees no tool output at all -- a blind retry. The floor of the repair loop: "
        "whatever this arm recovers is resampling, not diagnosis.",
    ),
    ArmSpec(
        "evidence=self",
        {FACTOR_EVIDENCE: "self"},
        "Repair sees only evidence the agent derived itself. The strict, self-derived track.",
    ),
    ArmSpec(
        "evidence=oracle",
        {FACTOR_EVIDENCE: "oracle"},
        "Repair sees benchmark-oracle output. An upper bound, NOT comparable to published "
        "single-shot numbers.",
    ),
    ArmSpec(
        "rounds=0",
        {FACTOR_ROUNDS: 0},
        "Generation only, no repair. Isolates the generator from the loop entirely.",
    ),
    ArmSpec(
        "rounds=1",
        {FACTOR_ROUNDS: 1},
        "One repair round. With rounds=0 and the baseline, gives the shape of the return "
        "curve rather than a single point on it.",
    ),
    ArmSpec(
        "rounds=3",
        {FACTOR_ROUNDS: 3},
        "Three repair rounds. Tests whether the loop is still paying past the default.",
    ),
)


#: Which evidence policies let the repair agent see output produced by the benchmark's own
#: testbench. ``logs`` is oracle-derived: the simulation transcript it forwards is the
#: oracle's verdict on the candidate. The baseline uses it, so most cross-track deltas in this
#: report measure exactly what oracle feedback is worth. Override with ``--track-override``
#: if a policy's semantics change.
SELF_DERIVED = "self-derived"
ORACLE_DERIVED = "oracle-derived"
UNCLASSIFIED = "unclassified"

EVIDENCE_TRACKS: "dict[str, tuple[str, str]]" = {
    "none": (SELF_DERIVED, "the repair agent sees no tool output at all"),
    "self": (SELF_DERIVED, "the repair agent sees only evidence it derived itself"),
    "logs": (ORACLE_DERIVED, "the repair agent sees the BENCHMARK testbench's simulation output"),
    "oracle": (ORACLE_DERIVED, "the repair agent sees benchmark-oracle output"),
}


def classify_track(policy: str, overrides: "dict[str, str] | None" = None) -> "tuple[str, str]":
    """``(track, reason)`` for an evidence policy."""

    if overrides and policy in overrides:
        return overrides[policy], f"track forced to {overrides[policy]!r} by --track-override"
    if policy in EVIDENCE_TRACKS:
        return EVIDENCE_TRACKS[policy]
    return (
        UNCLASSIFIED,
        f"evidence policy {policy!r} is not in EVIDENCE_TRACKS; its numbers cannot be placed "
        "in either track and must not be quoted as a headline",
    )


def validate_arms(specs: "Sequence[ArmSpec]") -> None:
    """Every non-baseline arm changes exactly one known factor. Fatal if not.

    An arm that moved two knobs at once would produce a delta nothing in the table can
    attribute, which is the one failure mode an ablation cannot survive.
    """

    seen: "set[str]" = set()
    for spec in specs:
        if spec.name in seen:
            raise SystemExit(f"duplicate arm name {spec.name!r} in ARM_SPECS")
        seen.add(spec.name)
        unknown = sorted(set(spec.override) - set(BASELINE_FACTORS))
        if unknown:
            raise SystemExit(
                f"arm {spec.name!r} overrides unknown factor(s) {unknown}; "
                f"known factors are {sorted(BASELINE_FACTORS)}"
            )
        if spec.name == BASELINE_ARM:
            if spec.override:
                raise SystemExit(f"the {BASELINE_ARM!r} arm must override nothing")
            continue
        if len(spec.override) != 1:
            raise SystemExit(
                f"arm {spec.name!r} overrides {len(spec.override)} factors "
                f"({sorted(spec.override)}); an ablation arm must change exactly one"
            )
        key, value = next(iter(spec.override.items()))
        if value == BASELINE_FACTORS[key]:
            raise SystemExit(
                f"arm {spec.name!r} sets {key}={value!r}, which is the baseline value; "
                "it would duplicate the baseline arm"
            )


def select_arms(
    names: "Sequence[str] | None",
    skip: "Sequence[str] | None",
    specs: "Sequence[ArmSpec]" = ARM_SPECS,
) -> "list[ArmSpec]":
    """Resolve ``--arms`` / ``--skip-arms``. An unknown name is fatal, never silently dropped."""

    by_name = {spec.name: spec for spec in specs}
    unknown = [n for n in list(names or ()) + list(skip or ()) if n not in by_name]
    if unknown:
        raise SystemExit(
            f"unknown arm name(s): {', '.join(sorted(set(unknown)))}. "
            f"Known arms: {', '.join(by_name)}"
        )
    chosen = [by_name[n] for n in dict.fromkeys(names)] if names else list(specs)
    dropped = set(skip or ())
    chosen = [spec for spec in chosen if spec.name not in dropped]
    if not chosen:
        raise SystemExit("--arms/--skip-arms left no arms to run")
    if BASELINE_ARM not in {spec.name for spec in chosen}:
        raise SystemExit(
            f"the {BASELINE_ARM!r} arm cannot be skipped: every delta in the report is "
            "measured against it, and a table of arms with nothing to compare them to is "
            "not an ablation. Re-run with it included."
        )
    return chosen


# --------------------------------------------------------------------------- #
# hard-subset selection
# --------------------------------------------------------------------------- #


@dataclass
class HardSubset:
    """The designs an ablation is run over, plus the full audit trail of how they were picked."""

    selected: "list[str]"
    source: "str | None" = None
    basis: str = ""
    failed_round0: "list[str]" = field(default_factory=list)
    passed_round0: "list[str]" = field(default_factory=list)
    excluded_vacuous: "list[str]" = field(default_factory=list)
    excluded_unpassable: "list[str]" = field(default_factory=list)
    excluded_backend_failed: "list[str]" = field(default_factory=list)
    backend_failed_included: "list[str]" = field(default_factory=list)

    def to_dict(self) -> "dict[str, Any]":
        return {
            "basis": self.basis,
            "source": self.source,
            "selected": list(self.selected),
            "selected_count": len(self.selected),
            "failed_round0_before_exclusions": list(self.failed_round0),
            "passed_round0_not_selected": list(self.passed_round0),
            "excluded_vacuous_oracle": list(self.excluded_vacuous),
            "excluded_unpassable_oracle": list(self.excluded_unpassable),
            "excluded_backend_failed": list(self.excluded_backend_failed),
            "backend_failed_still_included": list(self.backend_failed_included),
            "vacuous_oracle_catalogue": sorted(rtllm_bench.VACUOUS_ORACLE_DESIGNS),
            "unpassable_oracle_catalogue": sorted(rtllm_bench.KNOWN_ORACLE_ISSUES),
        }


HARD_SUBSET_BASIS = (
    "designs that FAILED FUNCTIONALLY AT ROUND 0 in the source run -- the generation before "
    "any repair. A design that passes in every arm carries no information about any "
    "ingredient, so including it only inflates every denominator and shrinks every apparent "
    "effect. Round-0 pass/fail is read with the driver's own summarize_row(), not restated."
)

FULL_SUITE_BASIS = (
    "every design discovered in the benchmark checkout (--full-suite), used to confirm a "
    "winning configuration over the whole suite rather than the informative subset."
)

EXPLICIT_BASIS = "designs named explicitly with --designs; the hard-subset rule was not applied."


def select_hard_subset(
    rows: "Sequence[dict[str, Any]]",
    *,
    source: "str | None" = None,
    exclude_vacuous: bool = True,
    exclude_unpassable: bool = True,
    exclude_backend_failed: bool = False,
) -> HardSubset:
    """Pick the informative designs out of a prior run's ``results.jsonl`` rows.

    A design is *hard* iff it did not pass functionally at round 0. Designs whose oracle is
    vacuous (an empty module passes) or unpassable (nothing passes) are dropped by default and
    recorded: an arm cannot demonstrate anything on a design whose verdict is fixed.
    """

    summaries = [driver.summarize_row(row) for row in rows]
    summaries.sort(key=lambda s: s["design"])

    failed: "list[str]" = []
    passed: "list[str]" = []
    backend_failed: "set[str]" = set()
    for summary in summaries:
        name = summary["design"]
        if summary.get("backend_failed"):
            backend_failed.add(name)
        (passed if summary.get("func_pass_round0") else failed).append(name)

    vacuous = [n for n in failed if n in rtllm_bench.VACUOUS_ORACLE_DESIGNS]
    unpassable = [n for n in failed if n in rtllm_bench.KNOWN_ORACLE_ISSUES]

    selected = list(failed)
    if exclude_vacuous:
        selected = [n for n in selected if n not in rtllm_bench.VACUOUS_ORACLE_DESIGNS]
    if exclude_unpassable:
        selected = [n for n in selected if n not in rtllm_bench.KNOWN_ORACLE_ISSUES]
    dropped_backend = [n for n in selected if n in backend_failed] if exclude_backend_failed else []
    if exclude_backend_failed:
        selected = [n for n in selected if n not in backend_failed]

    return HardSubset(
        selected=selected,
        source=source,
        basis=HARD_SUBSET_BASIS,
        failed_round0=failed,
        passed_round0=passed,
        excluded_vacuous=vacuous if exclude_vacuous else [],
        excluded_unpassable=unpassable if exclude_unpassable else [],
        excluded_backend_failed=dropped_backend,
        backend_failed_included=sorted(backend_failed & set(selected)),
    )


def full_suite_subset(
    names: "Sequence[str]",
    *,
    exclude_vacuous: bool = True,
    exclude_unpassable: bool = True,
) -> HardSubset:
    """Every discovered design, with the same oracle exclusions applied and recorded."""

    ordered = sorted(dict.fromkeys(names))
    vacuous = [n for n in ordered if n in rtllm_bench.VACUOUS_ORACLE_DESIGNS]
    unpassable = [n for n in ordered if n in rtllm_bench.KNOWN_ORACLE_ISSUES]
    selected = list(ordered)
    if exclude_vacuous:
        selected = [n for n in selected if n not in rtllm_bench.VACUOUS_ORACLE_DESIGNS]
    if exclude_unpassable:
        selected = [n for n in selected if n not in rtllm_bench.KNOWN_ORACLE_ISSUES]
    return HardSubset(
        selected=selected,
        basis=FULL_SUITE_BASIS,
        excluded_vacuous=vacuous if exclude_vacuous else [],
        excluded_unpassable=unpassable if exclude_unpassable else [],
    )


def explicit_subset(
    names: "Sequence[str]",
    *,
    exclude_vacuous: bool = False,
    exclude_unpassable: bool = False,
) -> HardSubset:
    """``--designs``: run exactly what was asked for, still recording the oracle catalogues."""

    ordered = list(dict.fromkeys(names))
    vacuous = [n for n in ordered if n in rtllm_bench.VACUOUS_ORACLE_DESIGNS]
    unpassable = [n for n in ordered if n in rtllm_bench.KNOWN_ORACLE_ISSUES]
    selected = list(ordered)
    if exclude_vacuous:
        selected = [n for n in selected if n not in rtllm_bench.VACUOUS_ORACLE_DESIGNS]
    if exclude_unpassable:
        selected = [n for n in selected if n not in rtllm_bench.KNOWN_ORACLE_ISSUES]
    return HardSubset(
        selected=selected,
        basis=EXPLICIT_BASIS,
        excluded_vacuous=vacuous if exclude_vacuous else [],
        excluded_unpassable=unpassable if exclude_unpassable else [],
        backend_failed_included=[],
    )


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #

Z_95 = 1.959963984540054


def wilson_interval(successes: int, n: int, z: float = Z_95) -> "tuple[float, float] | None":
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because at n~17 -- and especially near 0 or 1,
    where several arms will sit -- the normal interval is badly wrong and can leave [0, 1].
    """

    if n <= 0:
        return None
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value: a binomial sign test on the discordant pairs.

    ``b`` = designs the baseline passed and the arm failed, ``c`` = the reverse. Designs that
    agree carry no information about the difference and are excluded by construction -- which
    is exactly why an unpaired comparison of two 17-design rates understates the evidence in
    one direction and overstates it in the other. The exact form is used rather than the
    chi-square approximation because at these counts the approximation is not valid.
    """

    b = max(0, int(b))
    c = max(0, int(c))
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5**n)
    return min(1.0, 2.0 * tail)


def min_discordant_for_significance(alpha: float = 0.05, max_n: int = 400) -> "int | None":
    """Smallest discordant count that could reach ``alpha`` even if every flip agrees.

    Computed, not asserted. At alpha=0.05 this is 6: an arm that flips 5 designs one way and
    none the other still has p=0.0625 and is not significant. Printing this number is the
    single most effective guard against reading a one- or two-design delta as an effect.
    """

    for n in range(1, max_n + 1):
        if 2.0 * (0.5**n) <= alpha:
            return n
    return None


def holm_adjust(pvalues: "Sequence[float]") -> "list[float]":
    """Holm-Bonferroni step-down adjustment, order preserved.

    Seven arms means seven tests; at an uncorrected alpha=0.05 the chance of at least one
    false positive across the family is about 30%. Reporting uncorrected p-values next to
    seven comparisons is how an ablation table manufactures an effect.
    """

    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [1.0] * m
    running = 0.0
    for rank, index in enumerate(order):
        running = max(running, (m - rank) * pvalues[index])
        adjusted[index] = min(1.0, running)
    return adjusted


def paired_bootstrap_delta_ci(
    pairs: "Sequence[tuple[bool, bool]]",
    *,
    iterations: int = 10000,
    alpha: float = 0.05,
    seed: int = 20250805,
) -> "tuple[float, float] | None":
    """Percentile CI on ``arm_rate - baseline_rate``, resampling *designs* (the paired unit).

    Deterministic for a given seed so the reported interval is reproducible.
    """

    n = len(pairs)
    if n == 0 or iterations <= 0:
        return None
    base = [1 if a else 0 for a, _ in pairs]
    arm = [1 if b else 0 for _, b in pairs]
    rng = random.Random(seed)
    deltas: "list[float]" = []
    for _ in range(iterations):
        total = 0
        for _ in range(n):
            i = rng.randrange(n)
            total += arm[i] - base[i]
        deltas.append(total / n)
    deltas.sort()
    lo_index = int(math.floor((alpha / 2.0) * iterations))
    hi_index = int(math.ceil((1.0 - alpha / 2.0) * iterations)) - 1
    lo_index = min(max(lo_index, 0), iterations - 1)
    hi_index = min(max(hi_index, 0), iterations - 1)
    return (deltas[lo_index], deltas[hi_index])


@dataclass
class PairedComparison:
    """One arm measured against the baseline over the designs both of them ran."""

    n_paired: int
    baseline_passes: int
    arm_passes: int
    delta_designs: int
    delta_pp: "float | None"
    baseline_only: "list[str]"
    arm_only: "list[str]"
    discordant: int
    p_exact: float
    p_holm: "float | None" = None
    bootstrap_ci_pp: "tuple[float, float] | None" = None
    unpaired_designs: "list[str]" = field(default_factory=list)

    def to_dict(self) -> "dict[str, Any]":
        return {
            "n_paired": self.n_paired,
            "baseline_passes": self.baseline_passes,
            "arm_passes": self.arm_passes,
            "delta_designs": self.delta_designs,
            "delta_pp": self.delta_pp,
            "baseline_passed_arm_failed": list(self.baseline_only),
            "arm_passed_baseline_failed": list(self.arm_only),
            "discordant": self.discordant,
            "p_exact_mcnemar": self.p_exact,
            "p_holm": self.p_holm,
            "bootstrap_delta_ci_pp": list(self.bootstrap_ci_pp) if self.bootstrap_ci_pp else None,
            "designs_not_run_in_both": list(self.unpaired_designs),
        }


def compare_to_baseline(
    baseline: "dict[str, bool]",
    arm: "dict[str, bool]",
    *,
    bootstrap_iterations: int = 10000,
    alpha: float = 0.05,
    seed: int = 20250805,
) -> PairedComparison:
    """Pair an arm's per-design outcomes against the baseline's and test the difference."""

    shared = sorted(set(baseline) & set(arm))
    unpaired = sorted(set(baseline) ^ set(arm))
    baseline_only = [d for d in shared if baseline[d] and not arm[d]]
    arm_only = [d for d in shared if arm[d] and not baseline[d]]
    b, c = len(baseline_only), len(arm_only)
    n = len(shared)
    base_passes = sum(1 for d in shared if baseline[d])
    arm_passes = sum(1 for d in shared if arm[d])
    delta_pp = ((arm_passes - base_passes) / n) * 100.0 if n else None
    ci = paired_bootstrap_delta_ci(
        [(baseline[d], arm[d]) for d in shared],
        iterations=bootstrap_iterations,
        alpha=alpha,
        seed=seed,
    )
    return PairedComparison(
        n_paired=n,
        baseline_passes=base_passes,
        arm_passes=arm_passes,
        delta_designs=c - b,
        delta_pp=delta_pp,
        baseline_only=baseline_only,
        arm_only=arm_only,
        discordant=b + c,
        p_exact=mcnemar_exact_p(b, c),
        bootstrap_ci_pp=(ci[0] * 100.0, ci[1] * 100.0) if ci else None,
    )


def significance_verdict(
    comparison: PairedComparison,
    *,
    alpha: float,
    floor: "int | None",
) -> str:
    """Plain-English verdict. Emits a direction word only when the corrected p clears alpha.

    Everything else reads ``NOT SIGNIFICANT`` with the counts that make it obvious why. This
    function is the report's only source of judgement language; nothing else in the markdown
    is allowed to characterise a delta.
    """

    if comparison.n_paired == 0:
        return "not comparable (no designs run in both arms)"
    if comparison.discordant == 0:
        return f"identical outcomes on all {comparison.n_paired} designs"
    p = comparison.p_holm if comparison.p_holm is not None else comparison.p_exact
    detail = (
        f"{comparison.discordant} discordant of {comparison.n_paired} "
        f"(+{len(comparison.arm_only)}/-{len(comparison.baseline_only)}), Holm p={p:.3f}"
    )
    if p > alpha:
        if floor is not None and comparison.discordant < floor:
            return (
                f"NOT SIGNIFICANT -- cannot be: {detail}; at alpha={alpha:g} no arm reaches "
                f"significance with fewer than {floor} discordant designs"
            )
        return f"NOT SIGNIFICANT: {detail}"
    direction = "arm above baseline" if comparison.delta_designs > 0 else "arm below baseline"
    return f"significant ({direction}): {detail}"


# --------------------------------------------------------------------------- #
# reading an arm's results
# --------------------------------------------------------------------------- #

#: Report column -> the per-design boolean in the driver's summarize_row output.
METRICS = {
    "func": "func_pass",
    "round0": "func_pass_round0",
    "syntax": "syntax_pass",
    "strict": "func_pass_strict",
}


def load_arm_table(arm_dir: Path) -> "list[dict[str, Any]]":
    """Per-design summary rows for one arm, from its report.json or its results.jsonl."""

    report_path = arm_dir / REPORT_JSON
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
        table = report.get("designs")
        if isinstance(table, list) and table:
            return [row for row in table if isinstance(row, dict)]
    rows = driver.load_prior_rows(arm_dir / RESULTS_FILE)
    return [driver.summarize_row(row) for row in rows]


def load_arm_wall_seconds(arm_dir: Path) -> "dict[str, float]":
    """Per-design wall-clock from the arm's results.jsonl (report.json does not carry it)."""

    wall: "dict[str, float]" = {}
    for row in driver.load_prior_rows(arm_dir / RESULTS_FILE):
        value = row.get("wall_s")
        if isinstance(value, (int, float)):
            wall[str(row.get("design"))] = float(value)
    return wall


def outcomes(table: "Sequence[dict[str, Any]]", metric: str) -> "dict[str, bool]":
    key = METRICS[metric]
    return {str(row["design"]): bool(row.get(key)) for row in table if row.get("design")}


def _mean(values: "Sequence[float]") -> "float | None":
    return (sum(values) / len(values)) if values else None


# --------------------------------------------------------------------------- #
# running an arm
# --------------------------------------------------------------------------- #


def arm_out_dir(root: Path, spec: ArmSpec) -> Path:
    return root / spec.slug()


def arm_command(
    spec: ArmSpec,
    *,
    benchmark: Path,
    out_dir: Path,
    designs: "Sequence[str]",
    workers: int = 1,
    backend: str = "claude-cli",
    model: str = "opus",
    llm_cli_cmd: "str | None" = None,
    sim_timeout: "int | None" = None,
    compile_timeout: "int | None" = None,
    verbose: bool = False,
    python: "str | None" = None,
    driver_path: Path = DRIVER_PATH,
    extra_args: "Sequence[str]" = (),
) -> "list[str]":
    """The exact ``run_rtllm_v2.py`` invocation for one arm.

    ``--resume`` is always present. Each arm owns its out-dir, so resuming can only ever pick
    up that arm's own rows -- and the driver refuses a resume whose scoring knobs disagree
    with what is already on disk, which turns a mis-specified rerun into an error instead of
    a blended report.
    """

    factors = spec.factors()
    cmd = [
        python or sys.executable,
        str(driver_path),
        "--benchmark",
        str(benchmark),
        "--out-dir",
        str(out_dir),
    ]
    if designs:
        cmd += ["--designs", *designs]
    cmd += [
        "--samples",
        str(factors[FACTOR_SAMPLES]),
        "--max-repair-rounds",
        str(factors[FACTOR_ROUNDS]),
        "--evidence-policy",
        str(factors[FACTOR_EVIDENCE]),
    ]
    if not factors[FACTOR_PLAN]:
        cmd.append("--no-plan")
    cmd += ["--workers", str(max(1, workers)), "--llm-backend", backend, "--llm-model", model]
    if llm_cli_cmd:
        cmd += ["--llm-cli-cmd", llm_cli_cmd]
    if sim_timeout is not None:
        cmd += ["--sim-timeout", str(sim_timeout)]
    if compile_timeout is not None:
        cmd += ["--compile-timeout", str(compile_timeout)]
    if verbose:
        cmd.append("--verbose")
    cmd.append("--resume")
    cmd += list(extra_args)
    return cmd


def run_arm_process(cmd: "Sequence[str]", log_path: Path, *, verbose: bool = False) -> int:
    """Run one arm, tee-ing the driver's output to ``log_path``. The single subprocess seam.

    Tests monkeypatch this function; nothing else in the module spawns a process.
    """

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {shlex.join(cmd)}\n")
        handle.flush()
        process = subprocess.Popen(  # noqa: S603 - argv list, no shell
            list(cmd),
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            handle.write(line)
            handle.flush()
            if verbose:
                sys.stdout.write(line)
                sys.stdout.flush()
        return process.wait()


#: run_rtllm_v2 exit codes that still leave a usable report behind. 3 means the sweep finished
#: but at least one design scored 0 because the LLM backend errored -- a real measurement of
#: the backend, not of the arm, so the arm is kept and flagged rather than discarded.
DRIVER_OK_CODES = (0, 3)
DRIVER_INTERRUPT = 130


def arm_is_complete(arm_dir: Path, designs: "Sequence[str]") -> bool:
    """True when this arm already has a report covering every requested design."""

    if not (arm_dir / REPORT_JSON).exists():
        return False
    have = {str(row.get("design")) for row in load_arm_table(arm_dir)}
    return set(designs).issubset(have)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def _pct(value: "float | None") -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _frac(successes: "int | None", n: "int | None") -> str:
    """A count that always states its denominator. Every rate cell goes through here."""

    if not n:
        return "n/a (0 designs)"
    return f"{successes}/{n} ({(successes / n) * 100:.1f}%)"


def _signed(value: "float | None", digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}"


def build_report(
    *,
    arms: "Sequence[dict[str, Any]]",
    subset: HardSubset,
    primary_metric: str,
    alpha: float,
    bootstrap_iterations: int,
    seed: int,
    benchmark: "str | None",
    out_dir: "str | None",
    warnings: "Sequence[str]" = (),
    baseline_arm: str = BASELINE_ARM,
) -> "dict[str, Any]":
    """Assemble the ablation report, running the paired statistics across all arms.

    Holm correction is applied across the non-baseline arms that actually produced numbers,
    so the family size is the number of comparisons made, not the number declared.
    """

    by_name = {arm["arm"]: arm for arm in arms}
    base = by_name.get(baseline_arm)
    base_outcomes = (base or {}).get("outcomes", {}).get(primary_metric, {})

    comparisons: "dict[str, PairedComparison]" = {}
    for arm in arms:
        if arm["arm"] == baseline_arm or not arm.get("ran"):
            continue
        arm_outcomes = arm.get("outcomes", {}).get(primary_metric, {})
        if not base_outcomes or not arm_outcomes:
            continue
        comparisons[arm["arm"]] = compare_to_baseline(
            base_outcomes,
            arm_outcomes,
            bootstrap_iterations=bootstrap_iterations,
            alpha=alpha,
            seed=seed,
        )

    names = list(comparisons)
    for name, adjusted in zip(names, holm_adjust([comparisons[n].p_exact for n in names])):
        comparisons[name].p_holm = adjusted

    floor = min_discordant_for_significance(alpha)
    n_primary = len(base_outcomes)
    rows: "list[dict[str, Any]]" = []
    for arm in arms:
        row = {key: value for key, value in arm.items() if key != "outcomes"}
        comparison = comparisons.get(arm["arm"])
        if comparison is not None:
            row["delta"] = comparison.to_dict()
            row["significance"] = significance_verdict(comparison, alpha=alpha, floor=floor)
        elif arm["arm"] == baseline_arm:
            row["delta"] = None
            row["significance"] = "reference arm"
        else:
            row["delta"] = None
            row["significance"] = "not run" if not arm.get("ran") else "no baseline to compare against"
        rows.append(row)

    return {
        "schema": SCHEMA,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "benchmark": benchmark,
        "out_dir": out_dir,
        "baseline_arm": baseline_arm,
        "baseline_factors": dict(BASELINE_FACTORS),
        "primary_metric": primary_metric,
        "subset": subset.to_dict(),
        "statistics": {
            "paired_test": (
                "exact McNemar: a two-sided binomial sign test on the designs where the two "
                "arms disagree. Concordant designs carry no information about the difference."
            ),
            "alpha": alpha,
            "multiple_comparison_correction": "Holm-Bonferroni across the non-baseline arms",
            "comparisons_in_family": len(comparisons),
            "interval": f"Wilson score, z={Z_95:.4f} (95%)",
            "bootstrap": (
                f"paired percentile bootstrap on the delta, {bootstrap_iterations} resamples "
                f"of designs, seed={seed}"
            ),
            "bootstrap_iterations": bootstrap_iterations,
            "seed": seed,
            "min_discordant_for_significance": floor,
            "designs_in_primary_denominator": n_primary,
            "one_design_in_pp": (100.0 / n_primary) if n_primary else None,
            "caveat": (
                f"n={n_primary} designs. One design is "
                f"{(100.0 / n_primary):.1f} percentage points"
                if n_primary
                else "no designs scored"
            )
            + (
                f"; no arm can reach significance at alpha={alpha:g} with fewer than {floor} "
                "discordant designs, however large the raw percentage gap looks."
                if floor
                else "."
            ),
        },
        "tracks": {
            SELF_DERIVED: [r["arm"] for r in rows if r.get("track") == SELF_DERIVED],
            ORACLE_DERIVED: [r["arm"] for r in rows if r.get("track") == ORACLE_DERIVED],
            UNCLASSIFIED: [r["arm"] for r in rows if r.get("track") == UNCLASSIFIED],
        },
        "arms": rows,
        "warnings": list(warnings),
    }


def _arm_table_row(row: "dict[str, Any]", *, baseline_track: str) -> str:
    name = row["arm"]
    if not row.get("ran"):
        reason = row.get("status_detail") or row.get("status", "not run")
        # 11 columns: arm, track, "not run", 7 blanks, reason.
        return f"| `{name}` | {row.get('track', '-')} | not run |" + " |" * 7 + f" {reason} |"
    n = row.get("designs_run") or 0
    delta = row.get("delta") or {}
    cross = " &Dagger;" if row.get("track") != baseline_track and name != BASELINE_ARM else ""
    if not n:
        return f"| `{name}` | {row.get('track', '-')} | 0 designs scored |" + " |" * 8
    if name == BASELINE_ARM:
        delta_designs = "reference"
        delta_pp = "reference"
        ci = "reference"
    else:
        delta_designs = f"{delta.get('delta_designs'):+d} of {delta.get('n_paired')}" if delta else "n/a"
        delta_pp = _signed(delta.get("delta_pp")) + " pp" if delta else "n/a"
        interval = delta.get("bootstrap_delta_ci_pp")
        ci = f"[{interval[0]:+.1f}, {interval[1]:+.1f}] pp" if interval else "n/a"
    wilson = row.get("func_wilson_pp")
    wilson_text = f"[{wilson[0]:.1f}, {wilson[1]:.1f}]%" if wilson else "n/a"
    rounds = row.get("mean_repair_rounds")
    wall = row.get("mean_wall_s")
    return (
        f"| `{name}`{cross} | {row.get('track', '-')} | {_frac(row.get('syntax_pass'), n)} | "
        f"{_frac(row.get('func_pass'), n)} {wilson_text} | {_frac(row.get('round0_pass'), n)} | "
        f"{'n/a' if rounds is None else f'{rounds:.2f} (n={n})'} | "
        f"{'n/a' if wall is None else f'{wall:.0f}s (n={n})'} | "
        f"{delta_designs} | {delta_pp} | {ci} | {row.get('significance', '')} |"
    )


def render_markdown(report: "dict[str, Any]") -> str:
    """The comparison table, written so that no cell can be quoted without its denominator."""

    stats = report["statistics"]
    subset = report["subset"]
    arms = report["arms"]
    by_name = {arm["arm"]: arm for arm in arms}
    baseline_track = by_name.get(report["baseline_arm"], {}).get("track", ORACLE_DERIVED)
    floor = stats.get("min_discordant_for_significance")
    n_primary = stats.get("designs_in_primary_denominator") or 0

    lines: "list[str]" = []
    lines.append("# RTLLM v2.0 agent-loop ablation")
    lines.append("")
    lines.append(f"Generated {report['generated_at']} &middot; benchmark `{report.get('benchmark')}`")
    lines.append("")
    lines.append("## Read this before reading the table")
    lines.append("")
    one_design_pp = stats.get("one_design_in_pp")
    one_design_text = "n/a" if one_design_pp is None else f"{one_design_pp:.1f}"
    lines.append(
        f"**n = {n_primary} designs.** One design is {one_design_text} percentage points. A one- "
        "or two-design difference between two arms is what this experiment produces when "
        "nothing is happening."
    )
    lines.append("")
    if floor:
        lines.append(
            f"Differences are tested with an **exact McNemar test** on the paired per-design "
            f"outcomes and **Holm-corrected** across the {stats.get('comparisons_in_family', 0)} "
            f"non-baseline comparison(s). At alpha={stats['alpha']:g}, **no arm can reach "
            f"significance with fewer than {floor} discordant designs** -- an arm that flips "
            f"{floor - 1} designs one way and none the other is still not significant. Read the "
            "`significance` column, not the delta column."
        )
        lines.append("")
    lines.append(
        "Every rate cell states its own denominator. `func` carries a Wilson 95% interval; the "
        "delta column carries a paired bootstrap 95% interval. Both are wide on purpose."
    )
    lines.append("")
    lines.append("### Two evidence tracks")
    lines.append("")
    lines.append(
        f"Arms whose repair agent can see output produced by the benchmark's own testbench are "
        f"**{ORACLE_DERIVED}**: their numbers are an upper bound and are **not comparable to "
        f"published single-shot pass@1**. Arms that never see the oracle are **{SELF_DERIVED}** "
        "and are the honest headline. The baseline arm is "
        f"**{baseline_track}**, so a delta between it and a {SELF_DERIVED} arm crosses tracks; "
        "those rows are marked &Dagger; and measure what oracle feedback is worth, nothing else."
    )
    lines.append("")

    header = (
        "| arm | track | syntax | func (Wilson 95%) | round-0 func | repair rounds | "
        "mean wall | &Delta; designs | &Delta; pp | &Delta; 95% CI | significance |"
    )
    divider = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"

    self_rows = [a for a in arms if a.get("track") == SELF_DERIVED]
    oracle_rows = [a for a in arms if a.get("track") == ORACLE_DERIVED]
    other_rows = [a for a in arms if a.get("track") not in (SELF_DERIVED, ORACLE_DERIVED)]

    lines.append("## Comparison")
    lines.append("")
    lines.append(header)
    lines.append(divider)
    ordered = (
        [a for a in arms if a["arm"] == report["baseline_arm"]]
        + [a for a in self_rows if a["arm"] != report["baseline_arm"]]
    )
    for arm in ordered:
        lines.append(_arm_table_row(arm, baseline_track=baseline_track))
    remaining = [a for a in oracle_rows if a["arm"] != report["baseline_arm"]]
    if remaining:
        lines.append(
            "| **&mdash; oracle-derived track below: upper bound, NOT comparable to published "
            "numbers &mdash;** | | | | | | | | | | |"
        )
        for arm in remaining:
            lines.append(_arm_table_row(arm, baseline_track=baseline_track))
    if other_rows:
        lines.append("| **&mdash; unclassified evidence policy: do not quote &mdash;** | | | | | | | | | | |")
        for arm in other_rows:
            lines.append(_arm_table_row(arm, baseline_track=baseline_track))
    lines.append("")
    lines.append(
        "&Dagger; cross-track comparison: the baseline sees oracle-derived feedback and this arm "
        "does not, so the delta is the value of that feedback, not of the arm's own factor alone."
    )
    lines.append("")

    lines.append("## What each arm changed")
    lines.append("")
    lines.append("| arm | factor changed | value | rationale |")
    lines.append("| --- | --- | --- | --- |")
    for arm in arms:
        factor = arm.get("factor_changed") or "(none -- baseline)"
        value = arm.get("factor_value")
        value_text = "-" if arm["arm"] == report["baseline_arm"] else f"`{value}`"
        lines.append(f"| `{arm['arm']}` | `{factor}` | {value_text} | {arm.get('rationale', '')} |")
    lines.append("")
    lines.append(
        "Baseline configuration: "
        + ", ".join(f"`{k}={v}`" for k, v in report["baseline_factors"].items())
        + ". Every other arm differs from it in exactly one of those factors."
    )
    lines.append("")

    lines.append("## Subset")
    lines.append("")
    lines.append(f"**Basis.** {subset['basis']}")
    lines.append("")
    if subset.get("source"):
        lines.append(f"Source run: `{subset['source']}`")
        lines.append("")
    lines.append(
        f"**{subset['selected_count']} design(s) selected**: "
        + (", ".join(f"`{n}`" for n in subset["selected"]) or "(none)")
    )
    lines.append("")
    if subset.get("excluded_unpassable_oracle"):
        lines.append(
            "**Excluded, oracle unpassable** (`rtllm_bench.KNOWN_ORACLE_ISSUES` -- the benchmark's "
            "own verified RTL fails these, so no arm could score): "
            + ", ".join(f"`{n}`" for n in subset["excluded_unpassable_oracle"])
        )
        lines.append("")
    if subset.get("excluded_vacuous_oracle"):
        lines.append(
            "**Excluded, oracle vacuous** (`rtllm_bench.VACUOUS_ORACLE_DESIGNS` -- a module with "
            "no logic at all passes these, so a pass measures nothing): "
            + ", ".join(f"`{n}`" for n in subset["excluded_vacuous_oracle"])
        )
        lines.append("")
    if subset.get("excluded_backend_failed"):
        lines.append(
            "**Excluded, backend errored in the source run** (the design never got a fair "
            "attempt there): " + ", ".join(f"`{n}`" for n in subset["excluded_backend_failed"])
        )
        lines.append("")
    if subset.get("backend_failed_still_included"):
        lines.append(
            "**Warning.** These selected designs failed in the source run because the LLM "
            "backend errored, not because the RTL was wrong: "
            + ", ".join(f"`{n}`" for n in subset["backend_failed_still_included"])
            + ". They are in the subset by chance rather than by difficulty. "
            "Re-select with `--exclude-backend-failed` to drop them."
        )
        lines.append("")
    lines.append(
        "Full catalogues, for audit -- `rtllm_bench.KNOWN_ORACLE_ISSUES` (unpassable) = "
        + ", ".join(f"`{n}`" for n in subset["unpassable_oracle_catalogue"])
        + "; `rtllm_bench.VACUOUS_ORACLE_DESIGNS` (vacuous) = "
        + ", ".join(f"`{n}`" for n in subset["vacuous_oracle_catalogue"])
        + ". A design in either list cannot demonstrate anything about any arm."
    )
    lines.append("")

    lines.append("## Statistics")
    lines.append("")
    for key in (
        "paired_test",
        "interval",
        "bootstrap",
        "multiple_comparison_correction",
        "min_discordant_for_significance",
        "caveat",
    ):
        if stats.get(key) is not None:
            lines.append(f"- **{key.replace('_', ' ')}**: {stats[key]}")
    lines.append(
        f"- **primary metric**: `{report['primary_metric']}` "
        "(the column the significance test is run on; the other columns are descriptive)"
    )
    lines.append("")
    lines.append("")
    lines.append("### Per-arm discordant designs")
    lines.append("")
    lines.append(
        "The designs that actually differ between an arm and the baseline. These, not the "
        "percentage columns, are the entire evidence for every delta in the table above."
    )
    lines.append("")
    for arm in arms:
        delta = arm.get("delta")
        if not delta:
            continue
        gained = delta.get("arm_passed_baseline_failed") or []
        lost = delta.get("baseline_passed_arm_failed") or []
        holm = delta.get("p_holm")
        holm_text = "n/a" if holm is None else f"{holm:.3f}"
        lines.append(
            f"- `{arm['arm']}` over {delta.get('n_paired')} paired designs: "
            f"arm-only passes {len(gained)} ({', '.join(gained) or 'none'}), "
            f"baseline-only passes {len(lost)} ({', '.join(lost) or 'none'}); "
            f"uncorrected exact p={delta.get('p_exact_mcnemar', float('nan')):.3f}, "
            f"Holm p={holm_text}."
        )
    lines.append("")

    if report.get("warnings"):
        lines.append("## Warnings")
        lines.append("")
        for warning in report["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_ablation.py",
        description=(
            "Ablation matrix over the RTLLM v2.0 agent loop: one arm per loop ingredient, each "
            "differing from a fixed baseline in exactly one factor, scored on a reproducible "
            "subset and reported with paired significance tests."
        ),
        epilog=(
            "Exit codes: 0 the matrix completed, 1 no arm produced results, 2 at least one arm "
            "failed (a report is still written for the arms that ran), 130 interrupted."
        ),
    )
    parser.add_argument("--benchmark", type=Path, default=os.environ.get("RTLLM_ROOT"), help="RTLLM checkout (default: $RTLLM_ROOT)")
    parser.add_argument("--out-dir", type=Path, required=True, help="Ablation out-dir; each arm gets a subdirectory")
    subset = parser.add_argument_group("subset selection")
    subset.add_argument(
        "--hard-subset-from",
        type=Path,
        default=None,
        metavar="RESULTS_JSONL",
        help="Select designs that FAILED AT ROUND 0 in this prior run's results.jsonl",
    )
    subset.add_argument("--designs", nargs="+", action="extend", default=[], metavar="NAME", help="Run exactly these designs, overriding --hard-subset-from")
    subset.add_argument("--full-suite", action="store_true", help="Run every discovered design (the confirmation sweep)")
    subset.add_argument("--include-vacuous", action="store_true", help="Keep designs an empty module passes (default: excluded, and listed in the report)")
    subset.add_argument("--include-unpassable", action="store_true", help="Keep designs nothing can pass (default: excluded, and listed in the report)")
    subset.add_argument("--exclude-backend-failed", action="store_true", help="Also drop designs whose source-run failure was an LLM backend error")
    arms = parser.add_argument_group("arms")
    arms.add_argument("--arms", nargs="+", action="extend", default=[], metavar="NAME", help=f"Run only these arms (default: all of {', '.join(s.name for s in ARM_SPECS)})")
    arms.add_argument("--skip-arms", nargs="+", action="extend", default=[], metavar="NAME", help="Skip these arms")
    arms.add_argument("--force-unsupported", action="store_true", help="Run arms whose --evidence-policy the driver does not accept yet (they will fail)")
    arms.add_argument("--track-override", nargs="+", action="extend", default=[], metavar="POLICY=TRACK", help=f"Reclassify an evidence policy, e.g. logs={SELF_DERIVED}")
    stats = parser.add_argument_group("statistics")
    stats.add_argument("--primary-metric", choices=sorted(METRICS), default="func", help="Metric the significance test runs on (default func)")
    stats.add_argument("--alpha", type=float, default=0.05, help="Significance level (default 0.05)")
    stats.add_argument("--bootstrap", type=int, default=10000, help="Paired bootstrap resamples for the delta CI (default 10000; 0 disables)")
    stats.add_argument("--seed", type=int, default=20250805, help="Bootstrap seed, so the interval is reproducible")
    runner = parser.add_argument_group("execution")
    runner.add_argument("--dry-run", action="store_true", help="Print the planned matrix and each arm's command, run nothing")
    runner.add_argument("--resume", action="store_true", help="Skip arms that already have a report covering the subset")
    runner.add_argument("--workers", type=int, default=1, help="Designs evaluated concurrently within an arm (default 1)")
    runner.add_argument("--llm-backend", default="claude-cli", help="Passed through to run_rtllm_v2.py (default claude-cli)")
    runner.add_argument("--llm-model", default="opus", help="Passed through to run_rtllm_v2.py (default opus)")
    runner.add_argument("--llm-cli-cmd", default=None, help="Passed through to run_rtllm_v2.py")
    runner.add_argument("--sim-timeout", type=int, default=None, help="Passed through to run_rtllm_v2.py")
    runner.add_argument("--compile-timeout", type=int, default=None, help="Passed through to run_rtllm_v2.py")
    runner.add_argument("--verbose", action="store_true", help="Echo each arm's driver output as it runs")
    runner.add_argument("--report-only", action="store_true", help="Rebuild the report from arm directories already on disk, run nothing")
    return parser


def parse_track_overrides(values: "Sequence[str]") -> "dict[str, str]":
    overrides: "dict[str, str]" = {}
    for value in values or ():
        if "=" not in value:
            raise SystemExit(f"--track-override expects POLICY=TRACK, got {value!r}")
        policy, track = value.split("=", 1)
        if track not in (SELF_DERIVED, ORACLE_DERIVED, UNCLASSIFIED):
            raise SystemExit(
                f"--track-override track must be one of {SELF_DERIVED}, {ORACLE_DERIVED}, "
                f"{UNCLASSIFIED}; got {track!r}"
            )
        overrides[policy.strip()] = track
    return overrides


def check_out_dir(out_dir: Path) -> None:
    """Refuse an out-dir that overlaps a protected run directory, in either direction."""

    resolved = out_dir.resolve()
    for relative in PROTECTED_DIRS:
        protected = (REPO_ROOT / relative).resolve()
        if resolved == protected or protected in resolved.parents or resolved in protected.parents:
            raise SystemExit(
                f"--out-dir {out_dir} overlaps the protected directory {protected}. That "
                "directory holds a run this ablation reads from; writing an arm into it would "
                "corrupt the source of the subset. Use a separate out-dir under runs/."
            )


def resolve_subset(args: argparse.Namespace) -> HardSubset:
    """Turn the subset flags into the design list, recording how it was derived."""

    if args.designs:
        return explicit_subset(
            args.designs,
            exclude_vacuous=not args.include_vacuous,
            exclude_unpassable=not args.include_unpassable,
        )
    if args.full_suite:
        if not args.benchmark:
            raise SystemExit("--full-suite needs --benchmark (or $RTLLM_ROOT) to discover designs")
        names = [d.name for d in rtllm_bench.discover_designs(Path(args.benchmark))]
        if not names:
            raise SystemExit(f"--full-suite discovered no designs under {args.benchmark}")
        return full_suite_subset(
            names,
            exclude_vacuous=not args.include_vacuous,
            exclude_unpassable=not args.include_unpassable,
        )
    if args.hard_subset_from:
        path = Path(args.hard_subset_from)
        if not path.exists():
            raise SystemExit(f"--hard-subset-from: no such file: {path}")
        rows = driver.load_prior_rows(path)
        if not rows:
            raise SystemExit(f"--hard-subset-from: {path} holds no usable rows")
        subset = select_hard_subset(
            rows,
            source=str(path),
            exclude_vacuous=not args.include_vacuous,
            exclude_unpassable=not args.include_unpassable,
            exclude_backend_failed=args.exclude_backend_failed,
        )
        if not subset.selected:
            raise SystemExit(
                f"--hard-subset-from: every design in {path} passed at round 0 (or was excluded "
                "as vacuous/unpassable), so there is nothing informative to ablate on. Use "
                "--designs or --full-suite."
            )
        return subset
    raise SystemExit(
        "choose a subset: --hard-subset-from RESULTS_JSONL (the reproducible hard subset), "
        "--designs NAME... , or --full-suite"
    )


def plan_arms(
    specs: "Sequence[ArmSpec]",
    *,
    out_dir: Path,
    subset: HardSubset,
    args: argparse.Namespace,
    supported_policies: "Sequence[str]",
    track_overrides: "dict[str, str]",
) -> "list[dict[str, Any]]":
    """Static plan for every arm: factors, track, out-dir, command, and whether it can run."""

    plan: "list[dict[str, Any]]" = []
    for spec in specs:
        factors = spec.factors()
        policy = str(factors[FACTOR_EVIDENCE])
        track, reason = classify_track(policy, track_overrides)
        directory = arm_out_dir(out_dir, spec)
        command = arm_command(
            spec,
            benchmark=Path(args.benchmark) if args.benchmark else Path("."),
            out_dir=directory,
            designs=subset.selected,
            workers=args.workers,
            backend=args.llm_backend,
            model=args.llm_model,
            llm_cli_cmd=args.llm_cli_cmd,
            sim_timeout=args.sim_timeout,
            compile_timeout=args.compile_timeout,
            verbose=args.verbose,
        )
        supported = (not supported_policies) or policy in supported_policies
        entry = {
            "arm": spec.name,
            "factors": factors,
            "factor_changed": spec.factor,
            "factor_value": spec.override.get(spec.factor) if spec.factor else None,
            "rationale": spec.rationale,
            "track": track,
            "track_reason": reason,
            "out_dir": str(directory),
            "command": command,
            "command_text": shlex.join(command),
            "supported": supported,
            "status": "planned" if supported or args.force_unsupported else "blocked",
            "status_detail": (
                ""
                if supported or args.force_unsupported
                else (
                    f"run_rtllm_v2.py does not accept --evidence-policy {policy!r} yet "
                    f"(it accepts {', '.join(supported_policies)}); the arm is skipped rather "
                    "than run into an argparse error. Re-run once the policy lands, or "
                    "--force-unsupported to try anyway."
                )
            ),
            "ran": False,
        }
        plan.append(entry)
    return plan


def collect_arm_metrics(entry: "dict[str, Any]", *, metrics: "Sequence[str]" = tuple(METRICS)) -> None:
    """Fill one plan entry with the numbers on disk. Mutates ``entry`` in place."""

    directory = Path(entry["out_dir"])
    table = load_arm_table(directory)
    if not table:
        entry["ran"] = False
        entry["designs_run"] = 0
        if not entry.get("status_detail"):
            entry["status_detail"] = "no results on disk for this arm"
        return
    wall = load_arm_wall_seconds(directory)
    entry["ran"] = True
    entry["designs_run"] = len(table)
    entry["outcomes"] = {metric: outcomes(table, metric) for metric in metrics}
    entry["syntax_pass"] = sum(1 for row in table if row.get("syntax_pass"))
    entry["func_pass"] = sum(1 for row in table if row.get("func_pass"))
    entry["round0_pass"] = sum(1 for row in table if row.get("func_pass_round0"))
    entry["strict_pass"] = sum(1 for row in table if row.get("func_pass_strict"))
    entry["backend_failed"] = sorted(str(row["design"]) for row in table if row.get("backend_failed"))
    interval = wilson_interval(entry["func_pass"], entry["designs_run"])
    entry["func_wilson_pp"] = [interval[0] * 100.0, interval[1] * 100.0] if interval else None
    round0_interval = wilson_interval(entry["round0_pass"], entry["designs_run"])
    entry["round0_wilson_pp"] = [round0_interval[0] * 100.0, round0_interval[1] * 100.0] if round0_interval else None
    entry["mean_repair_rounds"] = _mean([float(row.get("repair_rounds_used") or 0) for row in table])
    per_design_wall = [wall[str(row["design"])] for row in table if str(row.get("design")) in wall]
    entry["mean_wall_s"] = _mean(per_design_wall)
    entry["total_wall_s"] = sum(per_design_wall) if per_design_wall else None
    entry["wall_designs_measured"] = len(per_design_wall)


def print_plan(plan: "Sequence[dict[str, Any]]", subset: HardSubset, args: argparse.Namespace) -> None:
    """``--dry-run`` output: what would run, and the exact command for each arm."""

    print("=" * 78)
    print("ABLATION PLAN (dry run -- nothing is executed)")
    print("=" * 78)
    print()
    print(f"benchmark : {args.benchmark}")
    print(f"out-dir   : {args.out_dir}")
    print(f"baseline  : " + ", ".join(f"{k}={v}" for k, v in BASELINE_FACTORS.items()))
    print(f"metric    : {args.primary_metric}  (alpha={args.alpha:g}, seed={args.seed})")
    print()
    print(f"subset ({len(subset.selected)} designs) -- {subset.basis}")
    if subset.source:
        print(f"  source: {subset.source}")
    print("  " + (", ".join(subset.selected) or "(none)"))
    if subset.excluded_unpassable:
        print(f"  excluded, unpassable oracle: {', '.join(subset.excluded_unpassable)}")
    if subset.excluded_vacuous:
        print(f"  excluded, vacuous oracle   : {', '.join(subset.excluded_vacuous)}")
    if subset.excluded_backend_failed:
        print(f"  excluded, backend errored  : {', '.join(subset.excluded_backend_failed)}")
    print()
    floor = min_discordant_for_significance(args.alpha)
    n = len(subset.selected)
    if n and floor:
        print(
            f"power note: n={n}, so one design = {100.0 / n:.1f} pp, and no arm can reach "
            f"alpha={args.alpha:g} with fewer than {floor} discordant designs."
        )
        print()
    print(f"{len(plan)} arm(s):")
    print()
    for entry in plan:
        changed = entry["factor_changed"] or "(none -- baseline)"
        print(f"  {entry['arm']}")
        print(f"    changes  : {changed}" + ("" if entry["factor_changed"] is None else f" = {entry['factor_value']!r}"))
        print(f"    factors  : " + ", ".join(f"{k}={v}" for k, v in entry["factors"].items()))
        print(f"    track    : {entry['track']} ({entry['track_reason']})")
        print(f"    out-dir  : {entry['out_dir']}")
        print(f"    status   : {entry['status']}" + (f" -- {entry['status_detail']}" if entry["status_detail"] else ""))
        print(f"    command  : {entry['command_text']}")
        print()


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    validate_arms(ARM_SPECS)
    specs = select_arms(args.arms, args.skip_arms)
    track_overrides = parse_track_overrides(args.track_override)
    out_dir = Path(args.out_dir)
    check_out_dir(out_dir)
    subset = resolve_subset(args)
    supported = driver_evidence_choices()

    plan = plan_arms(
        specs,
        out_dir=out_dir,
        subset=subset,
        args=args,
        supported_policies=supported,
        track_overrides=track_overrides,
    )

    if args.dry_run:
        print_plan(plan, subset, args)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / PLAN_JSON).write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "baseline_factors": BASELINE_FACTORS,
                "subset": subset.to_dict(),
                "arms": [{k: v for k, v in e.items() if k != "command"} for e in plan],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    warnings: "list[str]" = []
    failures = 0
    interrupted = False
    for entry in plan:
        name = entry["arm"]
        directory = Path(entry["out_dir"])
        if entry["status"] == "blocked":
            warnings.append(f"arm `{name}` was not run: {entry['status_detail']}")
            print(f"[{name}] BLOCKED -- {entry['status_detail']}")
            continue
        if args.report_only:
            entry["status"] = "report-only"
            continue
        if args.resume and arm_is_complete(directory, subset.selected):
            entry["status"] = "reused"
            entry["status_detail"] = "already complete on disk; --resume skipped the rerun"
            print(f"[{name}] reused (complete on disk)")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "arm.log"
        print(f"[{name}] {entry['command_text']}")
        started = time.time()
        try:
            code = run_arm_process(entry["command"], log_path, verbose=args.verbose)
        except KeyboardInterrupt:
            interrupted = True
            entry["status"] = "interrupted"
            warnings.append(f"arm `{name}` was interrupted; its partial results are still on disk")
            print(f"[{name}] interrupted")
            break
        entry["exit_code"] = code
        entry["runner_wall_s"] = round(time.time() - started, 1)
        if code in DRIVER_OK_CODES:
            entry["status"] = "ok" if code == 0 else "ok-with-backend-errors"
            if code == 3:
                warnings.append(
                    f"arm `{name}` finished with driver exit 3: at least one design scored 0 "
                    "because the LLM backend errored, which measures the backend and not the arm."
                )
        elif code == DRIVER_INTERRUPT:
            interrupted = True
            entry["status"] = "interrupted"
            warnings.append(f"arm `{name}` was interrupted (driver exit 130)")
            print(f"[{name}] interrupted")
            break
        else:
            failures += 1
            entry["status"] = "failed"
            entry["status_detail"] = f"run_rtllm_v2.py exited {code}; see {log_path}"
            warnings.append(f"arm `{name}` failed: {entry['status_detail']}")
        print(f"[{name}] exit={code} status={entry['status']} ({entry['runner_wall_s']}s)")

    for entry in plan:
        collect_arm_metrics(entry)

    report = build_report(
        arms=plan,
        subset=subset,
        primary_metric=args.primary_metric,
        alpha=args.alpha,
        bootstrap_iterations=max(0, args.bootstrap),
        seed=args.seed,
        benchmark=str(args.benchmark) if args.benchmark else None,
        out_dir=str(out_dir),
        warnings=warnings,
    )
    (out_dir / REPORT_JSON).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / REPORT_MD).write_text(render_markdown(report), encoding="utf-8")
    print()
    print(f"wrote {out_dir / REPORT_JSON}")
    print(f"wrote {out_dir / REPORT_MD}")

    if interrupted:
        return 130
    if not any(entry.get("ran") for entry in plan):
        print("no arm produced results", file=sys.stderr)
        return 1
    return 2 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
