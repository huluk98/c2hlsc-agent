"""Offline tests for the ablation matrix runner.

Hermetic: ``run_ablation.run_arm_process`` -- the module's only subprocess seam -- is replaced
by a fake that writes the arm's report the way ``run_rtllm_v2.py`` would. No ``claude`` CLI,
no ``iverilog``, no benchmark checkout, no network.

The tests that matter most are the statistical ones. An ablation table that overstates
significance is worse than no table, so there are explicit tests that a small delta is
reported as ``NOT SIGNIFICANT`` and that no direction word ("better", "helps", ...) reaches
the rendered markdown for an arm that did not clear the corrected alpha.
"""

from __future__ import annotations

import importlib.util
import io
import json
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence
from unittest import mock

from c2hlsc_agent import rtllm_bench


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "run_ablation.py"
sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location("run_ablation", SCRIPT_PATH)
assert spec and spec.loader
ablation = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ablation
spec.loader.exec_module(ablation)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def result_row(
    design: str,
    *,
    round0_pass: bool,
    func_pass: "bool | None" = None,
    syntax_pass: bool = True,
    llm_error: "str | None" = None,
    wall_s: float = 12.0,
    repair_rounds: int = 0,
) -> "dict[str, Any]":
    """One ``results.jsonl`` row shaped the way ``run_rtllm_v2.py`` writes it."""

    if func_pass is None:
        func_pass = round0_pass
    sample = {
        "design": design,
        "sample": 0,
        "syntax_pass": syntax_pass,
        "func_pass": func_pass,
        "func_pass_strict": func_pass,
        "func_pass_round": 0 if round0_pass else (1 if func_pass else None),
        "syntax_pass_round": 0 if syntax_pass else None,
        "repair_rounds": repair_rounds,
        "llm_error": llm_error,
        "rounds": [
            {"round": index, "role": "rtl_generator", "sim": {"duration_s": 0.1}}
            for index in range(repair_rounds + 1)
        ],
    }
    return {
        "design": design,
        "category": "Arithmetic/Adder",
        "mode": "llm",
        "n_samples": 1,
        "syntax_success": 1 if syntax_pass else 0,
        "func_success": 1 if func_pass else 0,
        "samples": [sample],
        "wall_s": wall_s,
    }


def write_results(path: Path, rows: "Sequence[dict[str, Any]]") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def write_arm_output(arm_dir: Path, outcomes: "dict[str, bool]", *, round0: "dict[str, bool] | None" = None) -> None:
    """Write the report.json + results.jsonl an arm would leave behind."""

    arm_dir.mkdir(parents=True, exist_ok=True)
    round0 = round0 or {}
    rows = [
        result_row(name, round0_pass=round0.get(name, passed), func_pass=passed)
        for name, passed in sorted(outcomes.items())
    ]
    write_results(arm_dir / ablation.RESULTS_FILE, rows)
    table = [ablation.driver.summarize_row(row) for row in rows]
    (arm_dir / ablation.REPORT_JSON).write_text(
        json.dumps({"designs": table, "mode": "llm"}, indent=1), encoding="utf-8"
    )


class FakeRunner:
    """Stands in for ``run_arm_process``: records the command and writes the arm's report."""

    def __init__(self, outcomes_by_arm: "dict[str, dict[str, bool]] | None" = None, code: int = 0):
        self.calls: "list[list[str]]" = []
        self.outcomes_by_arm = outcomes_by_arm or {}
        self.code = code

    def __call__(self, cmd: "Sequence[str]", log_path: Path, *, verbose: bool = False) -> int:
        cmd = list(cmd)
        self.calls.append(cmd)
        out_dir = Path(cmd[cmd.index("--out-dir") + 1])
        designs = []
        if "--designs" in cmd:
            start = cmd.index("--designs") + 1
            for token in cmd[start:]:
                if token.startswith("--"):
                    break
                designs.append(token)
        outcomes = self.outcomes_by_arm.get(out_dir.name, {name: True for name in designs})
        write_arm_output(out_dir, {name: outcomes.get(name, False) for name in designs})
        return self.code

    @property
    def arms_run(self) -> "list[str]":
        return [Path(cmd[cmd.index("--out-dir") + 1]).name for cmd in self.calls]


def base_argv(out_dir: Path, source: Path, *extra: str) -> "list[str]":
    return [
        "--benchmark",
        "/nonexistent/rtllm",
        "--out-dir",
        str(out_dir),
        "--hard-subset-from",
        str(source),
        "--bootstrap",
        "200",
        *extra,
    ]


# --------------------------------------------------------------------------- #
# hard-subset selection
# --------------------------------------------------------------------------- #


