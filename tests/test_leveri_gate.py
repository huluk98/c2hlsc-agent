import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent import cli
from c2hlsc_agent.agent_loop import classify_failure
from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.convert import generate_hls_sources
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.hls_project import write_project
from c2hlsc_agent.hls_runner import (
    earliest_failing_phase,
    run_leveri_trace,
    verify_project,
    vitis_executable,
)
from c2hlsc_agent.report import final_status


VECTOR_ADD = """
#include <stdint.h>
void vector_add(const int32_t *a, const int32_t *b, int32_t *out, int n) {
  for (int i = 0; i < n; ++i) out[i] = a[i] + b[i];
}
"""


def _state(*phases: PhaseResult) -> VerificationState:
    state = VerificationState()
    for phase in phases:
        state.add_phase(phase)
    return state


def _vector_add_setup(tmp: Path):
    source = tmp / "input.c"
    source.write_text(VECTOR_ADD, encoding="utf-8")
    cfg = AgentConfig(
        top="vector_add",
        num_tests=8,
        arguments={
            "a": ArgumentConfig(direction="input", length=4),
            "b": ArgumentConfig(direction="input", length=4),
            "out": ArgumentConfig(direction="output", length=4),
            "n": ArgumentConfig(range=(0, 4)),
        },
    )
    return analyze_source(source, "vector_add", cfg), cfg


class LeveriPhaseOrderingTests(unittest.TestCase):
    def test_missing_leveri_phase_is_not_the_earliest_failure(self):
        state = _state(PhaseResult("software_equivalence", "pass"))
        self.assertIsNone(earliest_failing_phase(state, run_vitis_requested=False))

    def test_failed_leveri_phase_is_the_earliest_failure(self):
        state = _state(
            PhaseResult("software_equivalence", "pass"),
            PhaseResult("leveri_trace", "fail", summary="behavior mismatch cycle=3 column=out[1]"),
        )
        self.assertEqual(earliest_failing_phase(state, run_vitis_requested=True), "leveri_trace")

    def test_final_status_requires_leveri_only_when_it_ran(self):
        ran_and_failed = _state(
            PhaseResult("software_equivalence", "pass"),
            PhaseResult("leveri_trace", "fail"),
        )
        self.assertEqual(final_status(ran_and_failed, run_vitis=False, diagnostics_has_errors=False), "fail")
        explicitly_skipped = _state(
            PhaseResult("software_equivalence", "pass"),
            PhaseResult("leveri_trace", "skipped", summary="leveri gate disabled"),
        )
        self.assertEqual(final_status(explicitly_skipped, run_vitis=False, diagnostics_has_errors=False), "pass")
        never_ran = _state(PhaseResult("software_equivalence", "pass"))
        self.assertEqual(final_status(never_ran, run_vitis=False, diagnostics_has_errors=False), "pass")


class LeveriClassificationTests(unittest.TestCase):
    def test_behavior_mismatch_routes_to_failure_analyst(self):
        state = _state(
            PhaseResult("software_equivalence", "pass"),
            PhaseResult(
                "leveri_trace",
                "fail",
                stdout="HLS-LeVeri consistency check failed: behavior mismatch cycle=3 column=out[1] expected=5 actual=6",
            ),
        )
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.family, "leveri_trace_mismatch")
        self.assertEqual(decision.owner_agent, "failure_analyst")
        self.assertIn("first divergent cycle", decision.evidence_needed)

    def test_harness_misalignment_routes_to_testbench_agent(self):
        state = _state(
            PhaseResult("software_equivalence", "pass"),
            PhaseResult(
                "leveri_trace",
                "fail",
                stderr="HLS-LeVeri consistency check failed: stimulus mismatch cycle=2 column=a[0] golden=1 hls=2",
            ),
        )
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.family, "leveri_harness_misalignment")
        self.assertEqual(decision.owner_agent, "shift_left_testbench_agent")

    def test_build_failure_routes_to_testbench_agent(self):
        state = _state(
            PhaseResult("software_equivalence", "pass"),
            PhaseResult("leveri_trace", "fail", stderr="error: expected ';' before '}' token"),
        )
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.owner_agent, "shift_left_testbench_agent")


