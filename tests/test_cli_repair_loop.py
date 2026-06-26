import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.cli import build_parser, run_convert
from c2hlsc_agent.equivalence import PhaseResult, VerificationState


def _state(*phases: PhaseResult) -> VerificationState:
    state = VerificationState()
    for phase in phases:
        state.add_phase(phase)
    return state


class CliRepairLoopTests(unittest.TestCase):
    def test_max_iterations_reruns_from_beginning_after_applied_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.c"
            input_path.write_text(
                """
                #include <stddef.h>
                size_t bump(size_t n) {
                  return n + 1;
                }
                """,
                encoding="utf-8",
            )
            out_dir = root / "out"
            args = build_parser().parse_args(
                [
                    "convert",
                    "--input",
                    str(input_path),
                    "--top",
                    "bump",
                    "--out",
                    str(out_dir),
                    "--no-run-vitis",
                    "--max-iterations",
                    "2",
                ]
            )
            first = _state(PhaseResult("software_equivalence", "fail", stderr="error: 'size_t' has not been declared"))
            second = _state(PhaseResult("software_equivalence", "pass"))

            with patch("c2hlsc_agent.cli.verify_project", side_effect=[first, second]) as verify:
                rc = run_convert(args)

            self.assertEqual(rc, 0)
            self.assertEqual(verify.call_count, 2)
            self.assertIn("#include <stddef.h>", (out_dir / "src" / "hls_top.hpp").read_text(encoding="utf-8"))
            report = json.loads((out_dir / "conversion_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["iterations"], 2)
            self.assertEqual(len(report["repairs"]), 1)
            self.assertEqual(report["repairs"][0]["status"], "applied")
            self.assertEqual(report["repairs"][0]["target_files"], ["src/hls_top.hpp"])
            self.assertEqual(report["repair_audit_file"], "repair_audit.json")

    def test_unmatched_repair_does_not_consume_remaining_iterations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.c"
            input_path.write_text(
                """
                int bump(int n) {
                  return n + 1;
                }
                """,
                encoding="utf-8",
            )
            out_dir = root / "out"
            args = build_parser().parse_args(
                [
                    "convert",
                    "--input",
                    str(input_path),
                    "--top",
                    "bump",
                    "--out",
                    str(out_dir),
                    "--no-run-vitis",
                    "--max-iterations",
                    "3",
                ]
            )
            first = _state(PhaseResult("software_equivalence", "fail", stderr="mysterious failure with no safe repair"))

            with patch("c2hlsc_agent.cli.verify_project", return_value=first) as verify:
                rc = run_convert(args)

            self.assertEqual(rc, 1)
            self.assertEqual(verify.call_count, 1)
            report = json.loads((out_dir / "conversion_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["iterations"], 1)
            self.assertEqual(len(report["repairs"]), 1)
            self.assertEqual(report["repairs"][0]["status"], "no_change")


if __name__ == "__main__":
    unittest.main()
