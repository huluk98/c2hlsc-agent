import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "cosim_repair_loop.py"
sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location("cosim_repair_loop", SCRIPT_PATH)
assert spec and spec.loader
loop = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = loop
spec.loader.exec_module(loop)


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


SOURCE_A = "void tiny_add(int a, int b, int &out) { out = a + b; }\n"
SOURCE_B = "void tiny_add(int a, int b, int &out) { out = a - b; }\n"


class RecordLoopSafetyTests(unittest.TestCase):
    def _args(self, out_dir: Path, max_iterations: int = 2, max_retries: int = 0):
        return SimpleNamespace(
            out_dir=out_dir,
            part="part",
            clock="10",
            timeout_seconds=1,
            log_tail_lines=20,
            max_iterations=max_iterations,
            max_infra_retries=max_retries,
            retry_backoff_seconds=0.0,
        )

    def _write_project(self, out_dir, record, sig, record_id, part, clock):
        del part, clock
        design_dir = out_dir / f"design_{record_id}"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "dut.cpp").write_text(record["hls_cpp"], encoding="utf-8")
        return {
            "record_id": record_id,
            "top": sig.name,
            "path": str(design_dir),
        }

    def _run_with_fakes(
        self,
        tmp: Path,
        run_design,
        repair,
        prior=None,
        max_iterations=2,
        max_retries=0,
    ):
        rows: list[tuple[dict, dict | None, bool]] = []

        def persist(outcome, corpus, *, terminal=True):
            rows.append((dict(outcome), dict(corpus) if corpus else None, terminal))

        originals = loop.write_project, loop.run_design, loop.repair
        loop.write_project = self._write_project
        loop.run_design = run_design
        loop.repair = repair
        try:
            final = loop.run_record_loop(
                1,
                {"record_id": 1, "hls_cpp": SOURCE_A},
                self._args(
                    tmp,
                    max_iterations=max_iterations,
                    max_retries=max_retries,
                ),
                "vitis_hls",
                lambda system, user: "unused",
                persist,
                "test repairer",
                threading.Event(),
                prior=prior,
            )
        finally:
            loop.write_project, loop.run_design, loop.repair = originals
        return final, rows

    def test_rejects_a_to_b_to_a_cycle_and_keeps_latest_source(self):
        repairs = iter([SOURCE_B, SOURCE_A])

        def failing_run(vitis_hls, design, timeout, run_full_cosim, log_tail_lines):
            del vitis_hls, timeout, run_full_cosim, log_tail_lines
            return {
                **design,
                "status": "fail",
                "failed_phase": "cosim",
                "vitis_log_tail": "stable mismatch",
            }

        with tempfile.TemporaryDirectory() as tmp:
            final, rows = self._run_with_fakes(
                Path(tmp),
                failing_run,
                lambda *unused: next(repairs),
            )
            self.assertEqual(final["status"], "exhausted")
            self.assertIn("previously seen source", final["reason"])
            self.assertEqual(final["hls_cpp"], SOURCE_B)
            self.assertTrue(any(row[0]["status"] == "running" for row in rows))

    def test_infrastructure_error_after_repair_preserves_repaired_source(self):
        calls = 0

        def fail_then_error(vitis_hls, design, timeout, run_full_cosim, log_tail_lines):
            nonlocal calls
            del vitis_hls, timeout, run_full_cosim, log_tail_lines
            calls += 1
            if calls == 1:
                return {
                    **design,
                    "status": "fail",
                    "failed_phase": "cosim",
                    "vitis_log_tail": "functional mismatch",
                }
            raise OSError("temporary Vitis transport failure")

        with tempfile.TemporaryDirectory() as tmp:
            final, rows = self._run_with_fakes(
                Path(tmp),
                fail_then_error,
                lambda *unused: SOURCE_B,
                max_iterations=1,
            )
            self.assertEqual(final["status"], "exhausted")
            self.assertIn("infrastructure retry budget", final["reason"])
            self.assertEqual(final["hls_cpp"], SOURCE_B)
            self.assertEqual(rows[-1][1]["hls_cpp"], SOURCE_B)

    def test_seen_source_failure_state_survives_resume(self):
        failure = {
            "status": "fail",
            "failed_phase": "cosim",
            "vitis_log_tail": "stable mismatch",
        }
        failure_hash = loop.failure_fingerprint_for_result(failure)
        source_hash = loop.source_fingerprint(SOURCE_A)
        state_hash = loop.verification_state_fingerprint(source_hash, failure_hash)
        prior = {
            "record_id": 1,
            "status": "running",
            "hls_cpp": SOURCE_A,
            "iterations": [],
            "repaired": False,
            "repairs_used": 0,
            "retry_count": 0,
            "max_iterations": 2,
            "max_infra_retries": 0,
            "seen_source_fingerprints": [source_hash],
            "seen_state_fingerprints": [state_hash],
        }
        repair_calls = 0

        def failing_run(vitis_hls, design, timeout, run_full_cosim, log_tail_lines):
            del vitis_hls, timeout, run_full_cosim, log_tail_lines
            return {**design, **failure}

        def should_not_repair(*unused):
            nonlocal repair_calls
            repair_calls += 1
            return SOURCE_B

        with tempfile.TemporaryDirectory() as tmp:
            final, _ = self._run_with_fakes(
                Path(tmp),
                failing_run,
                should_not_repair,
                prior=prior,
            )
            self.assertEqual(final["status"], "exhausted")
            self.assertIn("same source and failure", final["reason"])
            self.assertEqual(repair_calls, 0)

    def test_repair_backend_retries_are_bounded(self):
        def failing_run(vitis_hls, design, timeout, run_full_cosim, log_tail_lines):
            del vitis_hls, timeout, run_full_cosim, log_tail_lines
            return {
                **design,
                "status": "fail",
                "failed_phase": "cosim",
                "vitis_log_tail": "functional mismatch",
            }

        repair_calls = 0

        def unavailable_repair(*unused):
            nonlocal repair_calls
            repair_calls += 1
            raise OSError("model transport unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            final, rows = self._run_with_fakes(
                Path(tmp),
                failing_run,
                unavailable_repair,
                max_retries=1,
            )
            self.assertEqual(repair_calls, 2)
            self.assertEqual(final["status"], "exhausted")
            self.assertIn("repair backend retry budget", final["reason"])
            self.assertEqual(final["hls_cpp"], SOURCE_A)
            self.assertTrue(any(row[0]["status"] == "retry_pending" for row in rows))


if __name__ == "__main__":
    unittest.main()
