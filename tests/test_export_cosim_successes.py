import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_cosim_successes.py"
spec = importlib.util.spec_from_file_location("export_cosim_successes", SCRIPT_PATH)
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = exporter
spec.loader.exec_module(exporter)


class ExportCosimSuccessesTests(unittest.TestCase):
    def test_exports_only_full_cosim_pass_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pass_case = root / "build" / "00000_pass_top"
            fail_case = root / "build" / "00001_fail_top"
            pass_case.mkdir(parents=True)
            fail_case.mkdir(parents=True)
            for name in exporter.MINIMAL_FILES:
                (pass_case / name).write_text(f"pass {name}\n", encoding="utf-8")
                (fail_case / name).write_text(f"fail {name}\n", encoding="utf-8")

            report = {
                "summary": {
                    "input": "accepted.jsonl",
                    "mode": "full_cosim",
                    "out_dir": str(root / "build"),
                    "part": "xc7z020clg484-1",
                    "clock": "10",
                },
                "results": [
                    {
                        "record_id": 0,
                        "source_file": "0_hls.txt",
                        "top": "pass_top",
                        "signature": "void pass_top()",
                        "oracle_kind": "semantic",
                        "status": "pass",
                        "returncode": 0,
                        "path": str(pass_case),
                        "verilog_files": ["hls_nl_project/solution1/syn/verilog/pass_top.v"],
                        "cosim_artifacts": ["hls_nl_project/solution1/sim/report/pass_top_cosim.rpt"],
                    },
                    {
                        "record_id": 1,
                        "source_file": "1_hls.txt",
                        "top": "fail_top",
                        "status": "fail",
                        "path": str(fail_case),
                    },
                ],
                "skipped": [{"record_id": 2, "status": "skipped"}],
            }
            report_path = root / "vitis_batch_report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            out_dir = root / "verified"

            self.assertEqual(exporter.main_from_args(["--report", str(report_path), "--out-dir", str(out_dir)]), 0)

            self.assertTrue((out_dir / "00000_pass_top" / "dut.cpp").exists())
            self.assertFalse((out_dir / "00001_fail_top").exists())
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["summary"]["passed"], 1)
            self.assertEqual(manifest["summary"]["failed"], 1)
            self.assertEqual(manifest["summary"]["skipped"], 1)
            self.assertIn('"top": "fail_top"', (out_dir / "failed.jsonl").read_text(encoding="utf-8"))

    def test_refuses_non_full_cosim_report_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "report.json"
            report_path.write_text(
                json.dumps({"summary": {"mode": "verilog_csynth"}, "results": [], "skipped": []}),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                exporter.main_from_args(["--report", str(report_path), "--out-dir", str(root / "out")])


if __name__ == "__main__":
    unittest.main()
