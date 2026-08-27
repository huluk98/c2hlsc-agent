from __future__ import annotations

import argparse
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.cli import _external_failure_state
from c2hlsc_agent.config import AgentConfig, load_config, merge_cli_config
from c2hlsc_agent.equivalence import PhaseResult
from c2hlsc_agent.hls_runner import _gate_cosim_on_log


class CosimLogGateTests(unittest.TestCase):
    def test_pass_with_failure_marker_is_downgraded(self):
        # Vitis can exit 0 while the CoSim log reports a mismatch.
        result = PhaseResult(
            "cosim", "pass", returncode=0, stdout="C/RTL co-simulation finished: FAIL\n"
        )
        self.assertEqual(_gate_cosim_on_log(result).status, "fail")

    def test_clean_pass_stays_pass(self):
        result = PhaseResult(
            "cosim", "pass", returncode=0, stdout="C/RTL co-simulation finished: PASS\n"
        )
        self.assertEqual(_gate_cosim_on_log(result).status, "pass")

    def test_non_pass_is_untouched(self):
        result = PhaseResult("cosim", "fail", returncode=1, stdout="boom")
        self.assertEqual(_gate_cosim_on_log(result).status, "fail")


class ExternalFailureStateTests(unittest.TestCase):
    def test_stage_not_in_active_phases_is_still_recorded(self):
        # Defensive: a stage outside the active phase list must not be dropped.
        state = _external_failure_state("csim", "log evidence", run_vitis=False)
        self.assertEqual(state.status_for("software_equivalence"), "pass")
        self.assertEqual(state.status_for("csim"), "fail")


