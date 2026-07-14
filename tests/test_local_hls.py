import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent import local_hls
from c2hlsc_agent.agent_loop import classify_failure
from c2hlsc_agent.analyze import FunctionArg
from c2hlsc_agent.config import AgentConfig
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.local_hls import BACKEND_LOG_TAG, LocalHlsCosim, _parse_cosim, _testbench_xml, resolve_cosim_backend


def _vector_add_args():
    return [
        FunctionArg(raw="const int32_t *a", name="a", c_type="const int32_t *", pointer_depth=1, is_const=True, direction="input", length=8),
        FunctionArg(raw="const int32_t *b", name="b", c_type="const int32_t *", pointer_depth=1, is_const=True, direction="input", length=8),
        FunctionArg(raw="int32_t *out", name="out", c_type="int32_t *", pointer_depth=1, direction="output", length=8),
        FunctionArg(raw="int n", name="n", c_type="int", direction="input", scalar_range=(0, 8)),
    ]


class TestbenchXmlTests(unittest.TestCase):
    def test_first_test_drives_full_length_and_zeros_outputs(self):
        xml = _testbench_xml(_vector_add_args(), num_tests=3, seed=7)
        first = xml.splitlines()[2]
        # length-like scalar n must be the full array length on the first vector,
        # never the degenerate 0 that leaves the kernel doing nothing.
        self.assertIn('n="8"', first)
        # output array is present and zero-initialised (Bambu recomputes expected)
        self.assertIn('out="{0,0,0,0,0,0,0,0}"', first)

    def test_emits_one_testbench_per_test(self):
        xml = _testbench_xml(_vector_add_args(), num_tests=4, seed=1)
        self.assertEqual(xml.count("<testbench "), 4)


class ParseCosimTests(unittest.TestCase):
    def test_pass_on_zero_exit_with_summary(self):
        ok, summary = _parse_cosim("...\n  Number of executions     : 5\n", 0)
        self.assertTrue(ok)
        self.assertIn("5 vectors", summary)

    def test_fail_on_nonzero_exit_surfaces_bambu_error(self):
        ok, summary = _parse_cosim("error -> Front-end compiler returns an error\n", 1)
        self.assertFalse(ok)
        self.assertIn("Front-end compiler", summary)


class ResolveBackendTests(unittest.TestCase):
    def test_explicit_choice_wins(self):
        cfg = AgentConfig(cosim_backend="local-hls")
        self.assertEqual(resolve_cosim_backend(cfg, remote=None), "local-hls")

    def test_auto_prefers_remote_then_vitis_then_local_then_none(self):
        cfg = AgentConfig(cosim_backend="auto")
        self.assertEqual(resolve_cosim_backend(cfg, remote=object()), "vitis-ssh")
        with mock.patch.object(local_hls.shutil, "which", return_value="/usr/bin/vitis_hls"):
            self.assertEqual(resolve_cosim_backend(cfg, remote=None), "vitis")
        with mock.patch.object(local_hls.shutil, "which", return_value=None), \
             mock.patch.object(local_hls, "available", return_value=(True, "")):
            self.assertEqual(resolve_cosim_backend(cfg, remote=None), "local-hls")
        with mock.patch.object(local_hls.shutil, "which", return_value=None), \
             mock.patch.object(local_hls, "available", return_value=(False, "no docker")):
            self.assertEqual(resolve_cosim_backend(cfg, remote=None), "none")


class RunLadderTests(unittest.TestCase):
    def _fake_bambu(self, cmd, capture_output, text, timeout):
        # cmd = [bash, bambu.sh, <workdir>, spec.c, --top-fname=X, ...]
        workdir = next(Path(a) for a in cmd if Path(a).is_dir())
        top = next(a.split("=", 1)[1] for a in cmd if a.startswith("--top-fname="))
        (workdir / f"{top}.v").write_text("module vector_add(); endmodule\n")
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout="  Total cycles : 54 cycles\n  Number of executions     : 2\n",
            stderr="",
        )

    def test_run_synthesizes_and_cosims_locally(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "golden.c").write_text("void vector_add(){}\n")
            backend = LocalHlsCosim(
                golden_c=project / "golden.c", top="vector_add",
                function_args=_vector_add_args(), num_tests=2, seed=7,
            )
            with mock.patch.object(local_hls.subprocess, "run", side_effect=self._fake_bambu):
                phases = backend.run(project)

            self.assertEqual(phases["csynth"].status, "pass")
            self.assertEqual(phases["cosim"].status, "pass")
            self.assertEqual(phases["csim"].status, "pass")
            # the synthesized RTL is collected into the project's rtl/ dir
            self.assertTrue((project / "rtl" / "vector_add.v").exists())

    def test_run_reports_failure_on_nonzero_exit(self):
        import tempfile

        def fail(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, 1, stdout="error -> synthesis blew up\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "golden.c").write_text("void vector_add(){}\n")
            backend = LocalHlsCosim(
                golden_c=project / "golden.c", top="vector_add",
                function_args=_vector_add_args(), num_tests=1, seed=7,
            )
            with mock.patch.object(local_hls.subprocess, "run", side_effect=fail):
                phases = backend.run(project)

            # no .v produced -> csynth fails, cosim blocked
            self.assertEqual(phases["csynth"].status, "fail")
            self.assertEqual(phases["cosim"].status, "blocked")


class ClassifyLocalHlsTests(unittest.TestCase):
    def _state_with_cosim_fail(self, summary):
        st = VerificationState()
        st.add_phase(PhaseResult("software_equivalence", "pass"))
        st.add_phase(PhaseResult("csim", "pass"))
        st.add_phase(PhaseResult("csynth", "pass"))
        st.add_phase(PhaseResult("cosim", "fail", summary=summary))
        return st

    def test_local_hls_failure_is_blocked_not_repairable(self):
        # A tagged local-hls cosim failure must classify as blocked so the
        # auto-repair loop never mutates the (host-equivalent) HLS-C.
        st = self._state_with_cosim_fail(f"{BACKEND_LOG_TAG} Bambu C/RTL co-simulation mismatch")
        decision = classify_failure(st, run_vitis_requested=True)
        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.family, "local_hls_backend")

    def test_untagged_cosim_failure_still_routes_to_repair(self):
        # The Vitis path is unaffected: an ordinary cosim mismatch is still repairable.
        st = self._state_with_cosim_fail("cosim mismatch expected=1 actual=2")
        decision = classify_failure(st, run_vitis_requested=True)
        self.assertNotEqual(decision.status, "blocked")


class OptimizeBaselineTests(unittest.TestCase):
    def _run(self, ppa_status):
        from c2hlsc_agent import cli

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "rtl").mkdir()
            (project / "rtl" / "top.v").write_text("module top(); endmodule\n")
            cfg = AgentConfig(top="top", node="nangate45")
            phase = PhaseResult("ppa", ppa_status, summary="area 100 um^2")
            with mock.patch.object(cli, "_ppa_gate_phase", return_value=phase):
                rc = cli._optimize_local_hls_baseline(project, cfg, analysis=None, verbose=False)
            report = json.loads((project / "qor_report.json").read_text())
            return rc, report

    def test_reports_baseline_and_marks_optimization_not_applicable(self):
        rc, report = self._run("pass")
        self.assertEqual(rc, 0)
        self.assertEqual(report["backend"], "local-hls")
        self.assertEqual(report["optimization"], "not_applicable")

    def test_unmet_criterion_exits_nonzero(self):
        rc, _ = self._run("fail")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
