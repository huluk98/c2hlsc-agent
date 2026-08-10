"""Tests for the statistics behind ``docs/outcomes_statistics.md``.

The numbers this module computes are committed into a document and quoted in
conversation, so the estimators are checked against values that can be derived by
hand rather than against the module's own output.
"""

from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "summarize_outcomes", REPO / "scripts" / "summarize_outcomes.py"
)
summarize = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(summarize)


class WilsonTests(unittest.TestCase):
    def test_known_interval(self):
        # 45/50 -> Wilson 95%. Recomputed independently from the closed form:
        #   denom  = 1 + z^2/n                                   = 1.076827
        #   centre = (p + z^2/2n) / denom                        = 0.871461
        #   margin = z*sqrt(p(1-p)/n + z^2/4n^2) / denom         = 0.085063
        low, high = summarize.wilson(45, 50)
        self.assertAlmostEqual(low, 0.786398, places=5)
        self.assertAlmostEqual(high, 0.956524, places=5)

    def test_interval_is_asymmetric_about_the_point_estimate(self):
        # The property that makes Wilson the right choice here: near the boundary the
        # interval is pulled inward, unlike the symmetric normal approximation.
        low, high = summarize.wilson(45, 50)
        self.assertGreater(0.9 - low, high - 0.9)

    def test_interval_brackets_the_point_estimate(self):
        for successes, total in ((1, 13), (10, 13), (4, 50), (43, 43), (0, 29)):
            low, high = summarize.wilson(successes, total)
            self.assertLessEqual(low, successes / total)
            self.assertGreaterEqual(high, successes / total)

    def test_boundaries_stay_inside_zero_and_one(self):
        # The normal approximation escapes [0,1] at the extremes; Wilson must not.
        for successes, total in ((0, 43), (43, 43), (0, 13), (13, 13)):
            low, high = summarize.wilson(successes, total)
            self.assertGreaterEqual(low, 0.0)
            self.assertLessEqual(high, 1.0)

    def test_empty_denominator_does_not_divide_by_zero(self):
        self.assertEqual(summarize.wilson(0, 0), (0.0, 0.0))


class McNemarTests(unittest.TestCase):
    def test_no_discordant_pairs_is_p_one(self):
        self.assertEqual(summarize.mcnemar_exact(0, 0), 1.0)

    def test_all_nine_flips_one_way(self):
        # The ablation's one significant result: 9 discordant, all one direction.
        # Two-sided exact = 2 * 2**-9 = 0.00390625.
        self.assertAlmostEqual(summarize.mcnemar_exact(9, 0), 2 * 2 ** -9, places=10)

    def test_twelve_flips_one_way_matches_the_repair_effect(self):
        self.assertAlmostEqual(summarize.mcnemar_exact(12, 0), 2 * 2 ** -12, places=12)

    def test_symmetric_in_its_arguments(self):
        for b, c in ((3, 7), (0, 5), (9, 1)):
            self.assertEqual(summarize.mcnemar_exact(b, c), summarize.mcnemar_exact(c, b))

    def test_even_split_is_not_significant(self):
        self.assertEqual(summarize.mcnemar_exact(5, 5), 1.0)

    def test_five_clean_flips_cannot_reach_significance(self):
        # The power floor that the ablation reports: 5 one-way flips is p=0.0625,
        # above alpha before any correction at all.
        self.assertAlmostEqual(summarize.mcnemar_exact(5, 0), 0.0625, places=10)
        self.assertGreater(summarize.mcnemar_exact(5, 0), 0.05)

    def test_never_exceeds_one(self):
        for b in range(6):
            for c in range(6):
                self.assertLessEqual(summarize.mcnemar_exact(b, c), 1.0)


