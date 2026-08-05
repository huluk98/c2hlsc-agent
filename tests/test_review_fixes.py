from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent import hls_runner
from c2hlsc_agent.agent_loop import classify_failure
from c2hlsc_agent.cli import _external_failure_state
from c2hlsc_agent.config import COSIM_BACKENDS, AgentConfig, load_config, merge_cli_config
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.hls_project import render_makefile, render_run_all
from c2hlsc_agent.hls_runner import _gate_cosim_on_log, _run_vitis_phase, run_software_equivalence


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

    def test_exit_zero_without_positive_marker_is_downgraded(self):
        result = PhaseResult("cosim", "pass", returncode=0, stdout="Vitis HLS completed")
        gated = _gate_cosim_on_log(result)
        self.assertEqual(gated.status, "fail")
        self.assertIn("no positive", gated.summary)

    def test_non_pass_is_untouched(self):
        result = PhaseResult("cosim", "fail", returncode=1, stdout="boom")
        self.assertEqual(_gate_cosim_on_log(result).status, "fail")

    def test_failure_markers_beat_a_pass_verdict(self):
        for log in (
            "INFO: Aborting co-simulation: RTL simulation failed.\n",
            "ERROR: [COSIM 212-345] exploded\nC/RTL co-simulation finished: PASS\n",
            "ERROR: [SIM 211-100] failed\nC/RTL co-simulation finished: PASS\n",
            "Mismatch test=3 arg=out index=0 expected=1 actual=2 seed=5\n"
            "C/RTL co-simulation finished: PASS\n",
        ):
            with self.subTest(log=log.splitlines()[0]):
                result = PhaseResult("cosim", "pass", returncode=0, stdout=log)
                self.assertEqual(_gate_cosim_on_log(result).status, "fail")


class GeneratedCosimGateTests(unittest.TestCase):
    def test_run_all_uses_native_launcher_and_requires_cosim_pass(self):
        config = AgentConfig(vitis_bin="/opt/AMD/Vitis/bin/vitis-run")
        script = render_run_all(config)
        self.assertIn(
            "/opt/AMD/Vitis/bin/vitis-run --mode hls --tcl run_hls.tcl", script
        )
        self.assertIn("co-simulation finished: pass", script)
        self.assertIn("mismatch test=", script)
        self.assertIn("tee", script)

    def test_make_vitis_target_goes_through_shared_gate(self):
        makefile = render_makefile(AgentConfig(vitis_bin="vitis-run"))
        self.assertIn("vitis:\n\tbash run_all.sh --vitis-only", makefile)
        self.assertNotIn("vitis:\n\tvitis_hls -f run_hls.tcl", makefile)

    def test_generated_gate_rejects_hyphenated_abort_even_with_pass_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_vitis = root / "vitis-run"
            fake_vitis.write_text(
                "#!/usr/bin/env bash\n"
                "echo 'INFO: Aborting co-simulation: RTL simulation failed.'\n"
                "echo 'C/RTL co-simulation finished: PASS'\n",
                encoding="utf-8",
            )
            fake_vitis.chmod(0o755)
            run_all = root / "run_all.sh"
            run_all.write_text(
                render_run_all(AgentConfig(vitis_bin=str(fake_vitis))),
                encoding="utf-8",
            )
            run_all.chmod(0o755)

            result = subprocess.run(
                ["bash", str(run_all), "--vitis-only"],
                cwd=root,
                text=True,
                capture_output=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CoSim gate: FAIL", result.stderr)


class ToolTimeoutRoutingTests(unittest.TestCase):
    def _state(self, phase: str, summary: str) -> VerificationState:
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        order = ("csim", "csynth", "cosim")
        for name in order:
            if name == phase:
                state.add_phase(PhaseResult(name, "fail", summary=summary))
            elif order.index(name) < order.index(phase):
                state.add_phase(PhaseResult(name, "pass"))
            else:
                state.add_phase(PhaseResult(name, "blocked", summary=f"{phase} failed"))
        return state

    def _timeout_summary(self, phase: str) -> str:
        exc = subprocess.TimeoutExpired(["vitis-run"], 1200)
        with tempfile.TemporaryDirectory() as tmp:
            return hls_runner._timeout_result(
                Path(tmp), phase, exc, f"Vitis {phase}"
            ).summary

    def test_csim_and_csynth_timeouts_are_blocked_not_repaired(self):
        for phase in ("csim", "csynth"):
            with self.subTest(phase=phase):
                decision = classify_failure(
                    self._state(phase, self._timeout_summary(phase)), True
                )
                self.assertEqual(decision.status, "blocked")
                self.assertEqual(decision.family, "tool_timeout")
                self.assertEqual(decision.owner_agent, "cosim_operator")

    def test_real_csynth_error_is_still_repairable(self):
        decision = classify_failure(
            self._state(
                "csynth", "ERROR: [SYNCHK 200-11] loop is not synthesizable"
            ),
            True,
        )
        self.assertEqual(decision.owner_agent, "hlsc_repair_agent")
        self.assertNotEqual(decision.status, "blocked")


class HostToolchainMissingTests(unittest.TestCase):
    def test_make_not_found_is_blocked_not_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                hls_runner, "run_command", side_effect=FileNotFoundError()
            ):
                result = run_software_equivalence(Path(tmp))
        state = VerificationState()
        state.add_phase(result)
        decision = classify_failure(state, True)
        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.family, "host_toolchain_unavailable")


