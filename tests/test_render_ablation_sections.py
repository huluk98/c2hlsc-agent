"""Offline tests for the ablation-document renderer.

The renderer exists so that no figure in ``docs/loop_ablation.md`` is typed by hand. These
tests pin the two properties that makes it worth having: it is **idempotent** (re-running it
replaces the generated block rather than stacking copies), and it **never touches prose
outside the delimiters**. A generator that quietly duplicated or ate hand-written text would
be worse than transcribing the numbers manually.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_ablation_sections.py"
spec = importlib.util.spec_from_file_location("render_ablation_sections", SCRIPT_PATH)
assert spec and spec.loader
renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = renderer
spec.loader.exec_module(renderer)


DOC = """\
# doc

prose before

## 3. RTLLM results

<!--RTLLM_RESULTS-->

## 4. CHStone results

<!--CHSTONE_RESULTS-->

prose after
"""


class SpliceTests(unittest.TestCase):
    def test_first_render_wraps_the_placeholder_in_delimiters(self):
        out = renderer.splice(DOC, "RTLLM_RESULTS", "BODY")
        self.assertIn("<!--RTLLM_RESULTS:BEGIN-->\nBODY\n<!--RTLLM_RESULTS:END-->", out)
        self.assertNotIn("<!--RTLLM_RESULTS-->", out)

    def test_second_render_replaces_rather_than_stacks(self):
        once = renderer.splice(DOC, "RTLLM_RESULTS", "FIRST")
        twice = renderer.splice(once, "RTLLM_RESULTS", "SECOND")
        self.assertIn("SECOND", twice)
        self.assertNotIn("FIRST", twice)
        self.assertEqual(twice.count("<!--RTLLM_RESULTS:BEGIN-->"), 1)

    def test_prose_outside_the_block_is_untouched(self):
        out = renderer.splice(DOC, "RTLLM_RESULTS", "BODY")
        self.assertIn("prose before", out)
        self.assertIn("prose after", out)
        self.assertIn("<!--CHSTONE_RESULTS-->", out)  # the other placeholder survives

    def test_multiline_body_round_trips(self):
        body = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        once = renderer.splice(DOC, "RTLLM_RESULTS", body)
        self.assertEqual(renderer.splice(once, "RTLLM_RESULTS", body), once)

    def test_missing_placeholder_is_fatal(self):
        with self.assertRaises(SystemExit):
            renderer.splice("# doc with no placeholder\n", "RTLLM_RESULTS", "BODY")


class RenderTests(unittest.TestCase):
    def test_absent_run_renders_a_statement_not_a_fabricated_table(self):
        text = renderer.render_rtllm(None)
        self.assertIn("Not yet measured", text)
        self.assertNotIn("|", text)

    def test_arms_that_did_not_run_are_marked_not_run(self):
        report = {
            "baseline_arm": "baseline",
            "statistics": {"min_discordant_for_significance": 9},
            "arms": [
                {
                    "arm": "baseline",
                    "track": "oracle-derived",
                    "ran": True,
                    "designs_run": 13,
                    "func_pass": 10,
                    "round0_pass": 1,
                },
                {"arm": "rounds=3", "track": "oracle-derived", "ran": False},
            ],
        }
        text = renderer.render_rtllm(report)
        self.assertIn("not run", text)
        self.assertIn("10/13", text)
        # No arm has a delta, so the floor sentence still renders without crashing.
        self.assertIn("floor is **9 discordant", text)

    def test_check_mode_reports_drift_without_writing(self):
        tmp = Path(tempfile.mkdtemp()) / "doc.md"
        tmp.write_text(DOC)
        code = renderer.main(["--doc", str(tmp), "--check"])
        self.assertEqual(code, 1)  # placeholders unfilled -> would change
        self.assertEqual(tmp.read_text(), DOC, "--check must not write")


if __name__ == "__main__":
    unittest.main()
