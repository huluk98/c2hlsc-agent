from __future__ import annotations

import argparse
import tempfile
import unittest
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
