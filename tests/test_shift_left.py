"""Tests for the shift-left tier: stimulus schedule, dual-tier checks, coverage, refinement."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from c2hlsc_agent.agent_loop import classify_failure, classify_log_family
from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.convert import generate_hls_sources
from c2hlsc_agent.coverage_refine import (
    RefinementError,
    gate_coverage,
    ktest_to_vector,
    parse_ktest,
    refine_project,
    regenerate,
)
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.hls_project import write_project
from c2hlsc_agent.hls_runner import PHASE_ORDER, required_phases
from c2hlsc_agent.leveri_testgen import _columns, generate_leveri_testbenches
from c2hlsc_agent.stimulus import ExtraVector, StimulusError, render_helpers, validate_directed
from c2hlsc_agent import toolchain


HAVE_BUILD = shutil.which("g++") is not None and shutil.which("make") is not None
HAVE_GCOV = HAVE_BUILD and shutil.which("gcov") is not None
HAVE_KLEE = HAVE_GCOV and shutil.which("klee") is not None and shutil.which("clang++") is not None

VECTOR_ADD = """#include <stdint.h>
void vector_add(const int32_t *a, const int32_t *b, int32_t *out, int n) {
  for (int i = 0; i < n; ++i) out[i] = a[i] + b[i];
}
"""

UNREACHABLE_BRANCH = """#include <stdint.h>
int32_t picky(const int32_t *a, int32_t n) {
  int32_t acc = 0;
  if (n == 3) { acc += 7; }
  for (int i = 0; i < n; ++i) { acc += a[i]; }
  return acc;
}
"""

NEEDLE = """#include <stdint.h>
int32_t needle(const int32_t *a, int32_t n) {
  int32_t acc = 0;
  for (int i = 0; i < n; ++i) {
    if (a[i] == 12345) { acc += 1000; } else { acc += a[i]; }
  }
  return acc;
}
"""


def _project(tmp: Path, source: str, top: str, arguments: dict, **kwargs) -> tuple[Path, object, AgentConfig]:
    path = tmp / "input.c"
    path.write_text(source, encoding="utf-8")
    config = AgentConfig(top=top, input_files=[path], arguments=arguments, **kwargs)
    analysis = analyze_source(path, top, config)
    generated = generate_hls_sources(analysis, config)
    project = tmp / "project"
    write_project(project, analysis, generated, config)
    return project, analysis, config


def _load_generated_module(project: Path, relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, project / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_ktest(path: Path, objects: dict[str, bytes]) -> None:
    with path.open("wb") as handle:
        handle.write(b"KTEST")
        handle.write(struct.pack(">I", 3))
        handle.write(struct.pack(">I", 1))
        handle.write(struct.pack(">I", 6))
        handle.write(b"driver")
        handle.write(struct.pack(">I", 0))
        handle.write(struct.pack(">I", 0))
        handle.write(struct.pack(">I", len(objects)))
        for name, blob in objects.items():
            encoded = name.encode("utf-8")
            handle.write(struct.pack(">I", len(encoded)))
            handle.write(encoded)
            handle.write(struct.pack(">I", len(blob)))
            handle.write(blob)


class DirectedScheduleTests(unittest.TestCase):
    def test_configured_schedule_drives_the_emitted_patterns(self) -> None:
        config = AgentConfig(directed_tests=["zeros", "alternating"])
        helpers = render_helpers(config, "test_idx")
        self.assertIn("test_idx == 0) {  // zeros", helpers)
        self.assertIn("test_idx == 1) {  // alternating", helpers)
        self.assertNotIn("// minmax", helpers)

    def test_empty_schedule_means_no_directed_cases(self) -> None:
        helpers = render_helpers(AgentConfig(directed_tests=[]), "cycle")
        self.assertIn("(none — all random)", helpers)
        self.assertNotIn("// zeros", helpers)
        # The bounded-scalar corners disappear too, or the knob would only half apply.
        self.assertNotIn("cycle == 0", helpers)

    def test_unknown_pattern_is_rejected_not_ignored(self) -> None:
        with self.assertRaises(StimulusError) as ctx:
            validate_directed(["zeros", "sawtooth"])
        self.assertIn("sawtooth", str(ctx.exception))

    def test_default_schedule_is_the_documented_four(self) -> None:
        self.assertEqual(
            validate_directed(AgentConfig().directed_tests),
            ["zeros", "ones", "minmax", "alternating"],
        )


class TraceSchemaTests(unittest.TestCase):
    def _analysis(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "input.c"
        path.write_text(VECTOR_ADD, encoding="utf-8")
        config = AgentConfig(
            top="vector_add",
            num_tests=8,
            arguments={
                "a": ArgumentConfig(direction="input", length=4),
                "b": ArgumentConfig(direction="input", length=4),
                "out": ArgumentConfig(direction="output", length=4),
                "n": ArgumentConfig(range=(0, 4)),
            },
        )
        return analyze_source(path, "vector_add", config), config

    def test_output_columns_carry_their_active_length(self) -> None:
        analysis, _ = self._analysis()
        columns = _columns(analysis.function.args, analysis.function.return_type)
        outputs = [column for column in columns if column["role"] == "out"]
        self.assertTrue(outputs)
        for column in outputs:
            self.assertEqual(column["active_length_arg"], "n")
            self.assertEqual(columns[column["active_length_column"]]["name"], "n")

    def test_manifest_declares_the_full_dual_tier(self) -> None:
        analysis, config = self._analysis()
        manifest = json.loads(generate_leveri_testbenches(analysis, config).manifest_json)
        for check in (
            "static_control_flow_alignment",
            "static_data_dependency_alignment",
            "dynamic_output_consistency",
            "coverage_driven_refinement",
        ):
            self.assertIn(check, manifest["checks"])
        self.assertEqual(manifest["directed_tests"], ["zeros", "ones", "minmax", "alternating"])
        self.assertTrue(manifest["columns"])


class StaticStructuralTierTests(unittest.TestCase):
    """The static tier compares the two HARNESSES, so it separates TB bugs from design bugs."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project, _, _ = _project(
            Path(tmp.name),
            VECTOR_ADD,
            "vector_add",
            {
                "a": ArgumentConfig(direction="input", length=4),
                "b": ArgumentConfig(direction="input", length=4),
                "out": ArgumentConfig(direction="output", length=4),
                "n": ArgumentConfig(range=(0, 4)),
            },
            num_tests=4,
        )
        self.compare = _load_generated_module(self.project, "tb/leveri_compare.py", "leveri_compare_under_test")

    def test_paired_harnesses_have_identical_structure(self) -> None:
        golden = self.compare.normalize_source(
            (self.project / "tb" / "leveri_golden_tb.cpp").read_text(encoding="utf-8"), "vector_add"
        )
        hls = self.compare.normalize_source(
            (self.project / "tb" / "leveri_hls_tb.cpp").read_text(encoding="utf-8"), "vector_add"
        )
        self.assertEqual(self.compare.cfg_signature(golden), self.compare.cfg_signature(hls))
        self.assertEqual(self.compare.ddg_signature(golden), self.compare.ddg_signature(hls))
        self.assertTrue(self.compare.cfg_signature(golden), "the CFG signature must not be empty")

    def test_control_flow_divergence_is_detected(self) -> None:
        golden = "int main() { for (int i = 0; i < 4; ++i) { x = 1; } return 0; }"
        hls = "int main() { for (int i = 0; i < 4; ++i) { if (q) { x = 1; } } return 0; }"
        self.assertNotEqual(self.compare.cfg_signature(golden), self.compare.cfg_signature(hls))

    def test_data_dependency_divergence_is_detected(self) -> None:
        golden = "int main() { x = a + b; return 0; }"
        hls = "int main() { x = a + c; return 0; }"
        self.assertEqual(self.compare.cfg_signature(golden), self.compare.cfg_signature(hls))
        self.assertNotEqual(self.compare.ddg_signature(golden), self.compare.ddg_signature(hls))

    def test_normalization_ignores_the_contract_differences(self) -> None:
        golden = 'extern "C" {\n#include "../input.c"\n}\nint main() { f_ref(); }'
        hls = '#include "../src/hls_top.hpp"\nint main() { f(); }'
        self.assertEqual(
            self.compare.cfg_signature(self.compare.normalize_source(golden, "f")),
            self.compare.cfg_signature(self.compare.normalize_source(hls, "f")),
        )

    @unittest.skipUnless(HAVE_BUILD, "g++ and make are required")
    def test_one_sided_harness_change_fails_the_run(self) -> None:
        clean = subprocess.run(["make", "-C", str(self.project), "leveri-test"], capture_output=True, text=True)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)

        hls_tb = self.project / "tb" / "leveri_hls_tb.cpp"
        hls_tb.write_text(
            hls_tb.read_text(encoding="utf-8").replace(
                "  std::mt19937_64 rng = make_trace_rng();",
                "  std::mt19937_64 rng = make_trace_rng();\n  if (trace.good()) { trace.flush(); }",
            ),
            encoding="utf-8",
        )
        broken = subprocess.run(["make", "-C", str(self.project), "leveri-test"], capture_output=True, text=True)
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("static control-flow check failed", broken.stdout + broken.stderr)


