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
