import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "cosim_repair_loop.py"
sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location("cosim_repair_loop", SCRIPT_PATH)
assert spec and spec.loader
loop = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = loop
spec.loader.exec_module(loop)


class PickCodeTests(unittest.TestCase):
    def test_accepts_fenced_definition_of_requested_top(self):
        response = "```cpp\nvoid target(int value) { (void)value; }\n```"
        self.assertEqual(loop.pick_code(response, "target"), "void target(int value) { (void)value; }\n")

    def test_accepts_unfenced_definition_of_requested_top(self):
        response = "void target(int value) { (void)value; }"
        self.assertEqual(loop.pick_code(response, "target"), response + "\n")

    def test_rejects_declaration_only(self):
        self.assertIsNone(loop.pick_code("```cpp\nvoid target(int value);\n```", "target"))

    def test_rejects_wrong_top_fenced_fallback(self):
        response = "```cpp\nvoid different(int value) { (void)value; }\n```"
        self.assertIsNone(loop.pick_code(response, "target"))


class LoadDoneIdsTests(unittest.TestCase):
    def test_reads_ids_and_tolerates_bad_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            path.write_text(
                json.dumps({"record_id": 5, "status": "pass"}) + "\n"
                + json.dumps({"record_id": 9, "status": "fail"}) + "\n"
                + json.dumps({"status": "skipped"}) + "\n"  # no record_id
                + json.dumps({"record_id": None}) + "\n"
                + json.dumps({"record_id": 7, "status": "error", "error": "ssh dropped"}) + "\n"  # retried on resume
                + '{"record_id": 12, "sta',  # torn final line from a crash
                encoding="utf-8",
            )
            self.assertEqual(loop.load_done_ids(path), {5, 9})

    def test_missing_file_is_empty(self):
        self.assertEqual(loop.load_done_ids(Path("/nonexistent/results.jsonl")), set())


class ReconcileCorpusTests(unittest.TestCase):
    def test_dedupes_last_wins_and_drops_torn_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repaired_corpus.jsonl"
            path.write_text(
                json.dumps({"record_id": 1, "hls_cpp": "old", "cosim_status": "fail"}) + "\n"
                + json.dumps({"record_id": 2, "hls_cpp": "b", "cosim_status": "pass"}) + "\n"
                + json.dumps({"record_id": 1, "hls_cpp": "new", "cosim_status": "pass"}) + "\n"
                + '{"record_id": 3, "hls_c',  # torn final line
                encoding="utf-8",
            )
            loop.reconcile_corpus(path)
            rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
            by_id = {row["record_id"]: row for row in rows}
            self.assertEqual(len(rows), 2)  # no duplicate ids, torn line gone
            self.assertEqual(by_id[1]["hls_cpp"], "new")  # last occurrence wins

    def test_missing_file_is_noop(self):
        loop.reconcile_corpus(Path("/nonexistent/repaired_corpus.jsonl"))


class LoadResultRowsTests(unittest.TestCase):
    def test_last_row_per_id_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            path.write_text(
                json.dumps({"record_id": 1, "status": "error"}) + "\n"
                + json.dumps({"record_id": 1, "status": "pass", "repaired": True}) + "\n",
                encoding="utf-8",
            )
            rows = loop.load_result_rows(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "pass")


if __name__ == "__main__":
    unittest.main()