class HolmTests(unittest.TestCase):
    def test_smallest_p_is_multiplied_by_family_size(self):
        corrected = summarize.holm({"a": 0.0039, "b": 0.5, "c": 0.5, "d": 1.0,
                                    "e": 1.0, "f": 1.0, "g": 1.0})
        self.assertAlmostEqual(corrected["a"], 7 * 0.0039, places=6)

    def test_reproduces_the_reported_ablation_correction(self):
        # rounds=0 at exact p=0.00390625 in a family of 7 -> 0.0273, the one
        # comparison in the matrix that clears alpha.
        corrected = summarize.holm(
            {"rounds-0": 2 * 2 ** -9, "b": 0.125, "c": 0.5, "d": 1.0, "e": 1.0, "f": 1.0, "g": 1.0}
        )
        self.assertAlmostEqual(corrected["rounds-0"], 0.02734375, places=6)
        self.assertLessEqual(corrected["rounds-0"], 0.05)

    def test_is_monotone_in_the_original_ordering(self):
        raw = {"a": 0.01, "b": 0.02, "c": 0.03, "d": 0.04}
        corrected = summarize.holm(raw)
        ordered = [corrected[k] for k in sorted(raw, key=lambda k: raw[k])]
        self.assertEqual(ordered, sorted(ordered))

    def test_never_reports_below_the_uncorrected_value(self):
        raw = {"a": 0.01, "b": 0.2, "c": 0.9}
        for name, corrected_p in summarize.holm(raw).items():
            self.assertGreaterEqual(corrected_p, raw[name])

    def test_caps_at_one(self):
        for value in summarize.holm({"a": 0.9, "b": 0.95, "c": 0.99}).values():
            self.assertLessEqual(value, 1.0)


class PowerFloorTests(unittest.TestCase):
    def test_thirteen_designs_seven_arms_needs_nine_flips(self):
        self.assertEqual(summarize.min_flips_for_significance(13, 7), 9)

    def test_a_smaller_family_needs_fewer_flips(self):
        self.assertLess(
            summarize.min_flips_for_significance(13, 1),
            summarize.min_flips_for_significance(13, 7),
        )

    def test_returns_none_when_the_subset_is_too_small_to_ever_clear(self):
        # 4 designs cannot reach alpha=0.05 in a family of 7: 2*2**-4 = 0.125.
        self.assertIsNone(summarize.min_flips_for_significance(4, 7))


class PairingTests(unittest.TestCase):
    def _row(self, name: str, ok: bool, round0: bool = False) -> dict:
        return {
            "design": name,
            "func_success": 1 if ok else 0,
            "samples": [{"rounds": [{"sim": {"func_pass": round0}}]}],
        }

    def test_counts_discordant_pairs_in_both_directions(self):
        a = {r["design"]: r for r in (self._row("x", True), self._row("y", False), self._row("z", True))}
        b = {r["design"]: r for r in (self._row("x", False), self._row("y", True), self._row("z", True))}
        n, a_only, b_only, a_score, b_score = summarize.paired(a, b)
        self.assertEqual((n, a_only, b_only, a_score, b_score), (3, 1, 1, 2, 2))

    def test_only_shared_designs_are_compared(self):
        a = {r["design"]: r for r in (self._row("x", True), self._row("only_a", True))}
        b = {r["design"]: r for r in (self._row("x", True), self._row("only_b", True))}
        n, _, _, a_score, b_score = summarize.paired(a, b)
        self.assertEqual((n, a_score, b_score), (1, 1, 1))

    def test_round0_scorer_reads_the_first_round_not_the_final_outcome(self):
        # A design that failed at round 0 and passed after repair must score 0 here.
        repaired = self._row("d", ok=True, round0=False)
        self.assertTrue(summarize.passed(repaired))
        self.assertFalse(summarize.round0_passed(repaired))

    def test_round0_scorer_ignores_later_rounds(self):
        row = {
            "design": "d",
            "func_success": 1,
            "samples": [{"rounds": [
                {"sim": {"func_pass": False}},
                {"sim": {"func_pass": True}},
            ]}],
        }
        self.assertFalse(summarize.round0_passed(row))

    def test_passed_handles_a_boolean_schema(self):
        # CHStone/Rosetta rows carry `ok`/`passed` rather than `func_success`.
        self.assertTrue(summarize.passed({"ok": True}))
        self.assertFalse(summarize.passed({"ok": False}))
        self.assertTrue(summarize.passed({"passed": 3}))

    def test_missing_scoring_key_is_not_a_pass(self):
        self.assertFalse(summarize.passed({"design": "d"}))


if __name__ == "__main__":
    unittest.main()