class LeveriLadderTests(unittest.TestCase):
    def test_leveri_failure_blocks_vitis_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            passing = PhaseResult("software_equivalence", "pass")
            failing = PhaseResult("leveri_trace", "fail", summary="behavior mismatch cycle=1 column=out[0]")
            with patch("c2hlsc_agent.hls_runner.run_software_equivalence", return_value=passing), patch(
                "c2hlsc_agent.hls_runner.run_leveri_trace", return_value=failing
            ), patch("c2hlsc_agent.hls_runner.run_vitis") as vitis:
                state = verify_project(project, run_vitis_requested=True)
        vitis.assert_not_called()
        self.assertEqual(state.status_for("leveri_trace"), "fail")
        for phase in ("csim", "csynth", "cosim"):
            self.assertEqual(state.status_for(phase), "blocked")
            self.assertEqual(state.phases[phase].summary, "leveri trace check failed")

    def test_leveri_disabled_reports_skipped_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            passing = PhaseResult("software_equivalence", "pass")
            vitis_phases = {name: PhaseResult(name, "skipped") for name in ("csim", "csynth", "cosim")}
            with patch("c2hlsc_agent.hls_runner.run_software_equivalence", return_value=passing), patch(
                "c2hlsc_agent.hls_runner.run_leveri_trace"
            ) as leveri, patch("c2hlsc_agent.hls_runner.run_vitis", return_value=vitis_phases):
                state = verify_project(project, run_vitis_requested=False, leveri=False)
        leveri.assert_not_called()
        self.assertEqual(state.status_for("leveri_trace"), "skipped")
        self.assertEqual(final_status(state, run_vitis=False, diagnostics_has_errors=False), "pass")

    def test_software_failure_blocks_leveri_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            failing = PhaseResult("software_equivalence", "fail", summary="Mismatch test=0")
            with patch("c2hlsc_agent.hls_runner.run_software_equivalence", return_value=failing):
                state = verify_project(project, run_vitis_requested=True)
        self.assertEqual(state.status_for("leveri_trace"), "blocked")

    def test_run_leveri_trace_skips_when_bundle_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_leveri_trace(Path(tmp))
        self.assertEqual(result.status, "skipped")


class VitisExecutableTests(unittest.TestCase):
    def test_env_override_wins_when_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "vitis_hls.bat"
            launcher.write_text("@echo off\n", encoding="utf-8")
            with patch.dict(os.environ, {"C2HLSC_VITIS_BIN": str(launcher)}):
                self.assertEqual(vitis_executable(), str(launcher))

    def test_missing_override_falls_back_to_which(self):
        with patch.dict(os.environ, {"C2HLSC_VITIS_BIN": "definitely-not-a-real-binary"}), patch(
            "c2hlsc_agent.hls_runner.shutil.which", return_value=None
        ):
            self.assertIsNone(vitis_executable())


class GoldenSmokeTests(unittest.TestCase):
    def test_smoke_failure_blocks_before_any_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.c"
            source.write_text(VECTOR_ADD, encoding="utf-8")
            out = root / "out"
            failing = PhaseResult("golden_smoke", "fail", summary="golden testbench failed to compile")
            with patch("c2hlsc_agent.cli.shutil.which", return_value="/usr/bin/g++"), patch(
                "c2hlsc_agent.cli.run_command", return_value=failing
            ), patch("c2hlsc_agent.cli.generate_hls_sources") as generate, patch(
                "c2hlsc_agent.cli.verify_project"
            ) as verify:
                code = cli.main(
                    [
                        "convert",
                        "--input",
                        str(source),
                        "--top",
                        "vector_add",
                        "--out",
                        str(out),
                        "--no-run-vitis",
                    ]
                )
        self.assertEqual(code, 1)
        generate.assert_not_called()
        verify.assert_not_called()

    def test_no_leveri_flag_skips_the_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.c"
            source.write_text(VECTOR_ADD, encoding="utf-8")
            out = root / "out"
            passing = VerificationState()
            passing.add_phase(PhaseResult("software_equivalence", "pass"))
            with patch("c2hlsc_agent.cli.run_command") as smoke_command, patch(
                "c2hlsc_agent.cli.verify_project", return_value=passing
            ):
                code = cli.main(
                    [
                        "convert",
                        "--input",
                        str(source),
                        "--top",
                        "vector_add",
                        "--out",
                        str(out),
                        "--no-run-vitis",
                        "--no-leveri",
                    ]
                )
        self.assertEqual(code, 0)
        smoke_command.assert_not_called()

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_smoke_passes_on_a_sound_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis, cfg = _vector_add_setup(root)
            out = root / "out"
            out.mkdir()
            result = cli._golden_trace_smoke(out, analysis, cfg)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "pass")

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_smoke_writes_the_golden_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis, cfg = _vector_add_setup(root)
            out = root / "out"
            out.mkdir()
            cli._golden_trace_smoke(out, analysis, cfg)
            self.assertTrue((out / ".golden_smoke" / "leveri_golden_trace.csv").exists())


