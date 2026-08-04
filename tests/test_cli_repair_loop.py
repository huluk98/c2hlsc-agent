import hashlib
import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.cli import (
    _config_path,
    _guard_output_dir,
    _invalidate_stale_reports,
    _project_signature,
    _read_relational_klee_evidence,
    build_parser,
    run_convert,
    run_repair,
)
from c2hlsc_agent.equivalence import PhaseResult, VerificationState


def _state(*phases: PhaseResult) -> VerificationState:
    state = VerificationState()
    for phase in phases:
        state.add_phase(phase)
    return state


class CliRepairLoopTests(unittest.TestCase):
    def test_convert_overwrite_flag_is_explicit(self):
        base = ["convert", "--input", "input.c", "--top", "kernel", "--out", "out"]
        self.assertFalse(build_parser().parse_args(base).overwrite)
        self.assertTrue(build_parser().parse_args([*base, "--overwrite"]).overwrite)

    def test_output_guard_preserves_a_different_golden_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "new.c"
            source.write_text("int new_source;\n", encoding="utf-8")
            out = root / "out"
            out.mkdir()
            (out / "input.c").write_text("int old_source;\n", encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "different source"):
                _guard_output_dir(out, source, overwrite=False)
            _guard_output_dir(out, source, overwrite=True)

    def test_output_guard_allows_same_source_but_rejects_source_inside_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            existing = out / "input.c"
            existing.write_text("int same;\n", encoding="utf-8")
            source = Path(tmp) / "source.c"
            source.write_text(existing.read_text(encoding="utf-8"), encoding="utf-8")

            _guard_output_dir(out, source, overwrite=False)
            with self.assertRaisesRegex(SystemExit, "inside --out"):
                _guard_output_dir(out, existing, overwrite=True)

    def test_stale_reports_are_invalidated_and_signature_covers_testbench(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for relative in ("src/hls_top.cpp", "src/hls_top.hpp", "tb/testbench.cpp"):
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")
            before = _project_signature(project)
            (project / "tb/testbench.cpp").write_text("changed", encoding="utf-8")
            self.assertNotEqual(before, _project_signature(project))

            for name in ("conversion_report.md", "conversion_report.json"):
                (project / name).write_text("stale", encoding="utf-8")
            _invalidate_stale_reports(project)
            self.assertFalse((project / "conversion_report.md").exists())
            self.assertFalse((project / "conversion_report.json").exists())

    def test_missing_config_has_a_clean_user_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.yaml"
            with self.assertRaisesRegex(SystemExit, "does not exist"):
                _config_path(type("Args", (), {"config": str(missing)})())

    def test_keep_going_emits_evidence_but_static_errors_remain_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.c"
            source.write_text(
                '#include <stdio.h>\nint bump(int n) { printf("%s", ""); return n + 1; }\n',
                encoding="utf-8",
            )
            out = root / "out"
            args = build_parser().parse_args(
                [
                    "convert",
                    "--input",
                    str(source),
                    "--top",
                    "bump",
                    "--out",
                    str(out),
                    "--no-run-vitis",
                    "--keep-going",
                ]
            )

            passing_host = _state(PhaseResult("software_equivalence", "pass"))
            with patch("c2hlsc_agent.cli.verify_project", return_value=passing_host):
                self.assertEqual(run_convert(args), 1)
            report = json.loads((out / "conversion_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["phases"]["software_equivalence"]["status"], "pass")

    def test_manual_symbolic_repair_rejects_inline_or_legacy_evidence(self):
        with self.assertRaisesRegex(SystemExit, "evidence-text is not accepted"):
            _read_relational_klee_evidence([], "KLEE assertion failure")

        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "klee_report.json"
            report.write_text(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "KLEE crashed",
                        "counterexample_names": [
                            "C2HLSC_RELATIONAL_MISMATCH:return"
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "requires a c2hlsc-klee-report-v1"):
                _read_relational_klee_evidence([str(report)], "")

    def test_manual_symbolic_repair_accepts_only_scoped_relational_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "klee_report.json"
            report.write_text(
                json.dumps(
                    {
                        "schema": "c2hlsc-klee-report-v1",
                        "scope": "golden_hlsc_relational",
                        "status": "fail",
                        "outcome": "counterexample",
                        "failure_kind": "relational_counterexample",
                        "completed_paths": 2,
                        "generated_tests": 1,
                        "timed_out": False,
                        "invocations": 1,
                        "observable_count": 1,
                        "top": "kernel",
                        "assumptions": {
                            "pointer_alias_model": "distinct_pointer_arguments",
                            "hidden_state_model": "no_mutable_hidden_state",
                            "comparison": "return_and_complete_pointer_post_state",
                        },
                        "artifact_sha256": {
                            relative: "0" * 64
                            for relative in (
                                "input.c",
                                "src/hls_top.hpp",
                                "src/hls_top.cpp",
                                "tb/klee_driver.cpp",
                                "tb/leveri_manifest.json",
                            )
                        },
                        "counterexample_names": [
                            "C2HLSC_RELATIONAL_MISMATCH:return"
                        ],
                        "counterexamples": [
                            {
                                "observable": "C2HLSC_RELATIONAL_MISMATCH:return",
                                "error_file": "coverage/klee-out/test000001.c2hlsc_relational.err",
                            }
                        ],
                        "ktest_files": ["coverage/klee-out/test000001.ktest"],
                        "commands": ["must not enter metadata"],
                    }
                ),
                encoding="utf-8",
            )

            evidence, metadata = _read_relational_klee_evidence([str(report)], "")

            self.assertIn("validated_relational_klee", evidence)
            self.assertNotIn("must not enter metadata", evidence)
            self.assertNotIn(str(report.resolve()), evidence)
            self.assertEqual(metadata["scope"], "golden_hlsc_relational")
            self.assertEqual(metadata["counterexample_count"], 1)
            self.assertNotIn("commands", metadata)

    def test_manual_symbolic_repair_binds_report_to_project_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            artifacts = (
                "input.c",
                "src/hls_top.hpp",
                "src/hls_top.cpp",
                "tb/klee_driver.cpp",
                "tb/leveri_manifest.json",
            )
            for relative in artifacts:
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"content:{relative}\n", encoding="utf-8")
            hashes = {
                relative: hashlib.sha256((project / relative).read_bytes()).hexdigest()
                for relative in artifacts
            }
            report = root / "klee_report.json"
            report.write_text(
                json.dumps(
                    {
                        "schema": "c2hlsc-klee-report-v1",
                        "scope": "golden_hlsc_relational",
                        "status": "fail",
                        "outcome": "counterexample",
                        "failure_kind": "relational_counterexample",
                        "invocations": 1,
                        "observable_count": 1,
                        "assumptions": {
                            "pointer_alias_model": "distinct_pointer_arguments",
                            "hidden_state_model": "no_mutable_hidden_state",
                            "comparison": "return_and_complete_pointer_post_state",
                        },
                        "top": "kernel",
                        "artifact_sha256": hashes,
                        "counterexample_names": ["C2HLSC_RELATIONAL_MISMATCH:return"],
                        "counterexamples": [
                            {
                                "observable": "C2HLSC_RELATIONAL_MISMATCH:return",
                                "error_file": "coverage/klee-out/test000001.c2hlsc_relational.err",
                            }
                        ],
                        "ktest_files": ["coverage/klee-out/test000001.ktest"],
                    }
                ),
                encoding="utf-8",
            )

            _, metadata = _read_relational_klee_evidence(
                [str(report)], "", project_dir=project, expected_top="kernel"
            )
            self.assertEqual(metadata["top"], "kernel")

            (project / "src/hls_top.cpp").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "requires a c2hlsc-klee-report-v1"):
                _read_relational_klee_evidence(
                    [str(report)], "", project_dir=project, expected_top="kernel"
                )

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
