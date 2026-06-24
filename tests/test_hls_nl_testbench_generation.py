import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_hls_nl_testbenches.py"
spec = importlib.util.spec_from_file_location("generate_hls_nl_testbenches", SCRIPT_PATH)
assert spec and spec.loader
gen = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gen
spec.loader.exec_module(gen)


class HlsNlTestbenchGenerationTests(unittest.TestCase):
    def test_extracts_hls_function_with_ap_uint_refs(self):
        code = """
        #include <ap_int.h>
        void simple_calculator(ap_uint<32> a, ap_uint<32> b,
                               ap_uint<32>& add, ap_uint<32>& sub) {
          add = a + b;
          sub = a - b;
        }
        """
        sig = gen.extract_function(code)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.name, "simple_calculator")
        self.assertEqual([arg.name for arg in sig.args], ["a", "b", "add", "sub"])
        self.assertEqual(sig.args[2].direction, "output")

    def test_renders_semantic_calculator_testbench(self):
        record = {
            "file": "1_hls.txt",
            "HLS_instruction": "**Design Task:** Simple Calculator\n",
            "hls_cpp": """
            #include <ap_int.h>
            void simple_calculator(ap_uint<32> a, ap_uint<32> b,
                                   ap_uint<32>& add, ap_uint<32>& sub,
                                   ap_uint<32>& mul, ap_uint<32>& div,
                                   bool reset_n) {
              add = a + b;
              sub = a - b;
              mul = a * b;
              div = b == 0 ? 0 : a / b;
            }
            """,
        }
        sig = gen.extract_function(record["hls_cpp"])
        oracle_kind, tb = gen.render_testbench(record, sig, 0)
        self.assertEqual(oracle_kind, "semantic")
        self.assertIn("Oracle kind: semantic", tb)
        self.assertIn("if (add != static_cast<ap_uint<32>>(a + b))", tb)
        self.assertIn("reset_n = static_cast<bool>(1)", tb)

    def test_writes_design_bundle(self):
        record = {
            "file": "7_hls.txt",
            "HLS_instruction": "**Design Task:** Comparator\n",
            "hls_cpp": """
            #include <ap_int.h>
            void comparator_block(ap_uint<8> a, ap_uint<8> b,
                                  ap_uint<1>& gt, ap_uint<1>& eq, ap_uint<1>& lt) {
              gt = a > b;
              eq = a == b;
              lt = a < b;
            }
            """,
        }
        sig = gen.extract_function(record["hls_cpp"])
        with tempfile.TemporaryDirectory() as tmp:
            row = gen.write_design(Path(tmp), record, sig, 5, "xc7z020clg484-1", "10")
            design_dir = Path(row["path"])
            self.assertEqual(row["oracle_kind"], "semantic")
            self.assertTrue((design_dir / "dut.cpp").exists())
            self.assertTrue((design_dir / "tb.cpp").exists())
            self.assertIn("cosim_design -rtl verilog", (design_dir / "run_hls.tcl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
