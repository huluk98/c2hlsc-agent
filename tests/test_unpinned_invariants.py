"""Regression tests for safety guards that otherwise have little direct coverage.

These tests intentionally pin fail-closed behavior: a repair ledger may recover from a
torn write, but verification evidence and mutable buffer observability may not degrade.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent import hlsc_repair_agent as repair
from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.cli import build_parser, run_convert
from c2hlsc_agent.config import AgentConfig, ArgumentConfig, _argument_config, load_config, merge_cli_config
from c2hlsc_agent.contract_planner import plan_contracts
from c2hlsc_agent.equivalence import PhaseResult, VerificationState, parse_mismatches
from c2hlsc_agent.hls_runner import (
    _COSIM_FAILURE_MARKERS,
    _EQUIV_SUCCESS_MARKER,
    _gate_cosim_on_log,
    _gate_equivalence_on_evidence,
)
from c2hlsc_agent.local_hls import _parse_cosim
from c2hlsc_agent.testgen import generate_testbench


def _change(path: str = "src/hls_top.cpp") -> repair.RepairFileChange:
    return repair.RepairFileChange(
        path=path,
        action="test change",
        before_sha256="0" * 64,
        after_sha256="1" * 64,
        diff="(test change)",
    )


def _outcome(iteration: int = 1, changes: tuple[repair.RepairFileChange, ...] | None = None) -> repair.RepairOutcome:
    return repair.RepairOutcome(
        iteration=iteration,
        stage="software_equivalence",
        family="test_double",
        owner_agent="test_double",
        status="applied",
        summary="test outcome",
        target_files=tuple(change.path for change in (changes or ())),
        changes=changes or (),
        evidence_excerpt="",
        next_action="rerun verification",
        repair_scope="src/hls_top.cpp",
    )


class RepairLedgerHardeningTests(unittest.TestCase):
    def test_corrupt_ledger_recovers_and_next_write_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / repair.REPAIR_AUDIT_FILENAME
            ledger.write_text('[{"iteration": 1', encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(repair.load_repair_audit(root), [])
            self.assertIn("treating repair history as empty", stderr.getvalue())

            with patch("c2hlsc_agent.hlsc_repair_agent.os.replace", wraps=os.replace) as replace:
                repair._append_audit(root, _outcome())
            replace.assert_called_once()
            self.assertEqual(len(json.loads(ledger.read_text(encoding="utf-8"))), 1)
            self.assertFalse((root / f".{repair.REPAIR_AUDIT_FILENAME}.tmp").exists())

    def test_legacy_ledger_without_normalized_hashes_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _outcome(changes=(_change(),)).to_dict()
            for entry in payload["changes"]:
                entry.pop("before_norm_sha256")
                entry.pop("after_norm_sha256")
            (root / repair.REPAIR_AUDIT_FILENAME).write_text(json.dumps([payload]), encoding="utf-8")
            [loaded] = repair.load_repair_audit(root)
            self.assertEqual(loaded.changes[0].before_norm_sha256, "")
            self.assertEqual(loaded.changes[0].after_norm_sha256, "")

    def test_structurally_malformed_entries_are_skipped_and_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / repair.REPAIR_AUDIT_FILENAME
            ledger.write_text(
                json.dumps([None, {}, {"iteration": 1, "changes": [None]}]),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(repair.load_repair_audit(root), [])
                repair._append_audit(root, _outcome())

            self.assertIn("ignoring malformed", stderr.getvalue())
            rewritten = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(len(rewritten), 1)
            self.assertEqual(rewritten[0]["summary"], "test outcome")

    def test_rewrite_records_raw_and_normalized_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "x.cpp"
            path.write_text("int x = 1;\n", encoding="utf-8")
            change = repair._rewrite_file(root, path, "format", "int   x = 2;\n")
            self.assertIsNotNone(change)
            assert change is not None
            self.assertEqual(len(change.before_norm_sha256), 64)
            self.assertEqual(len(change.after_norm_sha256), 64)


class _CannedLLM:
    model = "test-model"

    def __init__(self, candidate: str) -> None:
        self.candidate = candidate

    def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> str:
        return f"```cpp\n{self.candidate}```"


class RepairCandidateGuardTests(unittest.TestCase):
    def test_whitespace_only_reproposal_is_rejected_as_oscillation(self):
        source = "void top(int *out) {\n  out[0] = 1;\n}\n"
        candidate = "void top(int *out) {   \n\t out[0] = 1;   \n}\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "input.c").write_text(source, encoding="utf-8")
            (root / "src" / "hls_top.cpp").write_text(source, encoding="utf-8")
            config = AgentConfig(top="top", arguments={"out": ArgumentConfig(length=1)})
            analysis = analyze_source(root / "input.c", "top", config)
            changes, oscillated = repair._llm_repair(
                root,
                analysis,
                type("Decision", (), {"family": "test"})(),
                "software_equivalence",
                "mismatch",
                _CannedLLM(candidate),
                config,
            )
            self.assertEqual(changes, [])
            self.assertTrue(oscillated)
            self.assertEqual((root / "src" / "hls_top.cpp").read_text(encoding="utf-8"), source)

    def test_header_only_mechanical_repair_does_not_suppress_llm_source_repair(self):
        header_change = _change("src/hls_top.hpp")
        source_change = _change("src/hls_top.cpp")
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "fail", summary="missing declaration"))
        analysis = type(
            "Analysis",
            (),
            {"diagnostics": type("Diagnostics", (), {"has_errors": False})()},
        )()
        decision = type(
            "Decision",
            (),
            {
                "status": "repair",
                "family": "compile_error",
                "owner_agent": "repair",
                "next_action": "rerun",
                "repair_scope": "generated source",
            },
        )()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "c2hlsc_agent.hlsc_repair_agent.earliest_failing_phase",
            return_value="software_equivalence",
        ), patch("c2hlsc_agent.hlsc_repair_agent.classify_failure", return_value=decision), patch(
            "c2hlsc_agent.hlsc_repair_agent._repair_missing_standard_includes",
            return_value=[header_change],
        ), patch("c2hlsc_agent.hlsc_repair_agent._repair_restrict_for_cpp", return_value=[]), patch(
            "c2hlsc_agent.hlsc_repair_agent._repair_missing_original_support", return_value=[]
        ), patch("c2hlsc_agent.hlsc_repair_agent._repair_invalid_interface_pragmas", return_value=[]), patch(
            "c2hlsc_agent.hlsc_repair_agent._llm_repair", return_value=([source_change], False)
        ) as llm_repair:
            result = repair.repair_project(
                Path(tmp), analysis, AgentConfig(use_llm=True), state, 1, llm=object()
            )
        llm_repair.assert_called_once()
        self.assertEqual(result.status, "applied_llm")
        self.assertEqual(result.changes, (header_change, source_change))

    def test_header_repair_remains_applied_when_llm_candidate_oscillates(self):
        header_change = _change("src/hls_top.hpp")
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "fail", summary="missing declaration"))
        analysis = type(
            "Analysis",
            (),
            {"diagnostics": type("Diagnostics", (), {"has_errors": False})()},
        )()
        decision = type(
            "Decision",
            (),
            {
                "status": "repair",
                "family": "compile_error",
                "owner_agent": "repair",
                "next_action": "rerun",
                "repair_scope": "generated source",
            },
        )()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "c2hlsc_agent.hlsc_repair_agent.earliest_failing_phase",
            return_value="software_equivalence",
        ), patch("c2hlsc_agent.hlsc_repair_agent.classify_failure", return_value=decision), patch(
            "c2hlsc_agent.hlsc_repair_agent._repair_missing_standard_includes",
            return_value=[header_change],
        ), patch("c2hlsc_agent.hlsc_repair_agent._repair_restrict_for_cpp", return_value=[]), patch(
            "c2hlsc_agent.hlsc_repair_agent._repair_missing_original_support", return_value=[]
        ), patch("c2hlsc_agent.hlsc_repair_agent._repair_invalid_interface_pragmas", return_value=[]), patch(
            "c2hlsc_agent.hlsc_repair_agent._llm_repair", return_value=([], True)
        ):
            result = repair.repair_project(
                Path(tmp), analysis, AgentConfig(use_llm=True), state, 1, llm=object()
            )

        self.assertEqual(result.status, "applied")
        self.assertTrue(result.changed)
        self.assertEqual(result.changes, (header_change,))
        self.assertIn("oscillation guard", result.summary)

    def test_include_inference_uses_only_missing_declaration_lines(self):
        evidence = "note: memcpy is optimized here\nerror: widget was not declared in this scope"
        self.assertEqual(repair._includes_needed_from_evidence(evidence), [])
        self.assertEqual(
            repair._includes_needed_from_evidence("error: memcpy was not declared in this scope"),
            ["<string.h>"],
        )

    def test_restrict_rewrite_preserves_preprocessor_plumbing(self):
        source = (
            "#ifndef restrict\n#define restrict __restrict__\n#endif\n"
            "void top(int * restrict out);\n"
        )
        rewritten = repair._replace_restrict_tokens(source)
        self.assertIn("#ifndef restrict", rewritten)
        self.assertIn("#define restrict __restrict__", rewritten)
        self.assertIn("int * __restrict__ out", rewritten)


def _fail_state() -> VerificationState:
    state = VerificationState()
    state.add_phase(PhaseResult("software_equivalence", "fail", stderr="unrepairable mismatch"))
    return state


class RunConvertOscillationGuardTests(unittest.TestCase):
    marker = "// oscillation test double\n"

    def _alternating_repair(self, project_dir, analysis, config, state, iteration, llm=None, audit_store=None):
        path = Path(project_dir) / "src" / "hls_top.cpp"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(self.marker, "") if self.marker in text else self.marker + text,
            encoding="utf-8",
        )
        return _outcome(iteration, (_change(),))

    def test_two_state_cycle_stops_before_iteration_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.c"
            input_path.write_text("int bump(int n) { return n + 1; }\n", encoding="utf-8")
            out_dir = root / "out"
            args = build_parser().parse_args(
                [
                    "convert", "--input", str(input_path), "--top", "bump", "--out", str(out_dir),
                    "--no-run-vitis", "--max-iterations", "4", "--auto-repair", "--verbose",
                ]
            )
            stdout = io.StringIO()
            with patch("c2hlsc_agent.cli.verify_project", side_effect=lambda *a, **k: _fail_state()) as verify, patch(
                "c2hlsc_agent.cli.repair_project", side_effect=self._alternating_repair
            ), contextlib.redirect_stdout(stdout):
                rc = run_convert(args)
            self.assertEqual(rc, 1)
            self.assertEqual(verify.call_count, 2)
            self.assertIn("oscillation", stdout.getvalue())


class ParsingAndConfigurationInvariantTests(unittest.TestCase):
    def test_return_mismatch_form_is_parsed(self):
        [mismatch] = parse_mismatches("Mismatch test=5 return expected=12 actual=13 seed=123")
        self.assertEqual((mismatch.test_index, mismatch.argument, mismatch.seed), (5, "return", 123))
        self.assertIsNone(mismatch.element_index)

    def test_no_llm_overrides_config_and_parser_exposes_flag(self):
        args = argparse.Namespace(use_llm=False, no_llm=True)
        self.assertFalse(merge_cli_config(AgentConfig(use_llm=True), args).use_llm)
        parsed = build_parser().parse_args(
            ["convert", "--input", "i.c", "--top", "t", "--out", "o", "--no-llm"]
        )
        self.assertTrue(parsed.no_llm)

    def test_argument_directions_and_nonzero_test_counts_are_validated(self):
        for bad in ("out", "OUTPUT", "inputt"):
            with self.subTest(direction=bad), self.assertRaises(ValueError):
                _argument_config({"direction": bad})
        self.assertEqual(_argument_config({"direction": "inout"}).direction, "inout")
        with self.assertRaises(ValueError):
            merge_cli_config(AgentConfig(), argparse.Namespace(num_tests=0))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"input_files":["input.c"],"top":"k","num_tests":0}', encoding="utf-8")
            self.assertEqual(load_config(path).num_tests, 1)


class VerificationEvidenceInvariantTests(unittest.TestCase):
    def test_every_cosim_failure_marker_overrides_zero_exit(self):
        self.assertTrue(_COSIM_FAILURE_MARKERS)
        for marker in _COSIM_FAILURE_MARKERS:
            with self.subTest(marker=marker):
                result = PhaseResult("cosim", "pass", returncode=0, stdout=marker.upper())
                self.assertEqual(_gate_cosim_on_log(result).status, "fail")

    def test_cosim_log_file_requires_positive_pass_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cosim.log"
            path.write_text("Vitis exited 0\n", encoding="utf-8")
            markerless = _gate_cosim_on_log(PhaseResult("cosim", "pass", returncode=0, log_path=path))
            self.assertEqual(markerless.status, "fail")
            self.assertIn("no positive", markerless.summary)
            path.write_text("C/RTL co-simulation finished: PASS\n", encoding="utf-8")
            self.assertEqual(
                _gate_cosim_on_log(PhaseResult("cosim", "pass", returncode=0, log_path=path)).status,
                "pass",
            )

    def test_missing_cosim_log_and_marker_is_a_failure(self):
        result = PhaseResult(
            "cosim", "pass", returncode=0, stdout="build complete", log_path=Path("/nonexistent/cosim.log")
        )
        self.assertEqual(_gate_cosim_on_log(result).status, "fail")

    def test_host_equivalence_requires_comparison_success_marker(self):
        markerless = PhaseResult("software_equivalence", "pass", returncode=0, stdout="linked")
        gated = _gate_equivalence_on_evidence(markerless)
        self.assertEqual(gated.status, "fail")
        self.assertIn(_EQUIV_SUCCESS_MARKER, gated.summary)
        marked = PhaseResult(
            "software_equivalence",
            "pass",
            returncode=0,
            stdout="c2hlsc_agent: all 4 tests passed, comparisons=32",
        )
        self.assertIs(_gate_equivalence_on_evidence(marked), marked)

    def test_bambu_requires_positive_execution_count(self):
        for text in ("Bambu finished synthesis\n", "Number of executions : 0\n"):
            with self.subTest(text=text):
                ok, _ = _parse_cosim(text, 0)
                self.assertFalse(ok)
        ok, summary = _parse_cosim("Number of executions : 16\n", 0)
        self.assertTrue(ok)
        self.assertIn("16 vectors", summary)


class PointerObservabilityInvariantTests(unittest.TestCase):
    def _analyze(self, source: str, top: str, **lengths):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "input.c"
        path.write_text(source, encoding="utf-8")
        config = AgentConfig(
            top=top,
            arguments={name: ArgumentConfig(length=length) for name, length in lengths.items()},
        )
        return analyze_source(path, top, config), config

    def test_const_input_void_top_fails_closed_when_nothing_is_compared(self):
        analysis, config = self._analyze(
            "void sink(const int *a, int n) { int x = a[0] + n; (void)x; }\n", "sink", a=8
        )
        testbench = generate_testbench(analysis, config)
        self.assertEqual(analysis.function.args[0].direction, "input")
        self.assertIn("if (comparisons_done == 0)", testbench)
        self.assertNotIn("++comparisons_done", testbench)

    def test_mutable_read_only_pointer_remains_observable_and_compared(self):
        analysis, config = self._analyze(
            "void inspect(int *a, int n) { int x = a[0] + n; (void)x; }\n", "inspect", a=8
        )
        testbench = generate_testbench(analysis, config)
        self.assertEqual(analysis.function.args[0].direction, "inout")
        self.assertIn("++comparisons_done", testbench)
        self.assertIn("ref_a[i]", testbench)

    def test_indirect_write_shapes_never_become_input_only(self):
        cases = (
            ("void f(int *out, int n) { fill(out, n); }", "out"),
            ("void f(int out[4][4], int n) { out[1][2] = n; }", "out"),
            ("void f(int *out, int n) { *(out + n) = 1; }", "out"),
            ("void f(struct box *p, int n) { p->v = n; }", "p"),
        )
        for source, name in cases:
            with self.subTest(source=source):
                analysis, _ = self._analyze(source, "f", **{name: 8})
                direction = next(arg.direction for arg in analysis.function.args if arg.name == name)
                self.assertIn(direction, {"output", "inout"})


class _PlannerLLM:
    model = "planner-test"

    def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> str:
        return (
            '```json\n{"arguments":{"out":{"direction":"input"},'
            '"aux":{"direction":"inout"}}}\n```'
        )


class ContractPlannerNarrowingInvariantTests(unittest.TestCase):
    def test_written_buffer_narrowing_is_rejected_while_widening_applies(self):
        source = (
            "void twin(int *out, int *aux, int n) {\n"
            "  for (int i = 0; i < 8; ++i) { out[i] = i; aux[i] = i * 2; }\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.c"
            path.write_text(source, encoding="utf-8")
            config = AgentConfig(
                top="twin",
                arguments={"out": ArgumentConfig(length=8), "aux": ArgumentConfig(length=8)},
            )
            analysis = analyze_source(path, "twin", config)
            result = plan_contracts(analysis, config, _PlannerLLM(), source)
        self.assertIn("rejected direction 'input'", result.skipped["out"])
        self.assertIsNone(config.arguments["out"].direction)
        self.assertEqual(config.arguments["aux"].direction, "inout")


if __name__ == "__main__":
    unittest.main()