class ConfigMergeTests(unittest.TestCase):
    def _args(self, **overrides):
        base = dict(
            keep_going=False,
            auto_repair=False,
            run_vitis=False,
            no_run_vitis=False,
            use_llm=False,
            no_llm=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_config_keep_going_not_clobbered_by_absent_flag(self):
        merged = merge_cli_config(AgentConfig(keep_going=True), self._args())
        self.assertTrue(merged.keep_going)

    def test_cli_keep_going_sets_true(self):
        merged = merge_cli_config(AgentConfig(keep_going=False), self._args(keep_going=True))
        self.assertTrue(merged.keep_going)

    def test_load_config_reads_loop_knobs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text(
            '{"input_files": ["input.c"], "top": "k", '
            '"max_iterations": 5, "auto_repair": true, "keep_going": true}',
            encoding="utf-8",
        )
        config = load_config(path)
        self.assertEqual(config.max_iterations, 5)
        self.assertTrue(config.auto_repair)
        self.assertTrue(config.keep_going)


if __name__ == "__main__":
    unittest.main()


class WindowsLauncherTests(unittest.TestCase):
    """W1: on Windows the launcher is vitis_hls.bat. shutil.which finds it through
    PATHEXT, but CreateProcess -- which subprocess uses -- appends only .exe, so the
    bare name fails to start on the very machine where Vitis IS installed."""

    def test_batch_launcher_goes_through_cmd_on_windows(self):
        from c2hlsc_agent.hls_runner import hls_launch_argv

        argv = hls_launch_argv(r"C:\Xilinx\Vitis_HLS\2024.2\bin\vitis_hls.bat",
                               "run_csim.tcl", windows=True)
        self.assertEqual(argv[:2], ["cmd", "/c"])
        self.assertEqual(argv[-2:], ["-f", "run_csim.tcl"])

    def test_exe_and_posix_launchers_are_invoked_directly(self):
        from c2hlsc_agent.hls_runner import hls_launch_argv

        self.assertEqual(
            hls_launch_argv("/tools/Xilinx/bin/vitis_hls", "run_csim.tcl", windows=False),
            ["/tools/Xilinx/bin/vitis_hls", "-f", "run_csim.tcl"],
        )
        self.assertEqual(
            hls_launch_argv("vitis_hls.exe", "run_csim.tcl", windows=True),
            ["vitis_hls.exe", "-f", "run_csim.tcl"],
        )

    def test_explicit_absolute_binary_is_honoured_and_checked(self):
        from c2hlsc_agent.hls_runner import resolve_hls_bin

        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "vitis_hls.bat"
            real.write_text("@echo off\n", encoding="utf-8")
            self.assertEqual(resolve_hls_bin(str(real)), str(real))
            self.assertIsNone(resolve_hls_bin(str(Path(tmp) / "absent.bat")))

    def test_unlaunchable_binary_is_reported_as_toolchain_unavailable(self):
        # The failure must reach classify_log_family's toolchain_unavailable family, so
        # the run is blocked and the repair agent never mutates correct source over it.
        from c2hlsc_agent.agent_loop import classify_log_family
        from c2hlsc_agent.hls_runner import _run_vitis_phase

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("c2hlsc_agent.hls_runner.run_command",
                            side_effect=FileNotFoundError("[WinError 2] cannot find the file")):
                result = _run_vitis_phase(Path(tmp), "csim", None)
        self.assertEqual(result.status, "fail")
        self.assertIn("vitis_hls not found", result.summary)
        self.assertEqual(classify_log_family("csim", result.summary), "toolchain_unavailable")


class VisibilityAndPortabilityTests(unittest.TestCase):
    """The batch that makes silent behaviour visible, plus the generated-project
    portability fix. None of these change what passes or fails a verification."""

    def _analysis(self, source: str, top: str, arguments: dict | None = None):
        from c2hlsc_agent.analyze import analyze_source
        from c2hlsc_agent.config import AgentConfig, ArgumentConfig

        cfg = AgentConfig(top=top, arguments=arguments or {})
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.c"
            src.write_text(source, encoding="utf-8")
            return analyze_source(src, top, cfg)

    def test_clamped_output_comparison_is_reported(self):
        # A scalar named like a length, with a declared range, silently narrows the
        # comparison. The report has to say so -- it is inferred from a NAME.
        from c2hlsc_agent.config import ArgumentConfig
        from c2hlsc_agent.testgen import active_length_map

        analysis = self._analysis(
            "void thresh(const int *in, int *out, int n) {\n"
            "  for (int i = 0; i < 16; i++) out[i] = (in[i] > n) ? 1 : 0;\n}\n",
            "thresh",
            {
                "in": ArgumentConfig(direction="input", length=16),
                "out": ArgumentConfig(direction="output", length=16),
                "n": ArgumentConfig(range=(0, 16)),
            },
        )
        self.assertEqual(active_length_map(analysis), {"in": "n", "out": "n"})

    def test_unclamped_design_reports_nothing(self):
        from c2hlsc_agent.config import ArgumentConfig
        from c2hlsc_agent.testgen import active_length_map

        analysis = self._analysis(
            "void dbl(const int in[8], int out[8]) {\n"
            "  for (int i = 0; i < 8; i++) out[i] = in[i] * 2;\n}\n",
            "dbl",
            {"in": ArgumentConfig(direction="input"), "out": ArgumentConfig(direction="output")},
        )
        self.assertEqual(active_length_map(analysis), {})

    def test_interface_pragma_ledger_reads_the_generated_source(self):
        # Previously the ledger was always empty for a model-written unit, so a changed
        # hardware contract left no trace in any artifact of the run.
        from c2hlsc_agent.convert import interface_pragmas_in

        source = (
            '#include "hls_top.hpp"\n'
            "void f(const int *a, int *out, int n) {\n"
            "#pragma HLS INTERFACE mode=m_axi port=a offset=slave bundle=gmem\n"
            "#pragma HLS INTERFACE mode=m_axi port=out offset=slave bundle=gmem\n"
            "#pragma HLS INTERFACE mode=s_axilite port=n\n"
            "#pragma HLS INTERFACE mode=s_axilite port=return\n"
            "}\n"
        )
        rows = interface_pragmas_in(source, "ap_memory", array_args={"a", "out"})
        self.assertEqual([r["argument"] for r in rows], ["a", "out", "n", "return"])
        # Array ports genuinely changed mode: flag them.
        self.assertIn("DIFFERS", rows[0]["reason"])
        self.assertIn("DIFFERS", rows[1]["reason"])
        # s_axilite on a scalar and on the return is the control interface under every
        # mode, including ap_memory. Flagging it would make the signal worthless.
        self.assertNotIn("DIFFERS", rows[2]["reason"])
        self.assertNotIn("DIFFERS", rows[3]["reason"])

    def test_blocked_classification_closes_the_run_blocked(self):
        from c2hlsc_agent.cli import _blocked_reason
        from c2hlsc_agent.config import AgentConfig
        from c2hlsc_agent.equivalence import PhaseResult, VerificationState

        analysis = self._analysis("int f(int a) { return a; }\n", "f")
        config = AgentConfig(top="f", run_vitis=True)

        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        state.add_phase(PhaseResult("csim", "fail", summary="vitis_hls not found on PATH"))
        reason = _blocked_reason(state, config, analysis)
        self.assertIsNotNone(reason)
        self.assertIn("toolchain_unavailable", reason)

        # A genuine behavioural failure must still close the run FAILED, not blocked.
        state2 = VerificationState()
        state2.add_phase(PhaseResult("software_equivalence", "fail", stdout="Mismatch test=0 ..."))
        self.assertIsNone(_blocked_reason(state2, config, analysis))

    def test_cosim_gate_also_reads_the_pulled_sim_report(self):
        # The gate used to run BEFORE the remote artifact pull, so the console transcript
        # was its only witness. Vitis's own report is a second, independent one.
        from c2hlsc_agent.equivalence import PhaseResult
        from c2hlsc_agent.hls_runner import _gate_cosim_on_log

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            report = project / "c2hlsc_project" / "solution1" / "sim" / "report"
            report.mkdir(parents=True)
            (report / "cosim.log").write_text(
                "*** C/RTL co-simulation finished: FAIL ***\n", encoding="utf-8"
            )
            clean_pass = PhaseResult("cosim", "pass", 0, stdout="all good", stderr="")
            gated = _gate_cosim_on_log(clean_pass, project)
        self.assertEqual(gated.status, "fail")
        self.assertIn("co-simulation failure", gated.summary)

    def test_header_only_change_invalidates_the_synthesis_report(self):
        # _repair_missing_standard_includes writes hls_top.hpp and nothing else.
        import os
        import time
        from c2hlsc_agent.qor_optimizer import _report_is_fresh

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "src").mkdir()
            (project / "tb").mkdir()
            (project / "src" / "hls_top.cpp").write_text("x", encoding="utf-8")
            (project / "tb" / "testbench.cpp").write_text("x", encoding="utf-8")
            (project / "src" / "hls_top.hpp").write_text("x", encoding="utf-8")
            xml = project / "csynth.xml"
            xml.write_text("<x/>", encoding="utf-8")
            self.assertTrue(_report_is_fresh(project, xml))
            # touch only the header, into the future so the filesystem clock cannot tie
            future = time.time() + 60
            os.utime(project / "src" / "hls_top.hpp", (future, future))
            self.assertFalse(_report_is_fresh(project, xml))

    def test_stale_agent_artifacts_removed_on_reconvert_without_flags(self):
        from c2hlsc_agent.cli import build_parser, run_convert

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "in.c").write_text("int f(int a) { return a + 1; }\n", encoding="utf-8")
            out = tmp / "out"
            argv = ["convert", "--input", str(tmp / "in.c"), "--top", "f", "--out", str(out)]
            parser = build_parser()
            self.assertEqual(run_convert(parser.parse_args(argv + ["--run-id", "r1"])), 0)
            # simulate a previous agent-flag run's leftovers
            (out / "contract_proposals.json").write_text("{}", encoding="utf-8")
            (out / "tb" / "augmented_vectors.json").write_text("{}", encoding="utf-8")
            self.assertEqual(run_convert(parser.parse_args(argv + ["--run-id", "r2"])), 0)
            self.assertFalse((out / "contract_proposals.json").exists())
            self.assertFalse((out / "tb" / "augmented_vectors.json").exists())

    def test_generated_scripts_use_the_running_interpreter(self):
        # "python3" is not a command on Windows; sys.executable always is.
        from c2hlsc_agent.leveri_testgen import generate_leveri_testbenches
        from c2hlsc_agent.verilog_testgen import generate_verilog_testbenches
        from c2hlsc_agent.config import AgentConfig

        analysis = self._analysis("int f(int a) { return a; }\n", "f")
        config = AgentConfig(top="f")
        gcov = generate_leveri_testbenches(analysis, config).gcov_script
        rtl = generate_verilog_testbenches(analysis, config).run_script
        for name, script in (("run_gcov.py", gcov), ("run_rtl_sim.py", rtl)):
            with self.subTest(script=name):
                self.assertNotIn('"python3"', script)
                self.assertIn("sys.executable", script)
                self.assertIn("import sys", script)