class HardSubsetTests(unittest.TestCase):
    def test_selects_only_designs_that_failed_at_round_zero(self):
        rows = [
            result_row("passes_clean", round0_pass=True),
            result_row("passes_after_repair", round0_pass=False, func_pass=True),
            result_row("never_passes", round0_pass=False, func_pass=False),
        ]
        subset = ablation.select_hard_subset(rows, source="fixture")

        # The rule is round 0, not final outcome: a design the loop rescued is exactly the
        # kind of design an ablation needs, and must not be filtered out as "passing".
        self.assertEqual(subset.selected, ["never_passes", "passes_after_repair"])
        self.assertEqual(subset.passed_round0, ["passes_clean"])
        self.assertEqual(subset.source, "fixture")
        self.assertIn("ROUND 0", subset.basis)

    def test_excludes_vacuous_and_unpassable_oracles_by_default(self):
        vacuous = sorted(rtllm_bench.VACUOUS_ORACLE_DESIGNS)[0]
        unpassable = sorted(rtllm_bench.KNOWN_ORACLE_ISSUES)[0]
        rows = [
            result_row("real_hard_design", round0_pass=False),
            result_row(vacuous, round0_pass=False),
            result_row(unpassable, round0_pass=False),
        ]
        subset = ablation.select_hard_subset(rows)

        self.assertEqual(subset.selected, ["real_hard_design"])
        self.assertEqual(subset.excluded_vacuous, [vacuous])
        self.assertEqual(subset.excluded_unpassable, [unpassable])
        # Both must survive into the report, or the exclusion is unauditable.
        payload = subset.to_dict()
        self.assertEqual(payload["excluded_vacuous_oracle"], [vacuous])
        self.assertEqual(payload["excluded_unpassable_oracle"], [unpassable])
        self.assertEqual(payload["vacuous_oracle_catalogue"], sorted(rtllm_bench.VACUOUS_ORACLE_DESIGNS))
        self.assertEqual(payload["unpassable_oracle_catalogue"], sorted(rtllm_bench.KNOWN_ORACLE_ISSUES))

    def test_oracle_exclusions_can_be_kept_explicitly(self):
        vacuous = sorted(rtllm_bench.VACUOUS_ORACLE_DESIGNS)[0]
        unpassable = sorted(rtllm_bench.KNOWN_ORACLE_ISSUES)[0]
        rows = [result_row(vacuous, round0_pass=False), result_row(unpassable, round0_pass=False)]

        subset = ablation.select_hard_subset(rows, exclude_vacuous=False, exclude_unpassable=False)

        self.assertEqual(subset.selected, sorted([vacuous, unpassable]))

    def test_backend_failures_are_flagged_and_optionally_dropped(self):
        rows = [
            result_row("real_hard_design", round0_pass=False),
            result_row("backend_died", round0_pass=False, func_pass=False, llm_error="502 from backend"),
        ]

        kept = ablation.select_hard_subset(rows)
        self.assertIn("backend_died", kept.selected)
        # Included, but never silently: the report has to warn that this design's failure
        # measured the backend rather than the model.
        self.assertEqual(kept.backend_failed_included, ["backend_died"])

        dropped = ablation.select_hard_subset(rows, exclude_backend_failed=True)
        self.assertEqual(dropped.selected, ["real_hard_design"])
        self.assertEqual(dropped.excluded_backend_failed, ["backend_died"])

    def test_selection_is_deterministic_and_sorted(self):
        rows = [result_row(name, round0_pass=False) for name in ("zeta", "alpha", "mu")]
        self.assertEqual(ablation.select_hard_subset(rows).selected, ["alpha", "mu", "zeta"])

    def test_full_suite_and_explicit_subsets_record_their_basis(self):
        vacuous = sorted(rtllm_bench.VACUOUS_ORACLE_DESIGNS)[0]
        full = ablation.full_suite_subset(["b", "a", vacuous])
        self.assertEqual(full.selected, ["a", "b"])
        self.assertEqual(full.excluded_vacuous, [vacuous])
        self.assertIn("--full-suite", full.basis)

        explicit = ablation.explicit_subset(["b", "a", "b"])
        self.assertEqual(explicit.selected, ["b", "a"])  # order preserved, deduped
        self.assertIn("--designs", explicit.basis)

    def test_resolve_subset_reads_a_real_results_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write_results(
                Path(tmp) / "results.jsonl",
                [result_row("hard_one", round0_pass=False), result_row("easy_one", round0_pass=True)],
            )
            args = ablation.build_parser().parse_args(base_argv(Path(tmp) / "out", source))
            subset = ablation.resolve_subset(args)
        self.assertEqual(subset.selected, ["hard_one"])

    def test_resolve_subset_refuses_an_empty_hard_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write_results(Path(tmp) / "results.jsonl", [result_row("easy", round0_pass=True)])
            args = ablation.build_parser().parse_args(base_argv(Path(tmp) / "out", source))
            with self.assertRaises(SystemExit) as caught:
                ablation.resolve_subset(args)
        self.assertIn("nothing informative to ablate", str(caught.exception))

    def test_designs_flag_overrides_the_hard_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write_results(Path(tmp) / "results.jsonl", [result_row("hard_one", round0_pass=False)])
            args = ablation.build_parser().parse_args(
                base_argv(Path(tmp) / "out", source, "--designs", "chosen_a", "chosen_b")
            )
            subset = ablation.resolve_subset(args)
        self.assertEqual(subset.selected, ["chosen_a", "chosen_b"])
        self.assertIn("--designs", subset.basis)


# --------------------------------------------------------------------------- #
# arms
# --------------------------------------------------------------------------- #


