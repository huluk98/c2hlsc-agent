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
from c2hlsc_agent.config import (
    STIMULUS_CONTRACT_PATH,
    AgentConfig,
    ArgumentConfig,
    apply_stimulus_contract,
    read_stimulus_contract,
)
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


sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ importable however this is invoked

from support import (  # noqa: E402 - tests/ is added to sys.path above
    BUILD_REASON,
    GCOV_REASON,
    HAVE_BUILD,
    HAVE_GCOV,
    HAVE_KLEE,
    HAVE_MAKE,
    HAVE_SYMLINKS,
    KLEE_REASON,
    run_make,
    run_target,
)

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

    @unittest.skipUnless(HAVE_BUILD, BUILD_REASON)
    def test_one_sided_harness_change_fails_the_run(self) -> None:
        clean = run_target(self.project, "leveri-test")
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)

        hls_tb = self.project / "tb" / "leveri_hls_tb.cpp"
        hls_tb.write_text(
            hls_tb.read_text(encoding="utf-8").replace(
                "  std::mt19937_64 rng = make_trace_rng();",
                "  std::mt19937_64 rng = make_trace_rng();\n  if (trace.good()) { trace.flush(); }",
            ),
            encoding="utf-8",
        )
        broken = run_target(self.project, "leveri-test")
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

    @unittest.skipUnless(HAVE_BUILD, BUILD_REASON)
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
            run = run_target(project, "test")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("all 6 tests passed", run.stdout)
            self.assertIn("+2 refinement vector(s)", run.stdout)

            trace = run_target(project, "leveri-test")
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

    @unittest.skipUnless(HAVE_GCOV, GCOV_REASON)
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

    @unittest.skipUnless(HAVE_GCOV, GCOV_REASON)
    def test_coverage_report_pinpoints_the_unreached_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, _ = self._picky(Path(tmp))
            run = run_target(project, "gcov-coverage")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads((project / "coverage" / "gcov_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertLess(report["branch_coverage"], 100.0)
            self.assertTrue(report["uncovered_branches"])
            self.assertIn("input.c", report["measured_files"])
            self.assertIn("src/hls_top.cpp", report["measured_files"])

    @unittest.skipUnless(HAVE_GCOV, GCOV_REASON)
    def test_coverage_target_gates_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, _ = self._picky(Path(tmp))
            run = run_target(project, "gcov-coverage", env={**os.environ, "C2HLSC_MIN_COVERAGE": "99"})
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


@unittest.skipUnless(HAVE_KLEE, KLEE_REASON)
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
                run_target(project, "klee-coverage").stdout
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
                run = run_target(project, target)
                self.assertEqual(run.returncode, 0, f"{target}: {run.stdout}{run.stderr}")


class KleeContainerFallbackTests(unittest.TestCase):
    """The container fallback must never turn an absent optional tool into a failure.

    Regression for the windows-latest CI failure: that runner has the docker CLI and a
    daemon that answers, but it runs WINDOWS containers, so `docker run` on the Linux
    klee image exits 125. The fallback fired and reported failure, where the contract is
    to skip cleanly -- KLEE being unavailable is not a build failure.
    """

    def _run_klee_module(self, tmp: Path):
        project, _, _ = _project(
            tmp,
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
        return project, _load_generated_module(project, "tb/run_klee.py", "run_klee_under_test")

    def _docker_info(self, os_type: str):
        def fake(command, *args, **kwargs):
            self.assertEqual(command[:2], ["docker", "info"])
            return subprocess.CompletedProcess(command, 0, stdout=os_type + "\n", stderr="")

        return fake

    def test_a_windows_container_daemon_is_not_usable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, module = self._run_klee_module(Path(tmp))
            with mock.patch.object(module.shutil, "which", return_value="/usr/bin/docker"), mock.patch.object(
                module.subprocess, "run", side_effect=self._docker_info("windows")
            ):
                ok, reason = module.docker_available()
            self.assertFalse(ok)
            self.assertIn("Linux image", reason)

    def test_a_linux_daemon_is_usable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, module = self._run_klee_module(Path(tmp))
            with mock.patch.object(module.shutil, "which", return_value="/usr/bin/docker"), mock.patch.object(
                module.subprocess, "run", side_effect=self._docker_info("linux")
            ):
                ok, reason = module.docker_available()
            self.assertTrue(ok, reason)

    def test_the_automatic_route_refuses_to_pull(self) -> None:
        """An unrequested multi-GB pull is not an acceptable side effect of `make`.

        On CI this turned a 35-second suite into a 206-second one before it was caught.
        """

        with tempfile.TemporaryDirectory() as tmp:
            _, module = self._run_klee_module(Path(tmp))
            calls = []

            def fake(command, *args, **kwargs):
                calls.append(command)
                # image inspect: not present locally
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="No such image")

            with mock.patch.object(module.subprocess, "run", side_effect=fake):
                self.assertFalse(module.image_present("klee/klee:latest"))
            self.assertEqual(calls[0][:3], ["docker", "image", "inspect"])
            self.assertNotIn("pull", " ".join(calls[0]))

    def test_an_automatic_container_failure_skips_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, module = self._run_klee_module(Path(tmp))
            code = module._container_failed("klee/klee:latest", False, "container exited 125", {})
            self.assertEqual(code, 0)
            report = json.loads((project / "coverage" / "klee_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "skipped")
            self.assertIn("doctor", report["remedy"])

    def test_a_forced_container_failure_is_a_real_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, module = self._run_klee_module(Path(tmp))
            code = module._container_failed("klee/klee:latest", True, "container exited 125", {})
            self.assertEqual(code, 1)
            report = json.loads((project / "coverage" / "klee_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")


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

    def test_every_makefile_recipe_delegates_to_the_driver(self) -> None:
        """One definition per recipe, and make stays optional."""

        from c2hlsc_agent.hls_project import render_makefile

        makefile = render_makefile(AgentConfig())
        self.assertIn("PYTHON ?= python3", makefile)
        recipes = [row.strip() for row in makefile.splitlines() if row.startswith("\t")]
        self.assertTrue(recipes)
        for row in recipes:
            # The only recipe that does not go through the driver is the Vitis one, which
            # invokes the vendor tool directly.
            if row.startswith("vitis_hls "):
                continue
            self.assertTrue(row.startswith("$(PYTHON) "), row)
            self.assertNotIn("python3 ", row, row)
        for target in ("test", "leveri-test", "gcov-coverage", "klee-coverage", "clean"):
            self.assertIn(f"tb/host_build.py {target}", makefile, target)

    def test_the_host_rungs_do_not_invoke_make(self) -> None:
        """make is not a native Windows tool, and both host rungs are required."""

        from c2hlsc_agent import hls_runner

        for target, phase, call in (
            ("test", "software_equivalence", hls_runner.run_software_equivalence),
            ("leveri-test", "trace_consistency", hls_runner.run_trace_consistency),
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(hls_runner, "run_command") as runner:
                    runner.return_value = PhaseResult(phase, "pass")
                    call(Path(tmp))
                command = runner.call_args[0][0]
                self.assertEqual(command[0], sys.executable)
                self.assertTrue(command[1].endswith("host_build.py"), command)
                self.assertEqual(command[2], target)
                self.assertNotIn("make", command)

    def test_the_driver_resolves_a_gcc_style_compiler_and_names_msvc(self) -> None:
        from c2hlsc_agent.hls_project import render_host_build

        driver = render_host_build(AgentConfig())
        # MSVC must be reported, never silently used: its flag syntax is incompatible.
        self.assertIn("only MSVC (cl.exe) was found", driver)
        self.assertIn('EXE = ".exe" if os.name == "nt" else ""', driver)
        self.assertIn("sys.executable", driver)

    @unittest.skipUnless(HAVE_BUILD, BUILD_REASON)
    def test_the_host_tier_runs_with_no_make_on_path(self) -> None:
        """The native-Windows scenario: a compiler and Python, no make and no shell."""

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
            self.assertTrue((project / "tb" / "host_build.py").exists())
            self.assertTrue((project / "run_all.py").exists())
            for target in ("test", "leveri-test"):
                run = subprocess.run(
                    [sys.executable, str(project / "tb" / "host_build.py"), target],
                    cwd=project,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(run.returncode, 0, f"{target}: {run.stdout}{run.stderr}")

    @unittest.skipUnless(HAVE_BUILD and HAVE_MAKE, "a C++ compiler and make are required")
    def test_the_makefile_alias_still_forwards_to_the_driver(self) -> None:
        """make is optional, but while it exists it must reach the same recipes."""

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
            )
            run = run_make(project, "test")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("host_build.py", run.stdout)

    def test_run_command_kills_the_process_tree_on_windows(self) -> None:
        from c2hlsc_agent import equivalence

        proc = mock.Mock()
        proc.pid = 4321
        proc.poll.return_value = 0
        with mock.patch.object(equivalence.os, "name", "nt"), mock.patch.object(
            equivalence.subprocess, "run"
        ) as runner:
            equivalence._kill_tree(proc)
        runner.assert_called_once()
        self.assertEqual(runner.call_args[0][0], ["taskkill", "/PID", "4321", "/T", "/F"])

    @unittest.skipUnless(HAVE_BUILD and HAVE_SYMLINKS, "a C++ compiler and symlink support are required")
    def test_the_rung_passes_with_no_python3_on_path(self) -> None:
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

    def test_container_diagnostics_reports_each_precondition_separately(self) -> None:
        """The container route has three independent preconditions that fail identically
        from outside; doctor has to distinguish them or a remote diagnosis is guesswork."""

        def fake(command, *args, **kwargs):
            if command[:2] == ["docker", "info"]:
                return subprocess.CompletedProcess(command, 0, stdout="windows\n", stderr="")
            if command[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="No such image")
            raise AssertionError(f"unexpected probe: {command}")

        with mock.patch.object(toolchain.shutil, "which", return_value="/usr/bin/docker"), mock.patch.object(
            toolchain.subprocess, "run", side_effect=fake
        ):
            diagnostics = toolchain.container_diagnostics()
        self.assertEqual(diagnostics["daemon"], "ok")
        self.assertEqual(diagnostics["os_type"], "windows")
        self.assertIs(diagnostics["image_present"], False)

    def test_container_diagnostics_without_docker(self) -> None:
        with mock.patch.object(toolchain.shutil, "which", return_value=None):
            diagnostics = toolchain.container_diagnostics()
        self.assertIsNone(diagnostics["cli"])
        self.assertEqual(diagnostics["daemon"], "not installed")
        self.assertNotIn("os_type", diagnostics)

    def test_install_reprobes_instead_of_trusting_the_exit_code(self) -> None:
        tool = next(item for item in toolchain.TOOLS if item.name == "yosys")
        absent = toolchain.ToolStatus(tool=tool, path=None, installable=True, command=["true"])
        completed = subprocess.CompletedProcess(args=["true"], returncode=0, stdout="", stderr="")
        with mock.patch("c2hlsc_agent.toolchain.subprocess.run", return_value=completed), mock.patch(
            "c2hlsc_agent.toolchain.locate", return_value=(None, "PATH")
        ):
            results = toolchain.install([absent])
        self.assertEqual(results[0]["status"], "failed")


class StimulusContractTests(unittest.TestCase):
    """A generated project must describe the stimulus it was built with.

    Coverage refinement regenerates the testbenches in place. If the argument metadata is
    not recoverable from the project itself, a scalar declared as a loop bound is redrawn
    unconstrained and the golden testbench reads past the end of its arrays.
    """

    def _guarded(self, tmp: Path):
        return _project(
            tmp,
            UNREACHABLE_BRANCH,
            "picky",
            {"a": ArgumentConfig(direction="input", length=8), "n": ArgumentConfig(range=(0, 8))},
            num_tests=4,
            seed=7,
        )

    def test_write_project_records_the_declared_argument_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, config = self._guarded(Path(tmp))
            contract = json.loads(
                (project / STIMULUS_CONTRACT_PATH).read_text(encoding="utf-8")
            )
            self.assertEqual(contract["top"], "picky")
            self.assertEqual(contract["num_tests"], config.num_tests)
            self.assertEqual(contract["seed"], config.seed)
            self.assertEqual(contract["arguments"]["n"]["range"], [0, 8])
            self.assertEqual(contract["arguments"]["a"]["length"], 8)
            self.assertEqual(contract["arguments"]["a"]["direction"], "input")

    def test_the_contract_round_trips_into_a_bare_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, config = self._guarded(Path(tmp))
            recovered = apply_stimulus_contract(
                AgentConfig(), read_stimulus_contract(project)
            )
            self.assertEqual(recovered.top, config.top)
            self.assertEqual(recovered.num_tests, config.num_tests)
            self.assertEqual(recovered.seed, config.seed)
            self.assertEqual(recovered.interface_mode, config.interface_mode)
            self.assertEqual(recovered.directed_tests, config.directed_tests)
            self.assertEqual(recovered.arguments["n"].range, (0, 8))
            self.assertEqual(recovered.arguments["a"].length, 8)

    def test_regenerating_from_the_contract_keeps_bounded_arguments_bounded(self) -> None:
        """The bug this pins: without the contract, ``n`` was drawn over all of int."""

        with tempfile.TemporaryDirectory() as tmp:
            project, analysis, _ = self._guarded(Path(tmp))
            golden = project / "tb" / "leveri_golden_tb.cpp"
            self.assertIn("n = bounded_scalar<", golden.read_text(encoding="utf-8"))

            # Exactly what `refine --project X` does with no --config: nothing but the
            # project on disk.
            recovered = apply_stimulus_contract(AgentConfig(), read_stimulus_contract(project))
            recovered.input_files = [project / "input.c"]
            regenerate(project, analysis, recovered)

            regenerated = golden.read_text(encoding="utf-8")
            self.assertIn("n = bounded_scalar<", regenerated)
            self.assertNotIn("n = random_value<", regenerated)

    def test_missing_contract_is_reported_rather_than_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_stimulus_contract(Path(tmp)))

    def test_a_corrupt_contract_reads_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "tb").mkdir()
            (project / STIMULUS_CONTRACT_PATH).write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_stimulus_contract(project))


class GoldenSourcePreservationTests(unittest.TestCase):
    def test_regenerating_in_place_does_not_copy_input_c_onto_itself(self) -> None:
        """``refine`` defaults --input to PROJECT/input.c, which used to raise SameFileError."""

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.c"
            path.write_text(UNREACHABLE_BRANCH, encoding="utf-8")
            config = AgentConfig(
                top="picky",
                input_files=[path],
                arguments={"a": ArgumentConfig(direction="input", length=8), "n": ArgumentConfig(range=(0, 8))},
                num_tests=4,
            )
            analysis = analyze_source(path, "picky", config)
            project = Path(tmp) / "project"
            write_project(project, analysis, generate_hls_sources(analysis, config), config)

            golden = project / "input.c"
            before = golden.read_bytes()
            # Re-analyze from the project copy, the way the CLI does.
            in_place = analyze_source(golden, "picky", config)
            regenerate(project, in_place, config)
            self.assertEqual(golden.read_bytes(), before)


class KleeReportDurabilityTests(unittest.TestCase):
    """A KLEE report must be written for every outcome, timeouts included.

    ``TimeoutExpired`` carries undecoded bytes even from a text-mode ``subprocess.run``.
    Those bytes used to reach ``json.dumps`` and raise, so a timed-out KLEE run produced a
    traceback and no report at all -- indistinguishable, downstream, from never running.
    """

    def _run_klee_module(self, tmp: Path):
        project, _, _ = _project(
            tmp,
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
        return project, _load_generated_module(project, "tb/run_klee.py", "run_klee_timeout_under_test")

    def test_as_text_decodes_the_bytes_a_timeout_hands_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, module = self._run_klee_module(Path(tmp))
            self.assertEqual(module.as_text(b"KLEE: done\xff"), "KLEE: done�")
            self.assertEqual(module.as_text("already text"), "already text")
            self.assertEqual(module.as_text(None), "")

    def test_write_report_never_fails_on_an_unexpected_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, module = self._run_klee_module(Path(tmp))
            module.write_report({"status": "fail", "cmd": Path("/usr/bin/klee"), "raw": b"\x00"})
            written = json.loads((project / "coverage" / "klee_report.json").read_text(encoding="utf-8"))
            self.assertEqual(written["status"], "fail")

    def test_a_timed_out_klee_run_reports_fail_rather_than_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, module = self._run_klee_module(Path(tmp))
            include_dir = Path(tmp) / "klee-include"
            include_dir.mkdir()

            def fake_resolve(env_name, *names):
                return {"KLEE": "klee", "KLEE_CXX": "clang++"}.get(env_name)

            class _Completed:
                returncode = 0
                stdout = ""
                stderr = ""

            def fake_run(command, *args, **kwargs):
                if command and str(command[0]).endswith("klee"):
                    raise subprocess.TimeoutExpired(
                        cmd=list(command), timeout=60, output=b"KLEE: partial\xff", stderr=b"boom\xfe"
                    )
                return _Completed()

            env = {"KLEE_INCLUDE_DIR": str(include_dir), "C2HLSC_KLEE_TIMEOUT": "60"}
            with mock.patch.object(module, "resolve_tool", side_effect=fake_resolve):
                with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
                    with mock.patch.dict(module.os.environ, env, clear=False):
                        rc = module.main()

            self.assertEqual(rc, 1)
            report = json.loads((project / "coverage" / "klee_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["mode"], "native")
            self.assertEqual(report["reason"], "timeout")
            self.assertIn("KLEE: partial", report["commands"][-1]["stdout"])
            self.assertIn("boom", report["commands"][-1]["stderr"])


if __name__ == "__main__":
    unittest.main()