class ExtraVectorTests(unittest.TestCase):
    def test_vectors_are_baked_into_both_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, _ = _project(
                Path(tmp),
                VECTOR_ADD,
                "vector_add",
                {
                    "a": ArgumentConfig(direction="input", length=4),
                    "b": ArgumentConfig(direction="input", length=4),
                    "out": ArgumentConfig(direction="output", length=4),
                    "n": ArgumentConfig(range=(0, 4)),
                },
                num_tests=4,
                extra_vectors=[ExtraVector({"a": [9, 8, 7, 6], "b": [1, 1, 1, 1], "n": 4}, origin="klee:test1")],
            )
            for relative in ("tb/testbench.cpp", "tb/leveri_golden_tb.cpp", "tb/leveri_hls_tb.cpp"):
                text = (project / relative).read_text(encoding="utf-8")
                self.assertIn("c2hlsc_extra_count = 1", text, relative)
                self.assertIn("{9, 8, 7, 6}", text, relative)
                self.assertIn("klee:test1", text, relative)
            # Output buffers stay sentinel-filled: a vector must never pre-seed the thing
            # under comparison.
            self.assertNotIn("c2hlsc_extra_out", (project / "tb" / "testbench.cpp").read_text(encoding="utf-8"))

    def test_no_vectors_leaves_the_generated_code_unchanged(self) -> None:
        config = AgentConfig(directed_tests=["zeros"])
        self.assertNotIn("c2hlsc_extra", render_helpers(config, "test_idx"))

    @unittest.skipUnless(HAVE_BUILD, "g++ and make are required")
    def test_refined_project_still_builds_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, _ = _project(
                Path(tmp),
                VECTOR_ADD,
                "vector_add",
                {
                    "a": ArgumentConfig(direction="input", length=4),
                    "b": ArgumentConfig(direction="input", length=4),
                    "out": ArgumentConfig(direction="output", length=4),
                    "n": ArgumentConfig(range=(0, 4)),
                },
                num_tests=4,
                extra_vectors=[
                    ExtraVector({"a": [9, 8, 7, 6], "b": [1, 1, 1, 1], "n": 4}, origin="klee:test1"),
                    ExtraVector({"a": [0, 0, 0, 0], "b": [0, 0, 0, 0], "n": 0}, origin="klee:test2"),
                ],
            )
            run = subprocess.run(["make", "-C", str(project), "test"], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("all 6 tests passed", run.stdout)
            self.assertIn("+2 refinement vector(s)", run.stdout)

            trace = subprocess.run(["make", "-C", str(project), "leveri-test"], capture_output=True, text=True)
            self.assertEqual(trace.returncode, 0, trace.stdout + trace.stderr)
            rows = (project / "leveri_golden_trace.csv").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(rows), 2 + 6)  # header + roles + 4 scheduled + 2 refinement