class ArmConstructionTests(unittest.TestCase):
    def test_every_arm_differs_from_baseline_in_exactly_one_factor(self):
        for spec in ablation.ARM_SPECS:
            with self.subTest(arm=spec.name):
                factors = spec.factors()
                differing = [
                    key for key, value in ablation.BASELINE_FACTORS.items() if factors[key] != value
                ]
                expected = 0 if spec.name == ablation.BASELINE_ARM else 1
                self.assertEqual(
                    len(differing),
                    expected,
                    f"arm {spec.name!r} changes {differing}; an ablation arm must change exactly one",
                )
                self.assertEqual(set(factors), set(ablation.BASELINE_FACTORS))

    def test_the_declared_matrix_validates(self):
        ablation.validate_arms(ablation.ARM_SPECS)
        names = [spec.name for spec in ablation.ARM_SPECS]
        self.assertEqual(
            names,
            [
                "baseline",
                "no-plan",
                "evidence=none",
                "evidence=self",
                "evidence=oracle",
                "rounds=0",
                "rounds=1",
                "rounds=3",
            ],
        )

    def test_validate_rejects_a_two_factor_arm(self):
        bad = (
            ablation.ArmSpec(ablation.BASELINE_ARM, {}, ""),
            ablation.ArmSpec("two-at-once", {"plan": False, "max_repair_rounds": 0}, ""),
        )
        with self.assertRaises(SystemExit) as caught:
            ablation.validate_arms(bad)
        self.assertIn("exactly one", str(caught.exception))

    def test_validate_rejects_an_arm_that_only_restates_the_baseline(self):
        bad = (
            ablation.ArmSpec(ablation.BASELINE_ARM, {}, ""),
            ablation.ArmSpec("noop", {"plan": True}, ""),
        )
        with self.assertRaises(SystemExit) as caught:
            ablation.validate_arms(bad)
        self.assertIn("baseline value", str(caught.exception))

    def test_validate_rejects_an_unknown_factor(self):
        bad = (
            ablation.ArmSpec(ablation.BASELINE_ARM, {}, ""),
            ablation.ArmSpec("bogus", {"temperature": 0.9}, ""),
        )
        with self.assertRaises(SystemExit) as caught:
            ablation.validate_arms(bad)
        self.assertIn("unknown factor", str(caught.exception))

    def test_select_arms_filters_and_rejects_unknown_names(self):
        chosen = ablation.select_arms(["baseline", "rounds=0"], None)
        self.assertEqual([spec.name for spec in chosen], ["baseline", "rounds=0"])

        skipped = ablation.select_arms(None, ["evidence=self", "evidence=oracle"])
        self.assertNotIn("evidence=self", [spec.name for spec in skipped])

        with self.assertRaises(SystemExit) as caught:
            ablation.select_arms(["no-such-arm"], None)
        self.assertIn("unknown arm", str(caught.exception))

    def test_baseline_cannot_be_skipped(self):
        with self.assertRaises(SystemExit) as caught:
            ablation.select_arms(None, ["baseline"])
        self.assertIn("cannot be skipped", str(caught.exception))

    def test_command_encodes_the_arm_and_is_always_resume_safe(self):
        by_name = {spec.name: spec for spec in ablation.ARM_SPECS}
        common = dict(benchmark=Path("/bench"), designs=["alpha", "beta"], python="python3")

        baseline = ablation.arm_command(by_name["baseline"], out_dir=Path("/o/baseline"), **common)
        self.assertIn("--resume", baseline)
        self.assertNotIn("--no-plan", baseline)
        self.assertEqual(baseline[baseline.index("--evidence-policy") + 1], "logs")
        self.assertEqual(baseline[baseline.index("--max-repair-rounds") + 1], "2")
        self.assertEqual(baseline[baseline.index("--samples") + 1], "1")
        self.assertEqual(baseline[baseline.index("--designs") + 1 : baseline.index("--designs") + 3], ["alpha", "beta"])

        no_plan = ablation.arm_command(by_name["no-plan"], out_dir=Path("/o/no-plan"), **common)
        self.assertIn("--no-plan", no_plan)

        rounds3 = ablation.arm_command(by_name["rounds=3"], out_dir=Path("/o/r3"), **common)
        self.assertEqual(rounds3[rounds3.index("--max-repair-rounds") + 1], "3")

        oracle = ablation.arm_command(by_name["evidence=oracle"], out_dir=Path("/o/eo"), **common)
        self.assertEqual(oracle[oracle.index("--evidence-policy") + 1], "oracle")

        # One flag apart, everything else identical: that is what makes the delta attributable
        # to the planner rather than to some other difference between the two invocations.
        normalised_baseline = [t for t in baseline if t != "/o/baseline"]
        normalised_no_plan = [t for t in no_plan if t not in ("/o/no-plan", "--no-plan")]
        self.assertEqual(normalised_baseline, normalised_no_plan)

    def test_arms_have_private_out_dirs(self):
        root = Path("/o")
        dirs = [str(ablation.arm_out_dir(root, spec)) for spec in ablation.ARM_SPECS]
        self.assertEqual(len(dirs), len(set(dirs)))
        self.assertNotIn("=", "".join(Path(d).name for d in dirs))  # slugged, filesystem-safe

    def test_track_classification_separates_oracle_from_self_derived(self):
        self.assertEqual(ablation.classify_track("none")[0], ablation.SELF_DERIVED)
        self.assertEqual(ablation.classify_track("self")[0], ablation.SELF_DERIVED)
        self.assertEqual(ablation.classify_track("logs")[0], ablation.ORACLE_DERIVED)
        self.assertEqual(ablation.classify_track("oracle")[0], ablation.ORACLE_DERIVED)
        # An unknown policy is never quietly filed under a track that would flatter it.
        track, reason = ablation.classify_track("brand-new-policy")
        self.assertEqual(track, ablation.UNCLASSIFIED)
        self.assertIn("must not be quoted", reason)

        forced, reason = ablation.classify_track("logs", {"logs": ablation.SELF_DERIVED})
        self.assertEqual(forced, ablation.SELF_DERIVED)
        self.assertIn("--track-override", reason)

    def test_track_override_parsing_rejects_junk(self):
        self.assertEqual(
            ablation.parse_track_overrides([f"logs={ablation.SELF_DERIVED}"]),
            {"logs": ablation.SELF_DERIVED},
        )
        with self.assertRaises(SystemExit):
            ablation.parse_track_overrides(["logs"])
        with self.assertRaises(SystemExit):
            ablation.parse_track_overrides(["logs=whatever"])

    def test_unsupported_evidence_policy_is_blocked_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write_results(Path(tmp) / "r.jsonl", [result_row("hard", round0_pass=False)])
            args = ablation.build_parser().parse_args(base_argv(Path(tmp) / "out", source))
            plan = ablation.plan_arms(
                list(ablation.ARM_SPECS),
                out_dir=Path(tmp) / "out",
                subset=ablation.select_hard_subset([result_row("hard", round0_pass=False)]),
                args=args,
                supported_policies=("logs", "none"),
                track_overrides={},
            )
        by_name = {entry["arm"]: entry for entry in plan}
        self.assertEqual(by_name["baseline"]["status"], "planned")
        self.assertEqual(by_name["evidence=none"]["status"], "planned")
        self.assertEqual(by_name["evidence=self"]["status"], "blocked")
        self.assertIn("does not accept", by_name["evidence=self"]["status_detail"])
        # And it un-blocks itself once the driver grows the choice.
        with tempfile.TemporaryDirectory() as tmp:
            source = write_results(Path(tmp) / "r.jsonl", [result_row("hard", round0_pass=False)])
            args = ablation.build_parser().parse_args(base_argv(Path(tmp) / "out", source))
            plan = ablation.plan_arms(
                list(ablation.ARM_SPECS),
                out_dir=Path(tmp) / "out",
                subset=ablation.select_hard_subset([result_row("hard", round0_pass=False)]),
                args=args,
                supported_policies=("logs", "none", "self", "oracle"),
                track_overrides={},
            )
        self.assertEqual({e["arm"]: e["status"] for e in plan}["evidence=self"], "planned")


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #


