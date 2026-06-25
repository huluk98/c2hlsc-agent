import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.agent_loop import leveri_testbench_policy, multi_agent_procedures
from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.convert import generate_hls_sources
from c2hlsc_agent.hls_project import write_project
from c2hlsc_agent.leveri_testgen import (
    LEVERI_REFERENCE_REPO,
    LEVERI_TESTBENCH_POLICY_ID,
    LEVERI_TESTBENCH_SYSTEM_PROMPT,
    generate_leveri_testbenches,
    get_leveri_testbench_contract,
)


class LeVeriTestgenTests(unittest.TestCase):
    def _analysis(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "input.c"
        path.write_text(
            """
            #include <stdint.h>
            void vector_add(const int32_t *a, const int32_t *b, int32_t *out, int n) {
              for (int i = 0; i < n; ++i) out[i] = a[i] + b[i];
            }
            """,
            encoding="utf-8",
        )
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
        return analyze_source(path, "vector_add", cfg), cfg

    def test_prompt_captures_hls_leveri_testbench_contract(self):
        self.assertIn("shift_left_testbench_agent", LEVERI_TESTBENCH_SYSTEM_PROMPT)
        self.assertIn("HLS-LeVeri shift-left verification style", LEVERI_TESTBENCH_SYSTEM_PROMPT)
        self.assertIn("paired golden-C and HLS-C testbenches", LEVERI_TESTBENCH_SYSTEM_PROMPT)
        self.assertIn("dual-tier consistency checking", LEVERI_TESTBENCH_SYSTEM_PROMPT)
        self.assertIn("gcov", LEVERI_TESTBENCH_SYSTEM_PROMPT)
        self.assertIn("KLEE symbolic driver", LEVERI_TESTBENCH_SYSTEM_PROMPT)
        self.assertIn("future HLS verification knowledge graph", LEVERI_TESTBENCH_SYSTEM_PROMPT)

    def test_contract_declares_testbench_ownership_only(self):
        contract = get_leveri_testbench_contract()
        self.assertEqual(contract.policy_id, LEVERI_TESTBENCH_POLICY_ID)
        self.assertEqual(contract.owner_agent, "shift_left_testbench_agent")
        self.assertEqual(contract.reference_repo, LEVERI_REFERENCE_REPO)
        self.assertFalse(contract.owns_hlsc_generation)

    def test_agent_loop_exposes_leveri_policy(self):
        policy = leveri_testbench_policy()
        self.assertEqual(policy["policy_id"], LEVERI_TESTBENCH_POLICY_ID)
        self.assertEqual(policy["owner_agent"], "shift_left_testbench_agent")

        testbench_agent = [p for p in multi_agent_procedures() if p.name == "shift_left_testbench_agent"][0]
        self.assertIn(LEVERI_TESTBENCH_POLICY_ID, testbench_agent.owns)
        self.assertIn("paired golden/HLS trace testbenches", testbench_agent.outputs)

    def test_generates_paired_trace_testbenches_and_manifest(self):
        analysis, cfg = self._analysis()
        bundle = generate_leveri_testbenches(analysis, cfg)
        self.assertIn("leveri_golden_trace.csv", bundle.golden_tb)
        self.assertIn("vector_add_ref", bundle.golden_tb)
        self.assertIn("leveri_hls_trace.csv", bundle.hls_tb)
        self.assertIn("vector_add(a, b, out, n)", bundle.hls_tb)
        self.assertIn("gcov_concrete_coverage", bundle.manifest_json)
        self.assertIn("klee_symbolic_path_exploration", bundle.manifest_json)
        self.assertIn("klee_make_symbolic", bundle.klee_driver)
        self.assertIn("klee_assume(n >= static_cast<int>(0));", bundle.klee_driver)
        self.assertIn("gcov_report.json", bundle.gcov_script)
        self.assertIn("klee_report.json", bundle.klee_script)
        self.assertIn("static_header_alignment", bundle.manifest_json)
        self.assertIn("dynamic_output_consistency", bundle.manifest_json)
        self.assertIn("HLS-LeVeri consistency check passed", bundle.compare_script)

    @unittest.skipUnless(shutil.which("g++") and shutil.which("make") and shutil.which("python3"), "g++, make, and python3 are required")
    def test_project_leveri_trace_check_passes(self):
        analysis, cfg = self._analysis()
        generated = generate_hls_sources(analysis, cfg)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name) / "project"
        write_project(project, analysis, generated, cfg)

        run = subprocess.run(["make", "-C", str(project), "leveri-test"], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("HLS-LeVeri consistency check passed", run.stdout)

    @unittest.skipUnless(shutil.which("g++") and shutil.which("gcov") and shutil.which("make") and shutil.which("python3"), "g++, gcov, make, and python3 are required")
    def test_project_gcov_coverage_target_writes_report(self):
        analysis, cfg = self._analysis()
        generated = generate_hls_sources(analysis, cfg)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name) / "project"
        write_project(project, analysis, generated, cfg)

        run = subprocess.run(["make", "-C", str(project), "gcov-coverage"], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = project / "coverage" / "gcov_report.json"
        self.assertTrue(report.exists())
        self.assertIn('"status": "pass"', report.read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("make") and shutil.which("python3"), "make and python3 are required")
    def test_project_klee_target_skips_cleanly_when_klee_missing(self):
        if shutil.which("klee") or Path("/Users/luke/.local/klee/bin/klee").exists():
            self.skipTest("this test only checks the portable no-KLEE fallback")
        analysis, cfg = self._analysis()
        generated = generate_hls_sources(analysis, cfg)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name) / "project"
        write_project(project, analysis, generated, cfg)

        run = subprocess.run(["make", "-C", str(project), "klee-coverage"], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = project / "coverage" / "klee_report.json"
        self.assertTrue(report.exists())
        self.assertIn('"status": "skipped"', report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
