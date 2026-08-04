from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from c2hlsc_agent.vitis_command import find_vitis_executable, vitis_tcl_command
from c2hlsc_agent.vitis_evidence import VitisEvidenceError, validate_vitis_project


CSYNTH_XML = """<?xml version="1.0"?>
<profile>
  <PerformanceEstimates>
    <SummaryOfTimingAnalysis>
      <TargetClockPeriod>10.0</TargetClockPeriod>
      <EstimatedClockPeriod>7.5</EstimatedClockPeriod>
    </SummaryOfTimingAnalysis>
    <SummaryOfOverallLatency>
      <Best-caseLatency>16</Best-caseLatency>
      <Worst-caseLatency>16</Worst-caseLatency>
      <Interval-min>17</Interval-min>
      <Interval-max>17</Interval-max>
    </SummaryOfOverallLatency>
  </PerformanceEstimates>
  <AreaEstimates><Resources><BRAM_18K>0</BRAM_18K><DSP>0</DSP><FF>20</FF><LUT>30</LUT></Resources></AreaEstimates>
</profile>
"""


class VitisCommandTests(unittest.TestCase):
    def test_builds_legacy_and_unified_native_commands(self):
        self.assertEqual(
            vitis_tcl_command("vitis_hls", "run_csim.tcl"),
            ["vitis_hls", "-f", "run_csim.tcl"],
        )
        self.assertEqual(
            vitis_tcl_command("/opt/AMD/Vitis/bin/vitis-run", "run_csim.tcl"),
            [
                "/opt/AMD/Vitis/bin/vitis-run",
                "--mode",
                "hls",
                "--tcl",
                "run_csim.tcl",
            ],
        )

    def test_default_launcher_falls_forward_to_vitis_run(self):
        with mock.patch(
            "c2hlsc_agent.vitis_command.shutil.which",
            side_effect=lambda name: "/opt/AMD/Vitis/bin/vitis-run" if name == "vitis-run" else None,
        ):
            self.assertEqual(
                find_vitis_executable(),
                "/opt/AMD/Vitis/bin/vitis-run",
            )


class VitisEvidenceTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "project"
        (project / "src").mkdir(parents=True)
        (project / "tb").mkdir()
        (project / "src" / "hls_top.cpp").write_text("void top() {}\n", encoding="utf-8")
        (project / "tb" / "testbench.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
        report_dir = project / "c2hlsc_project" / "solution1" / "syn" / "report"
        report_dir.mkdir(parents=True)
        (report_dir / "csynth.xml").write_text(CSYNTH_XML, encoding="utf-8")
        rtl_dir = project / "c2hlsc_project" / "solution1" / "syn" / "verilog"
        rtl_dir.mkdir()
        (rtl_dir / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
        (project / "cosim.log").write_text(
            "C/RTL co-simulation finished: PASS\n", encoding="utf-8"
        )
        phases = {
            "software_equivalence": "pass",
            "shift_left_trace": "pass",
            "coverage_gcov": "pass",
            "symbolic_klee": "skipped",
            "csim": "pass",
            "csynth": "pass",
            "cosim": "pass",
        }
        (project / "conversion_report.json").write_text(
            json.dumps(
                {
                    "status": "pass",
                    "top": "top",
                    "part": "xczu7ev-ffvc1156-2-e",
                    "clock_ns": 10.0,
                    "cosim_backend": "vitis",
                    "vitis_bin": "/opt/AMD/Vitis/bin/vitis-run",
                    **phases,
                }
            ),
            encoding="utf-8",
        )
        return project

    def test_accepts_fresh_native_vitis_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = validate_vitis_project(self._project(Path(tmp)))
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["backend"], "vitis")
        self.assertEqual(len(evidence["rtl"]), 1)
        self.assertEqual(
            evidence["native_cosim_command"],
            [
                "/opt/AMD/Vitis/bin/vitis-run",
                "--mode",
                "hls",
                "--tcl",
                "run_cosim.tcl",
            ],
        )

    def test_rejects_missing_positive_cosim_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            (project / "cosim.log").write_text("Vitis exited 0\n", encoding="utf-8")
            with self.assertRaisesRegex(VitisEvidenceError, "positive"):
                validate_vitis_project(project)

    def test_online_workflow_runs_native_ladder_and_evidence_gate(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "vitis-verify.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("runs-on: [self-hosted, linux, x64, vitis-hls]", workflow)
        self.assertIn("scripts/run_vitis_linux.sh", workflow)
        self.assertIn("c2hlsc_agent.vitis_evidence", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)


if __name__ == "__main__":
    unittest.main()
