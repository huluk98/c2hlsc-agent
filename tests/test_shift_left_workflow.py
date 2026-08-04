from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from c2hlsc_agent.agent_loop import classify_failure
from c2hlsc_agent.cli import main
from c2hlsc_agent.config import AgentConfig, merge_cli_config
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.hls_runner import (
    _report_backed_phase,
    earliest_failing_phase,
    run_shift_left_checks,
    verify_project,
)
from c2hlsc_agent.report import final_status


def _shift_phases(klee_status: str = "skipped") -> dict[str, PhaseResult]:
    return {
        "shift_left_trace": PhaseResult("shift_left_trace", "pass"),
        "coverage_gcov": PhaseResult("coverage_gcov", "pass"),
        "symbolic_klee": PhaseResult("symbolic_klee", klee_status, summary="klee not found"),
    }


def _relational_klee_metadata() -> dict[str, object]:
    return {
        "schema": "c2hlsc-klee-report-v1",
        "scope": "golden_hlsc_relational",
        "outcome": "counterexample",
        "failure_kind": "relational_counterexample",
        "completed_paths": 3,
        "generated_tests": 1,
        "timed_out": False,
        "invocations": 1,
        "observable_count": 4,
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
        "counterexample_names": ["C2HLSC_RELATIONAL_MISMATCH:out"],
        "counterexample_count": 1,
    }


