import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.cli import build_parser, run_convert, run_repair
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
                    "--auto-repair",
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
                    "--auto-repair",
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

    def test_max_iterations_does_not_auto_repair_by_default(self):
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
                    "3",
                ]
            )
            first = _state(PhaseResult("software_equivalence", "fail", stderr="error: 'size_t' has not been declared"))

            with patch("c2hlsc_agent.cli.verify_project", return_value=first) as verify:
                rc = run_convert(args)

            self.assertEqual(rc, 1)
            self.assertEqual(verify.call_count, 1)
            self.assertNotIn("#include <stddef.h>", (out_dir / "src" / "hls_top.hpp").read_text(encoding="utf-8"))
            report = json.loads((out_dir / "conversion_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["repairs"], [])

    def test_manual_repair_command_uses_external_evidence(self):
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
            convert_args = build_parser().parse_args(
                [
                    "convert",
                    "--input",
                    str(input_path),
                    "--top",
                    "bump",
                    "--out",
                    str(out_dir),
                    "--no-run-vitis",
                ]
            )
            with patch("c2hlsc_agent.cli.verify_project", return_value=_state(PhaseResult("software_equivalence", "fail"))):
                run_convert(convert_args)
            evidence = root / "software.log"
            evidence.write_text("error: 'size_t' has not been declared", encoding="utf-8")
            repair_args = build_parser().parse_args(
                [
                    "repair",
                    "--project",
                    str(out_dir),
                    "--input",
                    str(input_path),
                    "--top",
                    "bump",
                    "--stage",
                    "software_equivalence",
                    "--evidence",
                    str(evidence),
                ]
            )

            with contextlib.redirect_stdout(io.StringIO()):
                rc = run_repair(repair_args)

            self.assertEqual(rc, 0)
            self.assertIn("#include <stddef.h>", (out_dir / "src" / "hls_top.hpp").read_text(encoding="utf-8"))
            self.assertTrue((out_dir / "manual_repair_report.json").exists())
            audit = json.loads((out_dir / "repair_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit[0]["status"], "applied")


if __name__ == "__main__":
    unittest.main()
