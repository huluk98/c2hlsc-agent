"""Offline tests for the ``rtllm_v2_results/`` publisher.

Hermetic: run directories are synthesised in a temp dir. No benchmark, no simulator, no LLM.

The point of these tests is that the published directory cannot quietly disagree with the
run it claims to come from. ``rtllm_v2_results/`` is the number people quote, and the whole
reason it is generated rather than hand-written is that a hand-written copy drifts. So the
tests here concentrate on the two ways a publisher can lie: publishing a run that did not
finish, and computing the GPT comparison over a basis that flatters one column.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "make_rtllm_v2_results.py"
spec = importlib.util.spec_from_file_location("make_rtllm_v2_results", SCRIPT_PATH)
assert spec and spec.loader
publisher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = publisher
spec.loader.exec_module(publisher)


def design_row(name: str, *, func=True, round0=True, syntax=True, n=1, successes=None) -> "dict[str, Any]":
    return {
        "design": name,
        "category": "Test/Cat",
        "func_pass": func,
        "func_pass_round0": round0,
        "func_pass_strict": func,
        "syntax_pass": syntax,
        "n_samples": n,
        "func_success": successes if successes is not None else int(func),
        "syntax_success": int(syntax),
    }


def write_run(
    root: Path,
    designs: "list[dict[str, Any]]",
    *,
    completed: "int | None" = None,
    interrupted: bool = False,
    rtl: bool = True,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    totals = {
        "designs": len(designs),
        "designs_func_success": sum(1 for d in designs if d["func_pass"]),
        "designs_func_success_round0": sum(1 for d in designs if d["func_pass_round0"]),
        "designs_syntax_success": sum(1 for d in designs if d["syntax_pass"]),
    }
    report = {
        "designs": designs,
        "totals": totals,
        "selected_designs": len(designs),
        "completed_designs": len(designs) if completed is None else completed,
        "interrupted": interrupted,
        "agent_config": {"plan": True, "evidence_policy": "logs", "max_repair_rounds": 2, "samples": 1},
    }
    (root / "report.json").write_text(json.dumps(report))
    (root / "report.md").write_text("# RTLLM v2.0 report\n\nbody\n")
    (root / "results.jsonl").write_text("".join(json.dumps(d) + "\n" for d in designs))
    if rtl:
        for d in designs:
            sub = root / "designs" / d["design"]
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "rtl.v").write_text(f"module {d['design']}(); endmodule\n")
    return root


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.names = ["a", "b", "c", "d"]

    def _standard_runs(self, run_designs):
        write_run(self.tmp / "run", run_designs)
        write_run(self.tmp / "reference", [design_row(n) for n in self.names])
        write_run(self.tmp / "empty", [design_row(n, func=False, round0=False) for n in self.names])
        # GPT archives cover only a, b, c -- 'd' is out of their basis entirely.
        gpt = [design_row(n, n=5, successes=s) for n, s in zip("abc", (5, 0, 2))]
        write_run(self.tmp / "gpt35", gpt)
        write_run(self.tmp / "gpt4", gpt)

    def _publish(self, extra=()):
        return publisher.main(
            [
                "--run", str(self.tmp / "run"),
                "--reference", str(self.tmp / "reference"),
                "--empty", str(self.tmp / "empty"),
                "--gpt35", str(self.tmp / "gpt35"),
                "--gpt4", str(self.tmp / "gpt4"),
                "--out", str(self.tmp / "out"),
                *extra,
            ]
        )

    def test_publishes_every_artifact_and_one_v_file_per_design(self):
        self._standard_runs([design_row(n) for n in self.names])
        self.assertEqual(self._publish(), 0)
        out = self.tmp / "out"
        for name in ("report.md", "report.json", "results.jsonl", "comparison.md"):
            self.assertTrue((out / name).is_file(), name)
        self.assertEqual(
            sorted(p.name for p in (out / "designs").iterdir()),
            ["a.v", "b.v", "c.v", "d.v"],
        )
        # report.md carries the run it came from, so a quoted number is traceable.
        self.assertIn("GENERATED FILE", (out / "report.md").read_text())
        self.assertIn("do not edit by hand", (out / "report.md").read_text())

    def test_refuses_to_publish_an_unfinished_run(self):
        self._standard_runs([design_row(n) for n in self.names])
        # A run that stopped early must never be published: its rates are computed over a
        # denominator that looks complete.
        write_run(self.tmp / "run", [design_row(n) for n in self.names], completed=2)
        with self.assertRaises(SystemExit) as caught:
            self._publish()
        self.assertIn("completed 2 of 4", str(caught.exception))

    def test_refuses_to_publish_an_interrupted_run(self):
        self._standard_runs([design_row(n) for n in self.names])
        write_run(self.tmp / "run", [design_row(n) for n in self.names], interrupted=True)
        with self.assertRaises(SystemExit) as caught:
            self._publish()
        self.assertIn("interrupted", str(caught.exception))

    def test_comparison_uses_the_gpt_basis_not_the_full_run(self):
        # 'd' passes for the agent and is absent from both archives. Counting it would
        # inflate the agent's column against a denominator the archives never saw.
        self._standard_runs(
            [design_row("a"), design_row("b"), design_row("c", func=False, round0=False), design_row("d")]
        )
        self.assertEqual(self._publish(), 0)
        text = (self.tmp / "out" / "comparison.md").read_text()
        self.assertIn("**3 designs**", text)
        self.assertNotIn("/4 (", text)  # nothing scored on the 4-design basis

    def test_comparison_states_the_fairness_caveats_before_the_table(self):
        self._standard_runs([design_row(n) for n in self.names])
        self._publish()
        text = (self.tmp / "out" / "comparison.md").read_text()
        caveats, table = text.index("caveats before quoting"), text.index("## The table")
        self.assertLess(caveats, table, "caveats must precede the table, not follow it")
        for phrase in ("single-shot", "five samples", "round-0"):
            self.assertIn(phrase, text.lower().replace("**", ""))

    def test_pass_at_1_and_pass_at_5_are_computed_from_sample_counts(self):
        self._standard_runs([design_row(n) for n in self.names])
        self._publish()
        text = (self.tmp / "out" / "comparison.md").read_text()
        # successes 5,0,2 of 5 over three designs -> pass@1 = (1.0+0.0+0.4)/3 = 0.467,
        # pass@5 = 2 of 3 designs had at least one success.
        self.assertIn("0.467", text)
        self.assertIn("2/3 (66.7%) pass@5", text)

    def test_mismatched_gpt_bases_are_fatal(self):
        self._standard_runs([design_row(n) for n in self.names])
        write_run(self.tmp / "gpt4", [design_row(n, n=5, successes=1) for n in "ab"])
        with self.assertRaises(SystemExit) as caught:
            self._publish()
        self.assertIn("different design sets", str(caught.exception))

    def test_run_missing_a_design_the_basis_needs_is_fatal(self):
        self._standard_runs([design_row(n) for n in ("a", "b")])
        with self.assertRaises(SystemExit) as caught:
            self._publish()
        self.assertIn("missing designs", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
