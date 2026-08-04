import json
import os
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
        self.assertIn("relational KLEE driver", LEVERI_TESTBENCH_SYSTEM_PROMPT)
        self.assertIn("live HLS verification knowledge graph", LEVERI_TESTBENCH_SYSTEM_PROMPT)

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
        self.assertIn("klee_golden_hlsc_relational_check", bundle.manifest_json)
        self.assertIn("klee_make_symbolic", bundle.klee_driver)
        self.assertIn("klee_assume(shared_n >= static_cast<int>(0));", bundle.klee_driver)
        self.assertIn('#include "../src/hls_top.hpp"', bundle.klee_driver)
        self.assertIn("#define restrict __restrict__", bundle.klee_driver)
        self.assertIn("vector_add_ref(golden_a, golden_b, golden_out, golden_n)", bundle.klee_driver)
        self.assertIn("vector_add(hlsc_a, hlsc_b, hlsc_out, hlsc_n)", bundle.klee_driver)
        self.assertIn("seed_out", bundle.klee_driver)
        self.assertIn("C2HLSC_RELATIONAL_MISMATCH:out", bundle.klee_driver)
        self.assertIn('"scope": "golden_hlsc_relational"', bundle.manifest_json)
        self.assertIn('"schema": "c2hlsc-klee-report-v1"', bundle.manifest_json)
        self.assertIn("gcov_report.json", bundle.gcov_script)
        self.assertIn("klee_report.json", bundle.klee_script)
        self.assertIn("static_header_alignment", bundle.manifest_json)
        self.assertIn("dynamic_output_consistency", bundle.manifest_json)
        self.assertIn("HLS-LeVeri consistency check passed", bundle.compare_script)

    def test_klee_driver_clones_shared_state_and_checks_all_pointer_poststate(self):
        analysis, cfg = self._analysis()
        driver = generate_leveri_testbenches(analysis, cfg).klee_driver

        for name in ("a", "b", "out"):
            self.assertIn(f"seed_{name}[4]", driver)
            self.assertIn(f"golden_{name}[4]", driver)
            self.assertIn(f"hlsc_{name}[4]", driver)
            self.assertIn(f"golden_{name}[i] = seed_{name}[i];", driver)
            self.assertIn(f"hlsc_{name}[i] = seed_{name}[i];", driver)
            self.assertIn(f"C2HLSC_RELATIONAL_MISMATCH:{name}", driver)
        self.assertIn("shared_n", driver)
        self.assertIn("golden_n = shared_n;", driver)
        self.assertIn("hlsc_n = shared_n;", driver)
        self.assertIn("C2HLSC_INPUT_CONTRACT_MUTATION:golden:a", driver)
        self.assertIn("C2HLSC_INPUT_CONTRACT_MUTATION:hlsc:b", driver)
        self.assertNotIn("C2HLSC_INPUT_CONTRACT_MUTATION:golden:out", driver)
        self.assertLess(
            driver.index("C2HLSC_INPUT_CONTRACT_MUTATION:golden:a"),
            driver.index("vector_add(hlsc_a, hlsc_b, hlsc_out, hlsc_n)"),
        )
        self.assertLess(
            driver.index("C2HLSC_INPUT_CONTRACT_MUTATION:hlsc:a"),
            driver.index("C2HLSC_RELATIONAL_MISMATCH:out"),
        )

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_relational_driver_runtime_rejects_wrong_hlsc(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "input.c"
        source.write_text(
            "int bump(int *state) { state[0] += 1; return state[0]; }\n",
            encoding="utf-8",
        )
        cfg = AgentConfig(
            top="bump",
            arguments={"state": ArgumentConfig(direction="inout", length=1)},
        )
        analysis = analyze_source(source, "bump", cfg)
        project = root / "project"
        write_project(project, analysis, generate_hls_sources(analysis, cfg), cfg)
        include = root / "stub" / "klee"
        include.mkdir(parents=True)
        (include / "klee.h").write_text(
            """
#pragma once
#include <cstdio>
#include <cstdlib>
#include <cstring>
static inline void klee_make_symbolic(void *address, unsigned long size, const char *) {
  std::memset(address, 1, size);
}
static inline void klee_assume(unsigned long condition) {
  if (!condition) std::exit(90);
}
[[noreturn]] static inline void klee_report_error(
    const char *, int, const char *message, const char *) {
  std::fprintf(stderr, "%s\\n", message);
  std::exit(86);
}
""",
            encoding="utf-8",
        )

        def compile_and_run() -> subprocess.CompletedProcess[str]:
            exe = project / "coverage" / "relational_stub"
            exe.parent.mkdir(exist_ok=True)
            compiled = subprocess.run(
                [
                    "g++",
                    "-std=c++17",
                    "-I",
                    str(root / "stub"),
                    "-I",
                    str(project / "src"),
                    str(project / "tb" / "klee_driver.cpp"),
                    str(project / "src" / "hls_top.cpp"),
                    "-o",
                    str(exe),
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            return subprocess.run([str(exe)], text=True, capture_output=True)

        self.assertEqual(compile_and_run().returncode, 0)
        hls_source = project / "src" / "hls_top.cpp"
        hls_source.write_text(
            hls_source.read_text(encoding="utf-8").replace("state[0] += 1", "state[0] += 2"),
            encoding="utf-8",
        )
        mismatch = compile_and_run()
        self.assertEqual(mismatch.returncode, 86)
        self.assertIn("C2HLSC_RELATIONAL_MISMATCH:return", mismatch.stderr)

    def test_klee_manifest_blocks_vacuous_contract(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "input.c"
        source.write_text("void consume(int value) { (void)value; }\n", encoding="utf-8")
        cfg = AgentConfig(top="consume")
        analysis = analyze_source(source, "consume", cfg)
        manifest = json.loads(generate_leveri_testbenches(analysis, cfg).manifest_json)
        klee = manifest["coverage_hooks"]["klee"]

        self.assertEqual(klee["scope"], "golden_hlsc_relational")
        self.assertIn(
            "no return value or pointer post-state is available to compare",
            klee["unsupported_reasons"],
        )

    def test_klee_manifest_blocks_mutable_hidden_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "input.c"
        source.write_text(
            "static int hidden;\nint bump(int value) { hidden += value; return hidden; }\n",
            encoding="utf-8",
        )
        cfg = AgentConfig(top="bump")
        analysis = analyze_source(source, "bump", cfg)
        klee = json.loads(
            generate_leveri_testbenches(analysis, cfg).manifest_json
        )["coverage_hooks"]["klee"]

        self.assertTrue(
            any("mutable file-scope state" in reason for reason in klee["unsupported_reasons"])
        )

    def test_klee_manifest_blocks_hidden_state_added_by_generated_hlsc(self):
        analysis, cfg = self._analysis()
        candidate = generate_hls_sources(analysis, cfg)
        candidate.source = candidate.source.replace(
            '#include "hls_top.hpp"', '#include "hls_top.hpp"\nstatic int hidden_state;'
        )

        manifest = json.loads(
            generate_leveri_testbenches(analysis, cfg, candidate.source).manifest_json
        )
        reasons = manifest["coverage_hooks"]["klee"]["unsupported_reasons"]

        self.assertTrue(any("generated HLS-C" in reason for reason in reasons))

    @unittest.skipUnless(shutil.which("python3"), "python3 is required")
    def test_generated_klee_runner_classifies_clean_and_relational_fake_runs(self):
        analysis, cfg = self._analysis()
        generated = generate_hls_sources(analysis, cfg)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        project = root / "project"
        write_project(project, analysis, generated, cfg)
        tools = root / "tools"
        tools.mkdir()
        include = root / "include"
        include.mkdir()
        compiler = tools / "fake-compiler"
        compiler.write_text(
            "#!/bin/sh\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-o\" ]; then shift; : > \"$1\"; exit 0; fi\n"
            "  shift\n"
            "done\n"
            "exit 1\n",
            encoding="utf-8",
        )
        compiler.chmod(0o755)
        fake_klee = tools / "fake-klee"

        env = os.environ.copy()
        env.update(
            {
                "KLEE": str(fake_klee),
                "KLEE_CXX": str(compiler),
                "KLEE_LLVM_LINK": str(compiler),
                "KLEE_INCLUDE_DIR": str(include),
            }
        )

        def run_fake(
            error: bool, contract_error: bool = False
        ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
            error_lines = (
                'printf "%s\\n" "Error: C2HLSC_RELATIONAL_MISMATCH:out" '
                '> "$out/test000001.c2hlsc_relational.err"\n'
                if error
                else ""
            )
            contract_lines = (
                'printf "%s\\n" "Error: C2HLSC_INPUT_CONTRACT_MUTATION:golden:a" '
                '> "$out/test000002.c2hlsc_contract.err"\n'
                if contract_error
                else ""
            )
            fake_klee.write_text(
                "#!/bin/sh\n"
                "for arg in \"$@\"; do\n"
                "  case \"$arg\" in --output-dir=*) out=${arg#--output-dir=};; esac\n"
                "done\n"
                "mkdir -p \"$out\"\n"
                ': > "$out/test000001.ktest"\n'
                + error_lines
                + contract_lines
                + 'echo "KLEE: done: completed paths = 1"\n',
                encoding="utf-8",
            )
            fake_klee.chmod(0o755)
            completed = subprocess.run(
                ["python3", "tb/run_klee.py"],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
            )
            report = json.loads(
                (project / "coverage" / "klee_report.json").read_text(encoding="utf-8")
            )
            return completed, report

        clean, clean_report = run_fake(False)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertEqual(clean_report["status"], "pass")
        self.assertEqual(clean_report["outcome"], "no_counterexample")
        self.assertEqual(clean_report["completed_paths"], 1)

        mismatch, mismatch_report = run_fake(True)
        self.assertEqual(mismatch.returncode, 1, mismatch.stdout + mismatch.stderr)
        self.assertEqual(mismatch_report["status"], "fail")
        self.assertEqual(mismatch_report["failure_kind"], "relational_counterexample")
        self.assertEqual(
            mismatch_report["counterexample_names"],
            ["C2HLSC_RELATIONAL_MISMATCH:out"],
        )
        mixed, mixed_report = run_fake(True, contract_error=True)
        self.assertEqual(mixed.returncode, 1, mixed.stdout + mixed.stderr)
        self.assertEqual(mixed_report["status"], "fail")
        self.assertEqual(mixed_report["failure_kind"], "relational_counterexample")
        self.assertEqual(mixed_report["top"], "vector_add")
        self.assertEqual(
            set(mixed_report["artifact_sha256"]),
            {
                "input.c",
                "src/hls_top.hpp",
                "src/hls_top.cpp",
                "tb/klee_driver.cpp",
                "tb/leveri_manifest.json",
            },
        )

        hls_source = project / "src" / "hls_top.cpp"
        hls_source.write_text(
            hls_source.read_text(encoding="utf-8").replace(
                "{", "{\n  static int late_hidden_state;", 1
            ),
            encoding="utf-8",
        )
        blocked, blocked_report = run_fake(False)
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertEqual(blocked_report["status"], "blocked")
        self.assertEqual(
            blocked_report["failure_kind"], "current_candidate_hidden_state"
        )

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
        if shutil.which("klee"):
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