class StatisticsTests(unittest.TestCase):
    def test_wilson_interval_brackets_the_point_estimate(self):
        low, high = ablation.wilson_interval(14, 17)
        self.assertLess(low, 14 / 17)
        self.assertGreater(high, 14 / 17)
        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)

    def test_wilson_interval_stays_inside_zero_one_at_the_extremes(self):
        low, high = ablation.wilson_interval(0, 17)
        self.assertEqual(low, 0.0)
        self.assertLess(high, 0.3)
        low, high = ablation.wilson_interval(17, 17)
        self.assertEqual(high, 1.0)
        self.assertGreater(low, 0.7)
        self.assertIsNone(ablation.wilson_interval(0, 0))

    def test_wilson_interval_is_wide_at_n_seventeen(self):
        # The whole point of printing it: at this n the interval spans tens of points.
        low, high = ablation.wilson_interval(9, 17)
        self.assertGreater(high - low, 0.40)

    def test_mcnemar_exact_matches_hand_computed_values(self):
        self.assertEqual(ablation.mcnemar_exact_p(0, 0), 1.0)
        self.assertAlmostEqual(ablation.mcnemar_exact_p(0, 1), 1.0)
        self.assertAlmostEqual(ablation.mcnemar_exact_p(0, 5), 2 * (0.5**5))  # 0.0625
        self.assertAlmostEqual(ablation.mcnemar_exact_p(0, 6), 2 * (0.5**6))  # 0.03125
        self.assertAlmostEqual(ablation.mcnemar_exact_p(2, 3), 1.0)
        self.assertEqual(ablation.mcnemar_exact_p(3, 0), ablation.mcnemar_exact_p(0, 3))

    def test_five_clean_flips_are_not_significant_but_six_are(self):
        # The guardrail this whole script exists for.
        self.assertGreater(ablation.mcnemar_exact_p(0, 5), 0.05)
        self.assertLess(ablation.mcnemar_exact_p(0, 6), 0.05)

    def test_min_discordant_for_significance_is_six_at_five_percent(self):
        self.assertEqual(ablation.min_discordant_for_significance(0.05), 6)
        self.assertEqual(ablation.min_discordant_for_significance(0.01), 8)

    def test_holm_adjustment_is_monotone_and_order_preserving(self):
        self.assertEqual(ablation.holm_adjust([]), [])
        self.assertEqual(ablation.holm_adjust([0.01, 0.04]), [0.02, 0.04])
        self.assertEqual(ablation.holm_adjust([0.04, 0.01]), [0.04, 0.02])
        adjusted = ablation.holm_adjust([0.001, 0.02, 0.03, 0.5])
        self.assertTrue(all(a >= b for a, b in zip(adjusted, [0.001, 0.02, 0.03, 0.5])))
        self.assertTrue(all(value <= 1.0 for value in adjusted))

    def test_holm_makes_a_borderline_arm_non_significant(self):
        # Uncorrected 0.03 would "pass"; across a family of 7 it does not. This is the
        # difference between an ablation table and a fishing expedition.
        raw = 2 * (0.5**6)  # 0.03125, one arm flipping 6 designs cleanly
        self.assertLess(raw, 0.05)
        self.assertGreater(ablation.holm_adjust([raw] + [1.0] * 6)[0], 0.05)

    def test_paired_bootstrap_is_deterministic_and_brackets_the_delta(self):
        pairs = [(True, True)] * 6 + [(True, False)] * 2 + [(False, True)] * 3 + [(False, False)] * 1
        first = ablation.paired_bootstrap_delta_ci(pairs, iterations=500, seed=7)
        second = ablation.paired_bootstrap_delta_ci(pairs, iterations=500, seed=7)
        self.assertEqual(first, second)
        point = (9 - 8) / 12
        self.assertLessEqual(first[0], point)
        self.assertGreaterEqual(first[1], point)
        # And it straddles zero for a one-design difference, which is the honest answer.
        self.assertLess(first[0], 0.0)
        self.assertGreater(first[1], 0.0)
        self.assertIsNone(ablation.paired_bootstrap_delta_ci([], iterations=10))

    def test_compare_to_baseline_counts_discordant_pairs_by_hand(self):
        baseline = {f"d{i}": i <= 6 for i in range(1, 13)}  # d1..d6 pass
        arm = {f"d{i}": i <= 4 or i in (7, 8, 9) for i in range(1, 13)}  # d1..d4, d7..d9 pass

        comparison = ablation.compare_to_baseline(baseline, arm, bootstrap_iterations=200, seed=3)

        self.assertEqual(comparison.n_paired, 12)
        self.assertEqual(comparison.baseline_passes, 6)
        self.assertEqual(comparison.arm_passes, 7)
        self.assertEqual(comparison.baseline_only, ["d5", "d6"])
        self.assertEqual(comparison.arm_only, ["d7", "d8", "d9"])
        self.assertEqual(comparison.discordant, 5)
        self.assertEqual(comparison.delta_designs, 1)
        self.assertAlmostEqual(comparison.delta_pp, 100 / 12)
        self.assertAlmostEqual(comparison.p_exact, ablation.mcnemar_exact_p(2, 3))

    def test_compare_to_baseline_pairs_only_shared_designs(self):
        baseline = {"a": True, "b": False, "only_in_baseline": True}
        arm = {"a": True, "b": True, "only_in_arm": False}

        comparison = ablation.compare_to_baseline(baseline, arm, bootstrap_iterations=50, seed=1)

        self.assertEqual(comparison.n_paired, 2)
        self.assertEqual(comparison.arm_only, ["b"])
        self.assertEqual(comparison.baseline_only, [])

    def test_verdict_never_claims_an_effect_for_a_small_delta(self):
        baseline = {f"d{i}": i <= 5 for i in range(1, 18)}
        arm = dict(baseline)
        arm["d6"] = True  # exactly one design flips

        comparison = ablation.compare_to_baseline(baseline, arm, bootstrap_iterations=200, seed=1)
        comparison.p_holm = ablation.holm_adjust([comparison.p_exact])[0]
        verdict = ablation.significance_verdict(comparison, alpha=0.05, floor=6)

        self.assertEqual(comparison.delta_designs, 1)
        self.assertIn("NOT SIGNIFICANT", verdict)
        self.assertIn("cannot be", verdict)  # 1 discordant is below the floor of 6
        for word in ("better", "worse", "improves", "helps", "hurts", "above", "below"):
            self.assertNotIn(word, verdict.lower())

    def test_verdict_reports_a_real_effect_when_the_evidence_supports_it(self):
        baseline = {f"d{i}": False for i in range(1, 18)}
        arm = {name: index < 8 for index, name in enumerate(baseline)}  # 8 clean flips

        comparison = ablation.compare_to_baseline(baseline, arm, bootstrap_iterations=200, seed=1)
        comparison.p_holm = comparison.p_exact
        verdict = ablation.significance_verdict(comparison, alpha=0.05, floor=6)

        self.assertEqual(comparison.discordant, 8)
        self.assertLess(comparison.p_exact, 0.05)
        self.assertIn("significant", verdict)
        self.assertNotIn("NOT SIGNIFICANT", verdict)
        self.assertIn("arm above baseline", verdict)

    def test_verdict_for_identical_arms_states_the_denominator(self):
        outcomes = {f"d{i}": i % 2 == 0 for i in range(1, 18)}
        comparison = ablation.compare_to_baseline(outcomes, dict(outcomes), bootstrap_iterations=50, seed=1)
        verdict = ablation.significance_verdict(comparison, alpha=0.05, floor=6)
        self.assertIn("identical outcomes on all 17 designs", verdict)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def build_fixture_report(baseline_pass: "dict[str, bool]", arm_pass: "dict[str, bool]", **kwargs):
    """A two-arm report over hand-built outcomes."""

    def arm_entry(name: str, outcomes: "dict[str, bool]", track: str):
        n = len(outcomes)
        passes = sum(1 for value in outcomes.values() if value)
        interval = ablation.wilson_interval(passes, n)
        return {
            "arm": name,
            "track": track,
            "track_reason": "fixture",
            "factors": dict(ablation.BASELINE_FACTORS),
            "factor_changed": None if name == "baseline" else "max_repair_rounds",
            "factor_value": None if name == "baseline" else 0,
            "rationale": "fixture arm",
            "out_dir": f"/o/{name}",
            "command_text": "fixture",
            "status": "ok",
            "status_detail": "",
            "ran": True,
            "designs_run": n,
            "syntax_pass": n,
            "func_pass": passes,
            "round0_pass": passes,
            "strict_pass": passes,
            "mean_repair_rounds": 1.0,
            "mean_wall_s": 100.0,
            "func_wilson_pp": [interval[0] * 100, interval[1] * 100] if interval else None,
            "outcomes": {metric: dict(outcomes) for metric in ablation.METRICS},
        }

    return ablation.build_report(
        arms=[
            arm_entry("baseline", baseline_pass, ablation.ORACLE_DERIVED),
            arm_entry("rounds=0", arm_pass, ablation.ORACLE_DERIVED),
        ],
        subset=ablation.HardSubset(selected=sorted(baseline_pass), basis="fixture"),
        primary_metric="func",
        alpha=kwargs.get("alpha", 0.05),
        bootstrap_iterations=kwargs.get("bootstrap", 200),
        seed=1,
        benchmark="/bench",
        out_dir="/o",
    )


