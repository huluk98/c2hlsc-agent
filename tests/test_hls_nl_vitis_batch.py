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
    def test_timeout_with_bytes_output_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            design_dir = Path(tmp)
            design = {
                "record_id": 33,
                "source_file": "33_hls.txt",
                "top": "slow_top",
                "path": str(design_dir),
            }

            original_run = batch.subprocess.run

            def fake_run(*args, **kwargs):
                raise batch.subprocess.TimeoutExpired(
                    cmd=args[0],
                    timeout=kwargs["timeout"],
                    output=b"partial stdout bytes",
                    stderr=b"partial stderr bytes",
                )

            batch.subprocess.run = fake_run
            try:
                row = batch.run_design("vitis_hls", design, timeout=1, run_full_cosim=True, log_tail_lines=20)
            finally:
                batch.subprocess.run = original_run

            self.assertEqual(row["status"], "timeout")
            self.assertEqual(row["timeout_seconds"], 1)
            self.assertIn("partial stdout bytes", row["vitis_log_tail"])
            self.assertIn("partial stderr bytes", row["vitis_log_tail"])
            self.assertIn("partial stdout bytes", (design_dir / "vitis_full.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
