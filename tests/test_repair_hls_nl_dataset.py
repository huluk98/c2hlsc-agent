import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "repair_hls_nl_dataset.py"
spec = importlib.util.spec_from_file_location("repair_hls_nl_dataset", SCRIPT_PATH)
assert spec and spec.loader
repair = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = repair
spec.loader.exec_module(repair)


class RepairHlsNlDatasetTests(unittest.TestCase):
    def test_deletes_unbounded_while_loop(self):
        loop = "while (" + "true" + ")"
        record = {
            "HLS_instruction": "**Design Task:** Stream service\n",
            "hls_cpp": f"""
            #include <ap_int.h>
            void stream_service(ap_uint<8>& out) {{
              {loop} {{
                out++;
              }}
            }}
            """,
        }

        result = repair.repair_record(record, 0)

        self.assertEqual(result.status, "deleted")
        self.assertIn("contains_unbounded_infinite_loop", result.quarantine_reasons)
        self.assertTrue(result.code_features["has_unbounded_loop"])

    def test_deletes_infinite_for_loop(self):
        loop = "for (" + ";;" + ")"
        record = {
            "HLS_instruction": "**Design Task:** Loop service\n",
            "hls_cpp": f"""
            #include <ap_int.h>
            void loop_service(ap_uint<8>& out) {{
              {loop} {{
                out++;
              }}
            }}
            """,
        }

        result = repair.repair_record(record, 0)

        self.assertEqual(result.status, "deleted")
        self.assertIn("contains_unbounded_infinite_loop", result.quarantine_reasons)
        self.assertTrue(result.code_features["has_unbounded_loop"])

    def test_accepts_statically_bounded_loop(self):
        record = {
            "HLS_instruction": "**Design Task:** Bounded loop\n",
            "hls_cpp": """
            #include <ap_int.h>
            void bounded(ap_uint<8>& out) {
              out = 0;
              for (int i = 0; i < 4; ++i) {
                out += i;
              }
            }
            """,
        }

        result = repair.repair_record(record, 0)

        self.assertEqual(result.status, "accepted")
        self.assertFalse(result.code_features["has_unbounded_loop"])


if __name__ == "__main__":
    unittest.main()