class ReportTests(unittest.TestCase):
    def test_report_carries_the_delta_and_the_uncertainty(self):
        baseline = {f"d{i}": i <= 9 for i in range(1, 18)}
        arm = {f"d{i}": i <= 8 for i in range(1, 18)}

        report = build_fixture_report(baseline, arm)
        row = {entry["arm"]: entry for entry in report["arms"]}["rounds=0"]

        self.assertEqual(row["delta"]["delta_designs"], -1)
        self.assertAlmostEqual(row["delta"]["delta_pp"], -100 / 17)
        self.assertEqual(row["delta"]["baseline_passed_arm_failed"], ["d9"])
        self.assertEqual(row["delta"]["arm_passed_baseline_failed"], [])
        self.assertIsNotNone(row["delta"]["bootstrap_delta_ci_pp"])
        self.assertIsNotNone(row["delta"]["p_holm"])
        self.assertIn("NOT SIGNIFICANT", row["significance"])

    def test_report_states_the_small_n_caveat_numerically(self):
        baseline = {f"d{i}": i <= 9 for i in range(1, 18)}
        report = build_fixture_report(baseline, dict(baseline))
        stats = report["statistics"]

        self.assertEqual(stats["designs_in_primary_denominator"], 17)
        self.assertAlmostEqual(stats["one_design_in_pp"], 100 / 17)
        self.assertEqual(stats["min_discordant_for_significance"], 6)
        self.assertIn("percentage points", stats["caveat"])
        self.assertIn("Holm", stats["multiple_comparison_correction"])

    def test_markdown_never_asserts_a_direction_for_a_non_significant_arm(self):
        baseline = {f"d{i}": i <= 9 for i in range(1, 18)}
        arm = {f"d{i}": i <= 10 for i in range(1, 18)}  # one design better

        markdown = ablation.render_markdown(build_fixture_report(baseline, arm))

        self.assertIn("NOT SIGNIFICANT", markdown)
        for phrase in ("arm above baseline", "arm below baseline"):
            self.assertNotIn(phrase, markdown)
        self.assertIn("no arm can reach significance with fewer than 6 discordant designs", markdown)
        self.assertIn("n = 17 designs", markdown)

    def test_markdown_states_a_denominator_in_every_rate_cell(self):
        baseline = {f"d{i}": i <= 9 for i in range(1, 18)}
        markdown = ablation.render_markdown(build_fixture_report(baseline, dict(baseline)))

        # Only the comparison table; the "what each arm changed" table below it has no rates.
        comparison = markdown.split("## Comparison", 1)[1].split("## What each arm changed", 1)[0]
        table_rows = [line for line in comparison.splitlines() if line.startswith("| `")]
        self.assertEqual(len(table_rows), 2)
        for line in table_rows:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            for cell in cells[2:5]:  # syntax, func, round-0 func
                self.assertRegex(cell, r"\d+/\d+ \(\d+\.\d%\)", f"cell without a denominator: {cell!r}")

    def test_markdown_separates_and_labels_the_oracle_derived_track(self):
        baseline = {f"d{i}": i <= 9 for i in range(1, 18)}
        report = build_fixture_report(baseline, dict(baseline))
        report["arms"][1]["track"] = ablation.ORACLE_DERIVED
        report["arms"][0]["track"] = ablation.SELF_DERIVED
        report["tracks"] = {
            ablation.SELF_DERIVED: ["baseline"],
            ablation.ORACLE_DERIVED: ["rounds=0"],
            ablation.UNCLASSIFIED: [],
        }

        markdown = ablation.render_markdown(report)

        self.assertIn("oracle-derived track below", markdown)
        self.assertIn("NOT comparable to published", markdown)
        divider = next(i for i, line in enumerate(markdown.splitlines()) if "oracle-derived track below" in line)
        oracle_row = next(i for i, line in enumerate(markdown.splitlines()) if line.startswith("| `rounds=0`"))
        self.assertGreater(oracle_row, divider, "the oracle arm must sit below the separator")

    def test_markdown_lists_the_excluded_oracle_designs(self):
        subset = ablation.HardSubset(
            selected=["real"],
            basis="fixture",
            excluded_vacuous=["comparator_3bit"],
            excluded_unpassable=["ring_counter"],
        )
        report = ablation.build_report(
            arms=[],
            subset=subset,
            primary_metric="func",
            alpha=0.05,
            bootstrap_iterations=10,
            seed=1,
            benchmark="/bench",
            out_dir="/o",
        )
        markdown = ablation.render_markdown(report)
        self.assertIn("comparator_3bit", markdown)
        self.assertIn("ring_counter", markdown)
        self.assertIn("VACUOUS_ORACLE_DESIGNS", markdown)
        self.assertIn("KNOWN_ORACLE_ISSUES", markdown)

    def test_markdown_renders_an_arm_that_did_not_run(self):
        report = ablation.build_report(
            arms=[
                {
                    "arm": "evidence=self",
                    "track": ablation.SELF_DERIVED,
                    "ran": False,
                    "status": "blocked",
                    "status_detail": "policy not available yet",
                    "factors": dict(ablation.BASELINE_FACTORS),
                    "factor_changed": "evidence_policy",
                    "factor_value": "self",
                    "rationale": "",
                }
            ],
            subset=ablation.HardSubset(selected=["a"], basis="fixture"),
            primary_metric="func",
            alpha=0.05,
            bootstrap_iterations=10,
            seed=1,
            benchmark="/bench",
            out_dir="/o",
        )
        markdown = ablation.render_markdown(report)
        row = next(line for line in markdown.splitlines() if line.startswith("| `evidence=self`"))
        self.assertIn("not run", row)
        self.assertIn("policy not available yet", row)
        self.assertEqual(row.count("|"), 12)  # 11 columns