class VitisPhaseOsErrorTests(unittest.TestCase):
    def test_missing_local_binary_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                hls_runner, "run_command", side_effect=FileNotFoundError(2, "nope")
            ):
                result = _run_vitis_phase(Path(tmp), "csim", None, "vitis-run")
        self.assertEqual(result.status, "fail")
        self.assertIn("Vitis toolchain unavailable", result.summary)
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        state.add_phase(result)
        self.assertEqual(classify_failure(state, True).status, "blocked")

    def test_remote_os_error_is_reported_as_infrastructure(self):
        remote = mock.Mock(host="u@h")
        remote.run_phase.side_effect = PermissionError(13, "Permission denied")
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_vitis_phase(Path(tmp), "csim", remote, "vitis-run")
        self.assertEqual(result.status, "fail")
        self.assertIn("remote vitis unavailable", result.summary)


class PpaPhaseInReportTests(unittest.TestCase):
    def _render(self, ppa: PhaseResult | None) -> str:
        from c2hlsc_agent.analyze import analyze_source
        from c2hlsc_agent.convert import generate_hls_sources
        from c2hlsc_agent.hls_project import write_project
        from c2hlsc_agent.report import write_reports

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "input.c"
        source.write_text(
            "void scale(int *o, int n){for(int i=0;i<n;i++) o[i]=o[i]*2;}\n",
            encoding="utf-8",
        )
        config = AgentConfig(input_files=[source], top="scale", run_vitis=True)
        analysis = analyze_source(source, "scale", config)
        generated = generate_hls_sources(analysis, config)
        project = write_project(root / "out", analysis, generated, config)
        state = VerificationState()
        for phase in ("software_equivalence", "csim", "csynth", "cosim"):
            state.add_phase(PhaseResult(phase, "pass"))
        if ppa is not None:
            state.add_phase(ppa)
        write_reports(project, analysis, generated, config, state, 1, [])
        return (project.root / "conversion_report.md").read_text(encoding="utf-8")

    def test_failing_ppa_phase_is_rendered(self):
        md = self._render(
            PhaseResult("ppa", "fail", summary="node nangate45: slack -0.4 ns")
        )
        self.assertIn(
            "- PPA workflow criteria: `fail` — node nangate45: slack -0.4 ns", md
        )

    def test_absent_ppa_phase_adds_no_line(self):
        self.assertNotIn("PPA workflow criteria", self._render(None))


class ExternalFailureStateTests(unittest.TestCase):
    def test_stage_not_in_active_phases_is_still_recorded(self):
        # Defensive: a stage outside the active phase list must not be dropped.
        state = _external_failure_state("csim", "log evidence", run_vitis=False)
        self.assertEqual(state.status_for("software_equivalence"), "pass")
        self.assertEqual(state.status_for("csim"), "fail")
        self.assertEqual(
            state.phases["software_equivalence"].metadata["evidence_origin"],
            "operator_assumption",
        )


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


class CosimBackendConfigTests(unittest.TestCase):
    """A typo'd cosim_backend used to survive load_config, match no dispatch branch,
    leave run_vitis false, and still report a pass with the RTL ladder never run."""

    def _load(self, backend: str) -> AgentConfig:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text(
            '{"input_files": ["input.c"], "top": "k", "cosim_backend": "%s"}' % backend,
            encoding="utf-8",
        )
        return load_config(path)

    def test_every_supported_backend_is_accepted(self):
        for backend in COSIM_BACKENDS:
            self.assertEqual(self._load(backend).cosim_backend, backend)

    def test_typo_is_rejected_instead_of_silently_skipping_the_ladder(self):
        with self.assertRaises(ValueError) as caught:
            self._load("vitis-shh")
        self.assertIn("vitis-shh", str(caught.exception))

    def test_default_is_auto_when_the_key_is_absent(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text('{"input_files": ["input.c"], "top": "k"}', encoding="utf-8")
        self.assertEqual(load_config(path).cosim_backend, "auto")


if __name__ == "__main__":
    unittest.main()
