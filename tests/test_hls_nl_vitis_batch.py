import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "run_hls_nl_vitis_batch.py"
sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location("run_hls_nl_vitis_batch", SCRIPT_PATH)
assert spec and spec.loader
batch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = batch
spec.loader.exec_module(batch)


class HlsNlVitisBatchTests(unittest.TestCase):
    def test_timeout_is_recorded_with_failed_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            design_dir = Path(tmp)
            design = {
                "record_id": 33,
                "source_file": "33_hls.txt",
                "top": "slow_top",
                "path": str(design_dir),
            }

            original_run = batch.run_vitis_command

            def fake_run(command, cwd, timeout):
                if command[-1] == "run_csim.tcl":
                    return batch.VitisProcessResult(0, "csim ok")
                return batch.VitisProcessResult(None, "partial stdout bytes\npartial stderr bytes", timed_out=True)

            batch.run_vitis_command = fake_run
            try:
                row = batch.run_design("vitis_hls", design, timeout=1, run_full_cosim=True, log_tail_lines=20)
            finally:
                batch.run_vitis_command = original_run

            self.assertEqual(row["status"], "timeout")
            self.assertEqual(row["failed_phase"], "csynth")
            self.assertEqual(row["timeout_seconds"], 1)
            self.assertIn("partial stdout bytes", row["vitis_log_tail"])
            self.assertIn("partial stderr bytes", row["vitis_log_tail"])
            self.assertIn("partial stdout bytes", (design_dir / "vitis_full.log").read_text(encoding="utf-8"))
            self.assertEqual(row["phases"]["csim"]["status"], "pass")
            self.assertEqual(row["phases"]["csynth"]["status"], "timeout")

    def test_full_cosim_uses_split_phase_tcls(self):
        plan = batch.phase_plan(run_full_cosim=True)
        self.assertEqual(plan, [("csim", "run_csim.tcl"), ("csynth", "run_csynth.tcl"), ("cosim", "run_cosim.tcl")])

    def test_generated_design_contains_split_tcls(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {
                "record_id": 7,
                "original_file": "7_hls.txt",
                "HLS_instruction": "**Design Task:** Tiny Add\n",
                "hls_cpp": """
                #include <ap_int.h>
                void tiny_add(ap_uint<8> a, ap_uint<8> b, ap_uint<8>& out) {
                  out = a + b;
                }
                """,
            }
            args = type(
                "Args",
                (),
                {
                    "input": Path(tmp) / "input.jsonl",
                    "offset": 0,
                    "limit": None,
                    "out_dir": Path(tmp) / "out",
                    "part": "xc7z020clg484-1",
                    "clock": "10",
                },
            )()
            args.input.write_text(batch.json.dumps(record) + "\n", encoding="utf-8")

            designs, skipped = batch.generate_designs(args)

            self.assertEqual(skipped, [])
            design_dir = Path(designs[0]["path"])
            self.assertIn("csim_design", (design_dir / "run_csim.tcl").read_text(encoding="utf-8"))
            self.assertIn("csynth_design", (design_dir / "run_csynth.tcl").read_text(encoding="utf-8"))
            self.assertIn("cosim_design -rtl verilog", (design_dir / "run_cosim.tcl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