class StructuredMismatchEvidenceTests(unittest.TestCase):
    COMPARATOR_LINE = (
        "HLS-LeVeri consistency check failed: behavior mismatch cycle=3 column=out[1] "
        "expected=5 actual=6"
    )

    def test_parse_leveri_divergences_reads_both_kinds(self):
        from c2hlsc_agent.equivalence import parse_leveri_divergences

        text = (
            self.COMPARATOR_LINE
            + "\nHLS-LeVeri consistency check failed: stimulus mismatch cycle=2 column=a[0] golden=1 hls=9"
        )
        divergences = parse_leveri_divergences(text)
        kinds = {d.kind for d in divergences}
        self.assertEqual(kinds, {"behavior", "stimulus"})
        behavior = next(d for d in divergences if d.kind == "behavior")
        self.assertEqual((behavior.cycle, behavior.column, behavior.expected, behavior.actual), ("3", "out[1]", "5", "6"))

    def test_repair_prompt_contains_structured_divergence_section(self):
        from types import SimpleNamespace

        from c2hlsc_agent.llm import build_repair_prompt

        with tempfile.TemporaryDirectory() as tmp:
            analysis, _cfg = _vector_add_setup(Path(tmp))
        decision = SimpleNamespace(
            family="leveri_trace_mismatch",
            next_action="patch",
            repair_scope="generated HLS-C only",
        )
        evidence = "tool banner\n" + self.COMPARATOR_LINE + "\nmore log"
        _system, user = build_repair_prompt(
            analysis, decision, "leveri_trace", evidence, "src/hls_top.cpp", "void vector_add() {}"
        )
        self.assertIn("Structured mismatch evidence", user)
        self.assertIn("behavior divergence at cycle 3, column out[1]: expected=5 actual=6", user)
        structured_at = user.index("Structured mismatch evidence")
        tail_at = user.index("Earliest-failure evidence")
        self.assertLess(structured_at, tail_at)

    def test_repair_prompt_omits_the_section_without_mismatches(self):
        from types import SimpleNamespace

        from c2hlsc_agent.llm import build_repair_prompt

        with tempfile.TemporaryDirectory() as tmp:
            analysis, _cfg = _vector_add_setup(Path(tmp))
        decision = SimpleNamespace(family="synthesis_failure", next_action="patch", repair_scope="hls")
        _system, user = build_repair_prompt(
            analysis, decision, "csynth", "ERROR: [SYNCHK 200-11] boom", "src/hls_top.cpp", "void vector_add() {}"
        )
        self.assertNotIn("Structured mismatch evidence", user)

    def test_host_mismatch_lines_are_structured_too(self):
        from types import SimpleNamespace

        from c2hlsc_agent.llm import build_repair_prompt

        with tempfile.TemporaryDirectory() as tmp:
            analysis, _cfg = _vector_add_setup(Path(tmp))
        decision = SimpleNamespace(family="host_behavior_mismatch", next_action="patch", repair_scope="hls")
        evidence = "Mismatch test=4 arg=out index=2 expected=7 actual=8 seed=1"
        _system, user = build_repair_prompt(
            analysis, decision, "software_equivalence", evidence, "src/hls_top.cpp", "void vector_add() {}"
        )
        self.assertIn("host mismatch test=4 out[2]: expected=7 actual=8 seed=1", user)


class LeveriEndToEndTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("g++") and shutil.which("make"), "g++ and make are required")
    def test_seeded_divergence_fails_the_gate_with_behavior_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis, cfg = _vector_add_setup(root)
            generated = generate_hls_sources(analysis, cfg)
            project = root / "project"
            write_project(project, analysis, generated, cfg)
            hls_source = project / "src" / "hls_top.cpp"
            hls_source.write_text(
                hls_source.read_text(encoding="utf-8").replace("a[i] + b[i]", "a[i] - b[i]"),
                encoding="utf-8",
            )
            result = run_leveri_trace(project)
        self.assertEqual(result.status, "fail")
        evidence = result.stdout + result.stderr
        self.assertIn("behavior mismatch", evidence)

    @unittest.skipUnless(shutil.which("g++") and shutil.which("make"), "g++ and make are required")
    def test_clean_project_passes_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis, cfg = _vector_add_setup(root)
            generated = generate_hls_sources(analysis, cfg)
            project = root / "project"
            write_project(project, analysis, generated, cfg)
            result = run_leveri_trace(project)
        self.assertEqual(result.status, "pass", result.stdout + result.stderr)


def _load_drift_checker():
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "check_generated_testbenches.py"
    spec = importlib.util.spec_from_file_location("check_generated_testbenches", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GeneratedTestbenchDriftTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        analysis, cfg = _vector_add_setup(root)
        generated = generate_hls_sources(analysis, cfg)
        project = root / "project"
        write_project(project, analysis, generated, cfg)
        return project

    def test_manifest_records_generated_file_hashes(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            manifest = json.loads((project / "tb" / "leveri_manifest.json").read_text(encoding="utf-8"))
        hashes = manifest["generated_file_sha256"]
        self.assertIn("tb/leveri_golden_tb.cpp", hashes)
        self.assertIn("tb/leveri_hls_tb.cpp", hashes)
        self.assertEqual(len(hashes["tb/leveri_golden_tb.cpp"]), 64)

    def test_clean_project_passes_the_drift_check(self):
        checker = _load_drift_checker()
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            self.assertEqual(checker.main(["--project", str(project)]), 0)

    def test_hand_edited_testbench_fails_the_drift_check(self):
        checker = _load_drift_checker()
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            golden = project / "tb" / "leveri_golden_tb.cpp"
            golden.write_text(golden.read_text(encoding="utf-8") + "\n// hand edit\n", encoding="utf-8")
            self.assertEqual(checker.main(["--project", str(project)]), 1)

    def test_missing_generated_file_fails_the_drift_check(self):
        checker = _load_drift_checker()
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            (project / "tb" / "leveri_compare.py").unlink()
            self.assertEqual(checker.main(["--project", str(project)]), 1)

    def test_project_without_manifest_is_skipped_not_failed(self):
        checker = _load_drift_checker()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(checker.main(["--project", tmp]), 0)


if __name__ == "__main__":
    unittest.main()
