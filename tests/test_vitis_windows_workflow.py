from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "vivado-windows-verify.yml"
HELPER = ROOT / "scripts" / "run_vitis_windows.ps1"


class WindowsVitisWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.helper = HELPER.read_text(encoding="utf-8")

    def test_requires_the_licensed_windows_runner_and_powershell(self):
        self.assertIn(
            "runs-on: [self-hosted, windows, x64, vivado-hls]", self.workflow
        )
        self.assertIn("shell: powershell", self.workflow)
        self.assertNotIn("bash", self.workflow.lower())
        self.assertNotIn("run_vitis_linux.sh", self.workflow)
        self.assertNotIn("/bin/", self.workflow)

    def test_discovers_only_supported_native_amd_launchers(self):
        for launcher in (
            "vitis-run.exe",
            "vitis-run.bat",
            "vitis_hls.exe",
            "vitis_hls.bat",
            "vivado_hls.exe",
            "vivado_hls.bat",
        ):
            self.assertIn(launcher, self.helper)
        self.assertIn("Get-Command", self.helper)
        self.assertIn("-CommandType Application", self.helper)
        self.assertIn("Resolve-Path -LiteralPath", self.helper)
        self.assertIn("settings64.bat", self.helper)
        self.assertIn('Resolve-HostTool @("make.exe", "make")', self.helper)
        self.assertIn(
            'Resolve-HostTool @("g++.exe", "g++", "clang++.exe", "clang++")',
            self.helper,
        )
        self.assertIn("VITIS_SETTINGS contains characters unsafe", self.helper)
        self.assertIn('$name.StartsWith("vitis_hls")', self.helper)
        self.assertIn('@("-version")', self.helper)
        self.assertTrue(self.helper.rstrip().endswith("}"))

    def test_runs_the_same_conversion_contract_and_fail_closed_validator(self):
        for argument in (
            '"c2hlsc_agent.cli", "convert"',
            '"--no-llm"',
            '"--shift-left"',
            '"--run-vitis"',
            '"--cosim-backend", "vitis"',
            '"--vitis-bin", $launcher',
            '"c2hlsc_agent.vitis_evidence"',
        ):
            self.assertIn(argument, self.helper)
        self.assertIn("$LASTEXITCODE -ne 0", self.helper)
        self.assertIn("stale synthesis", self.helper)
        self.assertIn("positive PASS marker", self.helper)

    def test_uploads_reports_rtl_and_machine_readable_evidence(self):
        self.assertIn("actions/upload-artifact@v4", self.workflow)
        self.assertIn("conversion_report.*", self.workflow)
        self.assertIn("vitis_evidence.json", self.workflow)
        self.assertIn("toolchain_provenance.json", self.workflow)
        self.assertIn("/syn/report/**", self.workflow)
        self.assertIn("/syn/verilog/**", self.workflow)
        self.assertIn("/sim/report/**", self.workflow)
        self.assertNotIn("/impl/report/**", self.workflow)


if __name__ == "__main__":
    unittest.main()
