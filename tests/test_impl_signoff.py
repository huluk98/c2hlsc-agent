"""Post-place-and-route sign-off: Tcl emission, report parsing, and the phase seam.

The parser is the part that can be verified without any Xilinx toolchain, so it is
tested against a realistic ``export_impl.rpt`` body. The runner tests pin the two
properties that matter architecturally: ``impl`` is reachable through the existing
Vitis seam, and it is NOT part of the acceptance ladder.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent import hls_runner
from c2hlsc_agent.config import AgentConfig
from c2hlsc_agent.equivalence import PhaseResult
from c2hlsc_agent.hls_project import render_run_impl
from c2hlsc_agent.hls_runner import PHASE_ORDER, PHASE_TIMEOUTS, run_impl
from c2hlsc_agent.qor import find_impl_report, parse_impl_report

# Shape of the report Vitis HLS writes after `export_design -flow impl` drives Vivado
# through synthesis and place & route.
EXPORT_IMPL_RPT = """================================================================
== Vivado HLS Report for 'vector_add'
================================================================
* Date: Wed Aug  5 09:00:00 2026

== Implementation Tool: Xilinx Vivado v.2024.1

+ Result:
    - SLICE:         41
    - LUT:          138
    - FF:           156
    - DSP:            0
    - BRAM:           0
    - SRL:            2
    - URAM:           0
    - CP required:    10.000
    - CP achieved post-implementation:    3.276

+ Timing:
    * Summary: achieved post-implementation timing is reported above.
"""


def _write_report(project_dir: Path, body: str, name: str = "export_impl.rpt") -> Path:
    report_dir = project_dir / "c2hlsc_project" / "solution1" / "impl" / "report" / "verilog"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / name
    path.write_text(body, encoding="utf-8")
    return path


class ImplTclTests(unittest.TestCase):
    def test_renderer_emits_the_impl_flow(self):
        tcl = render_run_impl(AgentConfig(rtl="verilog"))
        self.assertIn("export_design -flow impl -rtl verilog", tcl)
        self.assertIn("open_project c2hlsc_project", tcl)

    def test_renderer_honours_the_configured_rtl_language(self):
        self.assertIn("-rtl vhdl", render_run_impl(AgentConfig(rtl="vhdl")))

    def test_renderer_does_not_reset_the_project(self):
        # -reset would discard the synthesis the impl flow is supposed to implement.
        self.assertNotIn("-reset", render_run_impl(AgentConfig(rtl="verilog")))


class ImplReportParsingTests(unittest.TestCase):
    def test_post_route_resources_and_achieved_period_are_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_report(Path(tmp), EXPORT_IMPL_RPT)
            metrics = parse_impl_report(path)
        self.assertEqual(metrics.impl["lut"], 138)
        self.assertEqual(metrics.impl["ff"], 156)
        self.assertEqual(metrics.impl["slice"], 41)
        self.assertEqual(metrics.impl["srl"], 2)
        self.assertEqual(metrics.impl["dsp"], 0)
        self.assertAlmostEqual(metrics.impl["cp_achieved_ns"], 3.276)
        self.assertAlmostEqual(metrics.impl["cp_required_ns"], 10.0)

    def test_impl_numbers_survive_serialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_report(Path(tmp), EXPORT_IMPL_RPT)
            payload = parse_impl_report(path).to_dict()
        self.assertEqual(payload["impl"]["lut"], 138)

    def test_impl_stays_out_of_the_candidate_scoring_signals(self):
        # P&R numbers cost a Vivado run to obtain; letting them into area_proxy would
        # drag the optimizer's candidate search into place-and-route.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_report(Path(tmp), EXPORT_IMPL_RPT)
            metrics = parse_impl_report(path)
        self.assertIsNone(metrics.area_proxy)

    def test_a_report_without_results_raises_instead_of_signing_off_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_report(Path(tmp), "== Vivado HLS Report ==\ntruncated\n")
            with self.assertRaises(RuntimeError):
                parse_impl_report(path)

    def test_find_prefers_the_canonical_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_report(project, "noise\n", name="aaa_other.rpt")
            canonical = _write_report(project, EXPORT_IMPL_RPT)
            self.assertEqual(find_impl_report(project), canonical)

    def test_find_falls_back_when_the_name_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            renamed = _write_report(project, EXPORT_IMPL_RPT, name="vector_add_export.rpt")
            self.assertEqual(find_impl_report(project), renamed)

    def test_find_returns_none_when_impl_never_ran(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_impl_report(Path(tmp)))


class ImplPhaseWiringTests(unittest.TestCase):
    def test_impl_is_not_part_of_the_acceptance_ladder(self):
        # The whole point of the separation: acceptance must never wait on P&R, and the
        # QoR optimizer must never pay for it per candidate.
        self.assertNotIn("impl", PHASE_ORDER)

    def test_impl_has_a_timeout_budget_larger_than_csynth(self):
        self.assertGreater(PHASE_TIMEOUTS["impl"], PHASE_TIMEOUTS["csynth"])

    def test_missing_local_launcher_fails_without_running_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(hls_runner, "find_vitis_executable", return_value=None):
                result = run_impl(Path(tmp), remote=None, vitis_bin="vitis_hls")
        self.assertEqual(result.status, "fail")
        self.assertIn("not found", result.summary)

    def test_remote_artifacts_are_pulled_before_the_report_is_read(self):
        # export_design writes its report on the remote; without a pull the parser would
        # read an absent or stale local file and sign off on the wrong numbers.
        calls = []

        class FakeRemote:
            host = "user@box"

            def push(self, project_dir):
                calls.append("push")
                return PhaseResult("vitis_push", "pass")

            def run_phase(self, project_dir, phase, timeout):
                calls.append(f"run:{phase}")
                return PhaseResult(phase, "pass")

            def pull(self, project_dir):
                calls.append("pull")
                return PhaseResult("vitis_pull", "pass")

        with tempfile.TemporaryDirectory() as tmp:
            result = run_impl(Path(tmp), remote=FakeRemote(), vitis_bin="vitis_hls")
        self.assertEqual(result.status, "pass")
        self.assertEqual(calls, ["push", "run:impl", "pull"])

    def test_a_failed_remote_push_does_not_run_impl(self):
        class FailingRemote:
            host = "user@box"

            def push(self, project_dir):
                return PhaseResult("vitis_push", "fail", summary="no route to host")

            def run_phase(self, project_dir, phase, timeout):  # pragma: no cover
                raise AssertionError("impl must not run when the project never arrived")

            def pull(self, project_dir):  # pragma: no cover
                raise AssertionError("nothing to pull")

        with tempfile.TemporaryDirectory() as tmp:
            result = run_impl(Path(tmp), remote=FailingRemote(), vitis_bin="vitis_hls")
        self.assertEqual(result.status, "fail")
        self.assertIn("remote vitis unavailable", result.summary)


if __name__ == "__main__":
    unittest.main()