class ShiftLeftWorkflowTests(unittest.TestCase):
    def test_report_backed_phase_preserves_optional_skip(self):
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            (project / "coverage").mkdir()
            command_log = project / "symbolic_klee.log"

            def run(*args, **kwargs):
                (project / "coverage" / "klee_report.json").write_text(
                    json.dumps(
                        {
                            "schema": "c2hlsc-klee-report-v1",
                            "scope": "golden_hlsc_relational",
                            "status": "skipped",
                            "reason": "klee not found",
                        }
                    ),
                    encoding="utf-8",
                )
                return PhaseResult("symbolic_klee", "pass", returncode=0, log_path=command_log)

            with patch(
                "c2hlsc_agent.hls_runner._run_make_phase",
                side_effect=run,
            ):
                result = _report_backed_phase(
                    project, "klee-coverage", "symbolic_klee", "klee_report.json", 10, False
                )
            self.assertEqual(result.status, "skipped")
            self.assertIn("klee not found", result.summary)
            self.assertEqual(result.log_path, command_log)

    def test_report_backed_phase_rejects_stale_report(self):
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            (project / "coverage").mkdir()
            report = project / "coverage" / "klee_report.json"
            report.write_text('{"status":"pass"}\n', encoding="utf-8")
            with patch(
                "c2hlsc_agent.hls_runner._run_make_phase",
                return_value=PhaseResult("symbolic_klee", "pass", returncode=0),
            ):
                result = _report_backed_phase(
                    project, "klee-coverage", "symbolic_klee", "klee_report.json", 10, False
                )
            self.assertEqual(result.status, "fail")
            self.assertFalse(report.exists())

    def test_report_backed_phase_reads_blocked_report_after_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            (project / "coverage").mkdir()
            command_log = project / "symbolic_klee.log"

            def run(*args, **kwargs):
                (project / "coverage" / "klee_report.json").write_text(
                    json.dumps(
                        {
                            "schema": "c2hlsc-klee-report-v1",
                            "scope": "golden_hlsc_relational",
                            "status": "blocked",
                            "reason": "timeout",
                        }
                    ),
                    encoding="utf-8",
                )
                return PhaseResult("symbolic_klee", "fail", returncode=1, log_path=command_log)

            with patch("c2hlsc_agent.hls_runner._run_make_phase", side_effect=run):
                result = _report_backed_phase(
                    project, "klee-coverage", "symbolic_klee", "klee_report.json", 10, False
                )
            self.assertEqual(result.status, "blocked")
            self.assertIn("timeout", result.summary)
            self.assertEqual(result.log_path, command_log)

    def test_report_backed_phase_rejects_unscoped_pass_and_non_object_json(self):
        for payload in ({"status": "pass"}, []):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as raw:
                project = Path(raw)
                (project / "coverage").mkdir()

                def run(*args, **kwargs):
                    (project / "coverage" / "klee_report.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                    return PhaseResult("symbolic_klee", "pass", returncode=0)

                with patch("c2hlsc_agent.hls_runner._run_make_phase", side_effect=run):
                    result = _report_backed_phase(
                        project,
                        "klee-coverage",
                        "symbolic_klee",
                        "klee_report.json",
                        10,
                        False,
                    )

                self.assertEqual(result.status, "fail")

    def test_report_backed_phase_preserves_allowlisted_relational_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            (project / "coverage").mkdir()
            payload = {"status": "fail", "reason": "mismatch", **_relational_klee_metadata()}

            def run(*args, **kwargs):
                (project / "coverage" / "klee_report.json").write_text(
                    json.dumps({**payload, "commands": ["private command"]}),
                    encoding="utf-8",
                )
                return PhaseResult("symbolic_klee", "fail", returncode=1)

            with patch("c2hlsc_agent.hls_runner._run_make_phase", side_effect=run):
                result = _report_backed_phase(
                    project, "klee-coverage", "symbolic_klee", "klee_report.json", 10, False
                )

            self.assertEqual(result.status, "fail")
            self.assertEqual(result.metadata, _relational_klee_metadata())
            self.assertNotIn("commands", result.metadata)

    def test_report_backed_phase_accepts_nonvacuous_exact_schema_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            (project / "coverage").mkdir()

            def run(*args, **kwargs):
                (project / "coverage" / "klee_report.json").write_text(
                    json.dumps(
                        {
                            "schema": "c2hlsc-klee-report-v1",
                            "scope": "golden_hlsc_relational",
                            "status": "pass",
                            "outcome": "no_counterexample",
                            "failure_kind": None,
                            "completed_paths": 2,
                            "generated_tests": 1,
                            "timed_out": False,
                            "invocations": 1,
                            "observable_count": 4,
                            "top": "kernel",
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
                            "bounded_lengths": {"out": 4},
                            "scalar_ranges": {"n": [0, 4]},
                            "assumptions": {
                                "pointer_alias_model": "distinct_pointer_arguments",
                                "hidden_state_model": "no_mutable_hidden_state",
                                "comparison": "return_and_complete_pointer_post_state",
                            },
                            "counterexample_names": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return PhaseResult("symbolic_klee", "pass", returncode=0)

            with patch("c2hlsc_agent.hls_runner._run_make_phase", side_effect=run):
                result = _report_backed_phase(
                    project, "klee-coverage", "symbolic_klee", "klee_report.json", 10, False
                )

            self.assertEqual(result.status, "pass")
            self.assertEqual(result.metadata["bounded_lengths"], {"out": 4})
            self.assertEqual(result.metadata["scalar_ranges"], {"n": [0, 4]})
            self.assertEqual(
                result.metadata["assumptions"]["pointer_alias_model"],
                "distinct_pointer_arguments",
            )

    def test_report_backed_phase_fails_closed_without_report(self):
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            with patch(
                "c2hlsc_agent.hls_runner._run_make_phase",
                return_value=PhaseResult("symbolic_klee", "pass", returncode=0),
            ):
                result = _report_backed_phase(
                    project, "klee-coverage", "symbolic_klee", "klee_report.json", 10, False
                )
            self.assertEqual(result.status, "fail")
            self.assertIn("exited 0", result.summary)

    def test_trace_failure_blocks_later_shift_left_checks(self):
        failure = PhaseResult("shift_left_trace", "fail", summary="trace mismatch")
        with patch("c2hlsc_agent.hls_runner._run_make_phase", return_value=failure):
            phases = run_shift_left_checks(Path("/unused"))
        self.assertEqual(phases["shift_left_trace"].status, "fail")
        self.assertEqual(phases["coverage_gcov"].status, "blocked")
        self.assertEqual(phases["symbolic_klee"].status, "blocked")

    def test_verify_runs_shift_left_before_hls_and_allows_klee_skip(self):
        calls: list[str] = []

        def shift(*args, **kwargs):
            calls.append("shift-left")
            return _shift_phases()

        def vitis(*args, **kwargs):
            calls.append("vitis")
            return {
                phase: PhaseResult(phase, "pass") for phase in ("csim", "csynth", "cosim")
            }

        with patch(
            "c2hlsc_agent.hls_runner.run_software_equivalence",
            return_value=PhaseResult("software_equivalence", "pass"),
        ), patch("c2hlsc_agent.hls_runner.run_shift_left_checks", side_effect=shift), patch(
            "c2hlsc_agent.hls_runner.run_vitis", side_effect=vitis
        ):
            state = verify_project(Path("/unused"), True)
        self.assertEqual(calls, ["shift-left", "vitis"])
        self.assertEqual(state.status_for("symbolic_klee"), "skipped")
        self.assertEqual(final_status(state, True, False), "pass")

    def test_trace_failure_blocks_hls_and_routes_to_testbench_agent(self):
        phases = _shift_phases()
        phases["shift_left_trace"] = PhaseResult("shift_left_trace", "fail")
        with patch(
            "c2hlsc_agent.hls_runner.run_software_equivalence",
            return_value=PhaseResult("software_equivalence", "pass"),
        ), patch("c2hlsc_agent.hls_runner.run_shift_left_checks", return_value=phases), patch(
            "c2hlsc_agent.hls_runner.run_vitis"
        ) as vitis:
            state = verify_project(Path("/unused"), True)
        vitis.assert_not_called()
        self.assertEqual(state.status_for("csim"), "blocked")
        self.assertEqual(earliest_failing_phase(state, True), "shift_left_trace")
        self.assertEqual(final_status(state, True, False), "fail")
        analysis = classify_failure(state, True)
        self.assertEqual(analysis.family, "shift_left_trace_failure")
        self.assertEqual(analysis.owner_agent, "shift_left_testbench_agent")

    def test_non_relational_klee_failure_is_blocked_and_does_not_block_hls(self):
        trace = PhaseResult(
            "shift_left_trace",
            "pass",
            stdout="HLS-LeVeri consistency check passed",
        )
        coverage = PhaseResult("coverage_gcov", "pass")
        klee_failure = PhaseResult("symbolic_klee", "fail", summary="klee crashed")
        with patch(
            "c2hlsc_agent.hls_runner._run_make_phase", return_value=trace
        ), patch(
            "c2hlsc_agent.hls_runner._report_backed_phase",
            side_effect=[coverage, klee_failure],
        ):
            phases = run_shift_left_checks(Path("/unused"))
        self.assertEqual(phases["coverage_gcov"].status, "pass")
        self.assertEqual(phases["symbolic_klee"].status, "blocked")
        self.assertIn("not a validated relational counterexample", phases["symbolic_klee"].summary)

    def test_shift_left_preserves_structured_relational_klee_counterexample(self):
        trace = PhaseResult(
            "shift_left_trace",
            "pass",
            stdout="HLS-LeVeri consistency check passed",
        )
        coverage = PhaseResult("coverage_gcov", "pass")
        relational_failure = PhaseResult(
            "symbolic_klee", "fail", metadata=_relational_klee_metadata()
        )
        with patch(
            "c2hlsc_agent.hls_runner._run_make_phase", return_value=trace
        ), patch(
            "c2hlsc_agent.hls_runner._report_backed_phase",
            side_effect=[coverage, relational_failure],
        ):
            phases = run_shift_left_checks(Path("/unused"))

        self.assertEqual(phases["symbolic_klee"].status, "fail")
        self.assertEqual(
            phases["symbolic_klee"].metadata["counterexample_names"],
            ["C2HLSC_RELATIONAL_MISMATCH:out"],
        )

    def test_relational_fields_without_reserved_mismatch_name_are_blocked(self):
        trace = PhaseResult(
            "shift_left_trace",
            "pass",
            stdout="HLS-LeVeri consistency check passed",
        )
        coverage = PhaseResult("coverage_gcov", "pass")
        metadata = _relational_klee_metadata()
        metadata["counterexample_names"] = ["division_by_zero"]
        relational_failure = PhaseResult("symbolic_klee", "fail", metadata=metadata)
        with patch(
            "c2hlsc_agent.hls_runner._run_make_phase", return_value=trace
        ), patch(
            "c2hlsc_agent.hls_runner._report_backed_phase",
            side_effect=[coverage, relational_failure],
        ):
            phases = run_shift_left_checks(Path("/unused"))

        self.assertEqual(phases["symbolic_klee"].status, "blocked")

    def test_structured_relational_klee_counterexample_blocks_hls_and_routes_repair(self):
        phases = _shift_phases()
        phases["symbolic_klee"] = PhaseResult(
            "symbolic_klee",
            "fail",
            summary="bounded relational mismatch",
            metadata=_relational_klee_metadata(),
        )
        with patch(
            "c2hlsc_agent.hls_runner.run_software_equivalence",
            return_value=PhaseResult("software_equivalence", "pass"),
        ), patch(
            "c2hlsc_agent.hls_runner.run_shift_left_checks", return_value=phases
        ), patch("c2hlsc_agent.hls_runner.run_vitis") as vitis:
            state = verify_project(Path("/unused"), True)

        vitis.assert_not_called()
        self.assertEqual(state.status_for("symbolic_klee"), "fail")
        self.assertEqual(state.status_for("csim"), "blocked")
        self.assertEqual(earliest_failing_phase(state, True), "symbolic_klee")
        self.assertEqual(final_status(state, True, False), "fail")
        analysis = classify_failure(state, True)
        self.assertEqual(analysis.family, "klee_relational_counterexample")
        self.assertEqual(analysis.owner_agent, "failure_analyst")

    def test_shift_left_is_default_on_with_cli_escape_hatch(self):
        config = AgentConfig()
        self.assertTrue(config.run_shift_left)
        args = Namespace(no_shift_left=True, shift_left=False)
        merged = merge_cli_config(config, args)
        self.assertFalse(merged.run_shift_left)

    def test_convert_writes_shift_left_evidence_and_knowledge_graph(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "input.c"
            source.write_text("int add_one(int value) { return value + 1; }\n", encoding="utf-8")
            project = root / "project"
            rc = main(
                [
                    "convert",
                    "--input",
                    str(source),
                    "--top",
                    "add_one",
                    "--out",
                    str(project),
                    "--no-run-vitis",
                    "--no-llm",
                ]
            )
            self.assertEqual(rc, 0)
            report = json.loads((project / "conversion_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["shift_left_trace"], "pass")
            self.assertEqual(report["coverage_gcov"], "pass")
            self.assertEqual(report["symbolic_klee"], "skipped")
            self.assertEqual(
                report["relational_klee"]["scope"], "golden_hlsc_relational"
            )
            self.assertIn("verification_knowledge_graph.json", report["generated_files"])
            graph = json.loads(
                (project / "verification_knowledge_graph.json").read_text(encoding="utf-8")
            )
            nodes = {node["id"]: node for node in graph["nodes"]}
            self.assertEqual(
                nodes["phase:shift_left_trace"]["properties"]["status"], "PASS"
            )
            self.assertEqual(nodes["phase:symbolic_klee"]["properties"]["status"], "SKIP")
            self.assertEqual(
                nodes["phase:symbolic_klee"]["properties"]["scope"],
                "golden_hlsc_relational",
            )
            self.assertIn("artifact:coverage/klee_report.json", nodes)


if __name__ == "__main__":
    unittest.main()