class KtestDecodingTests(unittest.TestCase):
    def test_ktest_round_trips_into_a_stimulus_vector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "test000001.ktest"
            _write_ktest(
                path,
                {
                    "a": b"".join(int(v).to_bytes(4, "little", signed=True) for v in (12345, -7, 0, 99)),
                    "n": (3).to_bytes(4, "little", signed=True),
                },
            )
            objects = parse_ktest(path)
            self.assertEqual(set(objects), {"a", "n"})

            source = root / "input.c"
            source.write_text(UNREACHABLE_BRANCH, encoding="utf-8")
            config = AgentConfig(
                top="picky",
                input_files=[source],
                arguments={"a": ArgumentConfig(direction="input", length=4), "n": ArgumentConfig(range=(0, 8))},
            )
            analysis = analyze_source(source, "picky", config)
            vector = ktest_to_vector(objects, analysis, origin="klee:test000001.ktest")
            self.assertEqual(vector.values["a"], [12345, -7, 0, 99])
            self.assertEqual(vector.values["n"], 3)
            self.assertEqual(vector.origin, "klee:test000001.ktest")

    def test_scalar_is_clamped_into_its_declared_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "t.ktest"
            _write_ktest(path, {"n": (9999).to_bytes(4, "little", signed=True)})
            source = root / "input.c"
            source.write_text(UNREACHABLE_BRANCH, encoding="utf-8")
            config = AgentConfig(
                top="picky",
                input_files=[source],
                arguments={"a": ArgumentConfig(direction="input", length=4), "n": ArgumentConfig(range=(0, 8))},
            )
            analysis = analyze_source(source, "picky", config)
            vector = ktest_to_vector(parse_ktest(path), analysis, origin="klee")
            self.assertEqual(vector.values["n"], 8)

    def test_non_ktest_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not.ktest"
            path.write_bytes(b"nope-not-a-ktest")
            with self.assertRaises(ValueError):
                parse_ktest(path)

    def test_gate_coverage_is_the_weaker_of_line_and_branch(self) -> None:
        self.assertEqual(gate_coverage({"line_coverage": 100.0, "branch_coverage": 75.0}), 75.0)
        self.assertEqual(gate_coverage({"line_coverage": 60.0}), 60.0)
        self.assertIsNone(gate_coverage({}))