# --------------------------------------------------------------------------- #
# end to end (fake subprocess)
# --------------------------------------------------------------------------- #


class MatrixRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.source = write_results(
            self.root / "prior" / "results.jsonl",
            [
                result_row("hard_a", round0_pass=False),
                result_row("hard_b", round0_pass=False, func_pass=True),
                result_row("hard_c", round0_pass=False),
                result_row("easy", round0_pass=True),
                result_row(sorted(rtllm_bench.KNOWN_ORACLE_ISSUES)[0], round0_pass=False),
            ],
        )
        self.out = self.root / "ablation"

    def run_main(self, *extra: str, runner: "FakeRunner | None" = None):
        runner = runner or FakeRunner()
        buffer = io.StringIO()
        with mock.patch.object(ablation, "run_arm_process", runner), contextlib.redirect_stdout(buffer):
            code = ablation.main(base_argv(self.out, self.source, *extra))
        return code, runner, buffer.getvalue()

    def test_matrix_runs_one_subprocess_per_supported_arm(self):
        code, runner, _ = self.run_main("--arms", "baseline", "no-plan", "rounds=0")

        self.assertEqual(code, 0)
        self.assertEqual(sorted(runner.arms_run), ["baseline", "no-plan", "rounds-0"])
        for cmd in runner.calls:
            self.assertIn("--resume", cmd)
            self.assertIn(str(SCRIPTS_DIR / "run_rtllm_v2.py"), cmd)
            out_dir = Path(cmd[cmd.index("--out-dir") + 1])
            self.assertEqual(out_dir.parent, self.out)

    def test_subset_flows_into_every_arm_command_and_into_the_report(self):
        _, runner, _ = self.run_main("--arms", "baseline", "rounds=0")

        for cmd in runner.calls:
            start = cmd.index("--designs") + 1
            designs = []
            for token in cmd[start:]:
                if token.startswith("--"):
                    break
                designs.append(token)
            self.assertEqual(designs, ["hard_a", "hard_b", "hard_c"])

        report = json.loads((self.out / ablation.REPORT_JSON).read_text())
        self.assertEqual(report["subset"]["selected"], ["hard_a", "hard_b", "hard_c"])
        self.assertEqual(report["subset"]["excluded_unpassable_oracle"], [sorted(rtllm_bench.KNOWN_ORACLE_ISSUES)[0]])
        self.assertEqual(report["subset"]["passed_round0_not_selected"], ["easy"])

    def test_report_files_and_plan_are_written(self):
        self.run_main("--arms", "baseline", "rounds=0")
        self.assertTrue((self.out / ablation.REPORT_JSON).exists())
        self.assertTrue((self.out / ablation.REPORT_MD).exists())
        self.assertTrue((self.out / ablation.PLAN_JSON).exists())
        plan = json.loads((self.out / ablation.PLAN_JSON).read_text())
        self.assertEqual(plan["baseline_factors"], ablation.BASELINE_FACTORS)

    def test_end_to_end_delta_matches_the_injected_outcomes(self):
        runner = FakeRunner(
            {
                "baseline": {"hard_a": True, "hard_b": True, "hard_c": False},
                "rounds-0": {"hard_a": True, "hard_b": False, "hard_c": False},
            }
        )
        code, _, _ = self.run_main("--arms", "baseline", "rounds=0", runner=runner)

        self.assertEqual(code, 0)
        report = json.loads((self.out / ablation.REPORT_JSON).read_text())
        rows = {entry["arm"]: entry for entry in report["arms"]}
        self.assertEqual(rows["baseline"]["func_pass"], 2)
        self.assertEqual(rows["baseline"]["designs_run"], 3)
        self.assertEqual(rows["rounds=0"]["func_pass"], 1)
        self.assertEqual(rows["rounds=0"]["delta"]["delta_designs"], -1)
        self.assertEqual(rows["rounds=0"]["delta"]["baseline_passed_arm_failed"], ["hard_b"])
        self.assertIn("NOT SIGNIFICANT", rows["rounds=0"]["significance"])

    def test_blocked_arms_are_reported_not_executed(self):
        # Pinned to a driver that lacks the policy, so this tests the blocking mechanism
        # rather than whatever --evidence-policy choices the driver happens to ship today.
        with mock.patch.object(ablation, "driver_evidence_choices", return_value=("logs", "none")):
            code, runner, output = self.run_main("--arms", "baseline", "evidence=self")

        self.assertNotIn("evidence-self", runner.arms_run)
        self.assertIn("BLOCKED", output)
        report = json.loads((self.out / ablation.REPORT_JSON).read_text())
        row = {entry["arm"]: entry for entry in report["arms"]}["evidence=self"]
        self.assertFalse(row["ran"])
        self.assertEqual(row["status"], "blocked")
        self.assertTrue(any("evidence=self" in warning for warning in report["warnings"]))
        self.assertEqual(code, 0)

    def test_a_failing_arm_does_not_abort_the_matrix(self):
        class FlakyRunner(FakeRunner):
            def __call__(self, cmd, log_path, *, verbose=False):
                out_dir = Path(list(cmd)[list(cmd).index("--out-dir") + 1])
                if out_dir.name == "rounds-0":
                    self.calls.append(list(cmd))
                    return 4
                return super().__call__(cmd, log_path, verbose=verbose)

        code, runner, _ = self.run_main("--arms", "baseline", "rounds=0", "no-plan", runner=FlakyRunner())

        self.assertEqual(code, 2)  # "at least one arm failed"
        self.assertEqual(sorted(runner.arms_run), ["baseline", "no-plan", "rounds-0"])
        report = json.loads((self.out / ablation.REPORT_JSON).read_text())
        rows = {entry["arm"]: entry for entry in report["arms"]}
        self.assertEqual(rows["rounds=0"]["status"], "failed")
        self.assertTrue(rows["baseline"]["ran"])
        self.assertTrue(rows["no-plan"]["ran"])

    def test_resume_skips_an_arm_that_is_already_complete(self):
        first_code, first_runner, _ = self.run_main("--arms", "baseline", "rounds=0")
        self.assertEqual(first_code, 0)
        self.assertEqual(len(first_runner.calls), 2)

        _, second_runner, output = self.run_main("--arms", "baseline", "rounds=0", "--resume")

        self.assertEqual(second_runner.calls, [], "a complete arm must not be re-run under --resume")
        self.assertIn("reused", output)
        report = json.loads((self.out / ablation.REPORT_JSON).read_text())
        rows = {entry["arm"]: entry for entry in report["arms"]}
        self.assertEqual(rows["baseline"]["status"], "reused")
        self.assertTrue(rows["baseline"]["ran"])  # numbers still come off disk
        self.assertEqual(rows["baseline"]["designs_run"], 3)

    def test_resume_reruns_an_arm_whose_report_is_short_of_the_subset(self):
        write_arm_output(self.out / "baseline", {"hard_a": True})  # only 1 of 3 designs

        _, runner, _ = self.run_main("--arms", "baseline", "rounds=0", "--resume")

        self.assertIn("baseline", runner.arms_run)
        self.assertIn("rounds-0", runner.arms_run)

    def test_arm_is_complete_checks_coverage_not_mere_existence(self):
        arm_dir = self.out / "arm"
        write_arm_output(arm_dir, {"a": True, "b": False})
        self.assertTrue(ablation.arm_is_complete(arm_dir, ["a", "b"]))
        self.assertTrue(ablation.arm_is_complete(arm_dir, ["a"]))
        self.assertFalse(ablation.arm_is_complete(arm_dir, ["a", "b", "c"]))
        self.assertFalse(ablation.arm_is_complete(self.out / "missing", ["a"]))

    def test_report_only_rebuilds_without_running_anything(self):
        self.run_main("--arms", "baseline", "rounds=0")
        (self.out / ablation.REPORT_MD).unlink()

        _, runner, _ = self.run_main("--arms", "baseline", "rounds=0", "--report-only")

        self.assertEqual(runner.calls, [])
        self.assertTrue((self.out / ablation.REPORT_MD).exists())

    def test_wall_clock_is_averaged_over_the_designs_measured(self):
        self.run_main("--arms", "baseline")
        report = json.loads((self.out / ablation.REPORT_JSON).read_text())
        row = {entry["arm"]: entry for entry in report["arms"]}["baseline"]
        self.assertAlmostEqual(row["mean_wall_s"], 12.0)
        self.assertEqual(row["wall_designs_measured"], 3)


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.source = write_results(
            self.root / "results.jsonl",
            [result_row("hard_a", round0_pass=False), result_row("hard_b", round0_pass=False)],
        )
        self.out = self.root / "ablation"

    def test_dry_run_prints_the_matrix_and_runs_nothing(self):
        def explode(*args, **kwargs):
            raise AssertionError("--dry-run must not start a subprocess")

        buffer = io.StringIO()
        with mock.patch.object(ablation, "run_arm_process", explode), contextlib.redirect_stdout(buffer):
            code = ablation.main(base_argv(self.out, self.source, "--dry-run"))
        output = buffer.getvalue()

        self.assertEqual(code, 0)
        self.assertFalse(self.out.exists(), "--dry-run must not create the out-dir")

        for spec in ablation.ARM_SPECS:
            self.assertIn(spec.name, output)
        self.assertIn("run_rtllm_v2.py", output)
        self.assertIn("--evidence-policy", output)
        self.assertIn("--resume", output)
        self.assertIn("hard_a", output)
        self.assertIn("--out-dir", output)

    def test_dry_run_states_the_power_limit_up_front(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ablation.main(base_argv(self.out, self.source, "--dry-run"))
        output = buffer.getvalue()

        self.assertIn("power note", output)
        self.assertIn("fewer than 6 discordant designs", output)
        self.assertIn("one design = 50.0 pp", output)  # n=2 in this fixture

    def test_dry_run_shows_each_arms_command_verbatim(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ablation.main(base_argv(self.out, self.source, "--arms", "baseline", "no-plan", "--dry-run"))
        output = buffer.getvalue()

        commands = [line.split("command  : ", 1)[1] for line in output.splitlines() if "command  :" in line]
        self.assertEqual(len(commands), 2)
        self.assertNotIn("--no-plan", commands[0])
        self.assertIn("--no-plan", commands[1])
        # One factor apart: the two commands differ only in --no-plan and the out-dir.
        tokens_a = [t for t in commands[0].split() if "baseline" not in t]
        tokens_b = [t for t in commands[1].split() if "no-plan" not in t]
        self.assertEqual(tokens_a, tokens_b)


class SafetyTests(unittest.TestCase):
    def test_out_dir_may_not_overlap_a_protected_run_directory(self):
        for candidate in ("runs/agent", "runs/agent/baseline", "runs"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(SystemExit) as caught:
                    ablation.check_out_dir(Path(candidate))
                self.assertIn("protected", str(caught.exception))

    def test_a_sibling_out_dir_is_allowed(self):
        ablation.check_out_dir(Path("runs/ablation_hard"))

    def test_protected_dirs_covers_the_in_flight_baseline_run(self):
        self.assertIn("runs/agent", ablation.PROTECTED_DIRS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