class RefinementLoopTests(unittest.TestCase):
    def _picky(self, tmp: Path, num_tests: int = 4):
        return _project(
            tmp,
            UNREACHABLE_BRANCH,
            "picky",
            {"a": ArgumentConfig(direction="input", length=8), "n": ArgumentConfig(range=(0, 8))},
            num_tests=num_tests,
        )

    def test_refusing_a_directory_that_is_not_a_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RefinementError):
                refine_project(Path(tmp), mock.Mock(), AgentConfig())

    def test_missing_coverage_tooling_reports_blocked_with_a_remedy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, analysis, config = self._picky(Path(tmp))
            with mock.patch(
                "c2hlsc_agent.coverage_refine.measure_coverage",
                return_value={"status": "skipped", "reason": "CXX or gcov not found"},
            ):
                outcome = refine_project(project, analysis, config)
            self.assertEqual(outcome.status, "blocked")
            self.assertIn("doctor --install", outcome.summary)
            self.assertTrue((project / "coverage_refinement.json").exists())

    @unittest.skipUnless(HAVE_GCOV, "g++, make and gcov are required")
    def test_widening_fallback_reaches_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, analysis, config = self._picky(Path(tmp))
            if shutil.which("klee"):
                self.skipTest("this test covers the no-KLEE fallback")
            outcome = refine_project(project, analysis, config, target=100.0, max_rounds=5)
            self.assertEqual(outcome.status, "met", outcome.summary)
            self.assertLess(outcome.baseline_coverage, 100.0)
            self.assertEqual(outcome.final_coverage, 100.0)
            self.assertTrue(any(item.strategy == "widen" for item in outcome.rounds))
            report = json.loads((project / "coverage_refinement.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "met")

    @unittest.skipUnless(HAVE_GCOV, "g++, make and gcov are required")
    def test_coverage_report_pinpoints_the_unreached_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, _ = self._picky(Path(tmp))
            run = subprocess.run(["make", "-C", str(project), "gcov-coverage"], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads((project / "coverage" / "gcov_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertLess(report["branch_coverage"], 100.0)
            self.assertTrue(report["uncovered_branches"])
            self.assertIn("input.c", report["measured_files"])
            self.assertIn("src/hls_top.cpp", report["measured_files"])

    @unittest.skipUnless(HAVE_GCOV, "g++, make and gcov are required")
    def test_coverage_target_gates_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, _ = self._picky(Path(tmp))
            run = subprocess.run(
                ["make", "-C", str(project), "gcov-coverage"],
                capture_output=True,
                text=True,
                env={**os.environ, "C2HLSC_MIN_COVERAGE": "99"},
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("below C2HLSC_MIN_COVERAGE", run.stdout)

    def test_regeneration_preserves_the_design_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, analysis, config = self._picky(Path(tmp))
            source = project / "src" / "hls_top.cpp"
            marker = "// a repair the verifier already accepted\n"
            source.write_text(marker + source.read_text(encoding="utf-8"), encoding="utf-8")
            config.extra_vectors = [ExtraVector({"a": [1] * 8, "n": 3}, origin="klee:t")]
            regenerate(project, analysis, config)
            self.assertIn(marker, source.read_text(encoding="utf-8"))
            self.assertIn("c2hlsc_extra_count = 1", (project / "tb" / "testbench.cpp").read_text(encoding="utf-8"))


@unittest.skipUnless(HAVE_KLEE, "a native klee and clang++ are required")
class SymbolicRefinementTests(unittest.TestCase):
    """The KLEE path, against real KLEE.

    A guarded equality branch is the case the random schedule provably cannot reach --
    widening a pseudo-random stream will not produce 12345 -- so this is the test that
    distinguishes a working symbolic loop from one that only looks like it works.
    """

    def _needle(self, tmp: Path):
        return _project(
            tmp,
            NEEDLE,
            "needle",
            {"a": ArgumentConfig(direction="input", length=4), "n": ArgumentConfig(range=(0, 4))},
            num_tests=8,
        )

    def test_klee_reaches_a_branch_the_random_schedule_cannot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, analysis, config = self._needle(Path(tmp))
            outcome = refine_project(project, analysis, config, target=100.0, max_rounds=3, allow_widen=False)

            self.assertEqual(outcome.status, "met", outcome.summary)
            self.assertLess(outcome.baseline_coverage, 100.0)
            self.assertEqual(outcome.final_coverage, 100.0)
            self.assertTrue(all(item.strategy == "klee" for item in outcome.rounds))

            # The magic constant must appear in a decoded vector, not merely somewhere.
            needles = [v for v in outcome.vectors if 12345 in (v.values.get("a") or [])]
            self.assertTrue(needles, "KLEE should have produced an input containing the guard constant")
            self.assertTrue(all(v.origin.startswith("klee:") for v in needles))

    def test_the_decoder_round_trips_real_klee_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, analysis, config = self._needle(Path(tmp))
            report = json.loads(
                subprocess.run(
                    ["make", "-C", str(project), "klee-coverage"], capture_output=True, text=True
                ).stdout
                and (project / "coverage" / "klee_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["mode"], "native")
            self.assertEqual(report["status"], "pass")
            self.assertGreater(report["ktest_count"], 0)

            produced = sorted((project / "coverage" / "klee-out").glob("*.ktest"))
            objects = parse_ktest(produced[0])
            # Object names are the argument names, and sizes follow the declared widths.
            self.assertEqual(set(objects), {"a", "n"})
            self.assertEqual(len(objects["a"]), 4 * 4)
            self.assertEqual(len(objects["n"]), 4)
            vector = ktest_to_vector(objects, analysis, origin="klee:real")
            self.assertEqual(len(vector.values["a"]), 4)
            self.assertLessEqual(vector.values["n"], 4)

    def test_refined_project_still_passes_every_host_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, analysis, config = self._needle(Path(tmp))
            outcome = refine_project(project, analysis, config, target=100.0, max_rounds=3, allow_widen=False)
            self.assertEqual(outcome.status, "met")
            for target in ("test", "leveri-test"):
                run = subprocess.run(["make", "-C", str(project), target], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, f"{target}: {run.stdout}{run.stderr}")


class TraceConsistencyRungTests(unittest.TestCase):
    def test_the_rung_is_required_on_the_host_tier(self) -> None:
        self.assertIn("trace_consistency", PHASE_ORDER)
        self.assertEqual(required_phases(False), ["software_equivalence", "trace_consistency"])
        self.assertEqual(PHASE_ORDER.index("trace_consistency"), 1)

    def test_static_divergence_is_owned_by_the_testbench_agent(self) -> None:
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        state.add_phase(
            PhaseResult(
                "trace_consistency",
                "fail",
                stdout="HLS-LeVeri static control-flow check failed: different CFG shapes",
            )
        )
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.family, "testbench_structural_divergence")
        self.assertEqual(decision.owner_agent, "shift_left_testbench_agent")
        self.assertIn("must not be touched", decision.repair_scope)

    def test_dynamic_divergence_is_owned_by_the_failure_analyst(self) -> None:
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        state.add_phase(
            PhaseResult(
                "trace_consistency",
                "fail",
                stdout="HLS-LeVeri dynamic behaviour check failed: behaviour mismatch cycle=3",
            )
        )
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.family, "trace_behavior_mismatch")
        self.assertEqual(decision.owner_agent, "failure_analyst")

    def test_missing_trace_tooling_is_blocked_not_repaired(self) -> None:
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        state.add_phase(
            PhaseResult("trace_consistency", "fail", summary="python3 not found on PATH; trace tooling unavailable")
        )
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.owner_agent, "cosim_operator")

    def test_log_family_recognizes_both_tiers(self) -> None:
        self.assertEqual(
            classify_log_family("trace_consistency", "HLS-LeVeri static data-dependency check failed"),
            "testbench_structural_divergence",
        )
        self.assertEqual(
            classify_log_family("trace_consistency", "HLS-LeVeri dynamic behaviour check failed"),
            "behavioral_mismatch",
        )


class InterpreterPortabilityTests(unittest.TestCase):
    """The trace rung is required, so it must not assume a python3 on PATH.

    On Windows the interpreter is usually ``python`` (or a Store stub that does nothing),
    and inside a virtualenv ``python3`` may not be the interpreter the project was
    generated with. Either would fail the rung, and therefore every conversion, on a
    machine where nothing is actually wrong.
    """

    def test_makefile_takes_the_interpreter_as_a_variable(self) -> None:
        from c2hlsc_agent.hls_project import render_makefile

        makefile = render_makefile(AgentConfig())
        self.assertIn("PYTHON ?= python3", makefile)
        for recipe in ("tb/leveri_compare.py", "tb/run_gcov.py", "tb/run_klee.py", "-m c2hlsc_agent refine"):
            line = next(row for row in makefile.splitlines() if recipe in row)
            self.assertIn("$(PYTHON)", line, recipe)
            self.assertNotIn("python3 ", line, recipe)

    def test_the_rung_hands_make_its_own_interpreter(self) -> None:
        from c2hlsc_agent import hls_runner

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "Makefile").write_text("", encoding="utf-8")
            with mock.patch.object(hls_runner, "run_command") as runner:
                runner.return_value = PhaseResult("trace_consistency", "pass")
                hls_runner.run_trace_consistency(project)
            command = runner.call_args[0][0]
            self.assertEqual(command[:2], ["make", "leveri-test"])
            self.assertEqual(command[2], f"PYTHON={sys.executable}")

    @unittest.skipUnless(HAVE_BUILD, "g++ and make are required")
    def test_the_rung_passes_with_no_python3_on_path(self) -> None:
        import sys as _sys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, _, _ = _project(
                root,
                VECTOR_ADD,
                "vector_add",
                {
                    "a": ArgumentConfig(direction="input", length=4),
                    "b": ArgumentConfig(direction="input", length=4),
                    "out": ArgumentConfig(direction="output", length=4),
                    "n": ArgumentConfig(range=(0, 4)),
                },
                num_tests=4,
            )
            stripped = root / "bin"
            stripped.mkdir()
            for tool in ("make", "g++", "as", "ld", "sh", "rm", "mkdir"):
                found = shutil.which(tool)
                if found:
                    (stripped / tool).symlink_to(found)
            if shutil.which("make", path=str(stripped)) is None:
                self.skipTest("could not build a PATH containing make")
            with mock.patch.dict(os.environ, {"PATH": str(stripped)}):
                self.assertIsNone(shutil.which("python3"))
                from c2hlsc_agent.hls_runner import run_trace_consistency

                result = run_trace_consistency(project)
            self.assertEqual(result.status, "pass", result.stdout + result.stderr + (result.summary or ""))


class ToolchainTests(unittest.TestCase):
    def test_every_tool_is_placed_in_a_known_tier(self) -> None:
        for tool in toolchain.TOOLS:
            self.assertIn(tool.tier, toolchain.TIERS, tool.name)
            self.assertTrue(tool.purpose)

    def test_a_tool_without_a_package_is_never_reported_installable(self) -> None:
        statuses = {status.tool.name: status for status in toolchain.check(["vendor"])}
        vitis = statuses["vitis_hls"]
        if not vitis.present:
            self.assertFalse(vitis.installable)
            self.assertIn("licensed", vitis.tool.manual)

    def test_environment_override_is_honoured_before_path(self) -> None:
        tool = next(item for item in toolchain.TOOLS if item.name == "vitis_hls")
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "vitis_hls"
            fake.write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"VITIS_HLS_BIN": str(fake)}):
                path, source = toolchain.locate(tool)
            self.assertEqual(path, str(fake))
            self.assertEqual(source, "$VITIS_HLS_BIN")

    def test_dry_run_reports_commands_without_running_them(self) -> None:
        absent = toolchain.ToolStatus(
            tool=next(item for item in toolchain.TOOLS if item.name == "yosys"),
            path=None,
            installable=True,
            command=["sudo", "apt-get", "install", "-y", "yosys"],
        )
        with mock.patch("c2hlsc_agent.toolchain.subprocess.run") as runner:
            results = toolchain.install([absent], dry_run=True)
        runner.assert_not_called()
        self.assertEqual(results[0]["status"], "would_run")

    def test_a_missing_brew_formula_is_not_offered(self) -> None:
        with mock.patch("c2hlsc_agent.toolchain.platform.system", return_value="Darwin"), mock.patch(
            "c2hlsc_agent.toolchain.shutil.which", side_effect=lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None
        ), mock.patch("c2hlsc_agent.toolchain._brew_has_formula", return_value=False):
            statuses = toolchain.check(["ppa"])
        for status in statuses:
            self.assertFalse(status.installable, status.tool.name)

    def test_install_reprobes_instead_of_trusting_the_exit_code(self) -> None:
        tool = next(item for item in toolchain.TOOLS if item.name == "yosys")
        absent = toolchain.ToolStatus(tool=tool, path=None, installable=True, command=["true"])
        completed = subprocess.CompletedProcess(args=["true"], returncode=0, stdout="", stderr="")
        with mock.patch("c2hlsc_agent.toolchain.subprocess.run", return_value=completed), mock.patch(
            "c2hlsc_agent.toolchain.locate", return_value=(None, "PATH")
        ):
            results = toolchain.install([absent])
        self.assertEqual(results[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
