import contextlib
import importlib.util
import io
import json
import stat
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

    def test_explicit_cosim_failure_marker_overrides_zero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            design_dir = Path(tmp)
            design = {"record_id": 34, "source_file": "34_hls.txt", "top": "tiny", "path": str(design_dir)}
            original_run = batch.run_vitis_command

            def fake_run(command, cwd, timeout):
                if command[-1] == "run_csynth.tcl":
                    rtl = design_dir / "hls_nl_project" / "solution1" / "syn" / "verilog" / "tiny.v"
                    rtl.parent.mkdir(parents=True)
                    rtl.write_text("module tiny; endmodule\n", encoding="utf-8")
                output = (
                    "C/RTL co-simulation finished: FAIL"
                    if command[-1] == "run_cosim.tcl"
                    else "phase completed"
                )
                return batch.VitisProcessResult(0, output)

            batch.run_vitis_command = fake_run
            try:
                row = batch.run_design("vitis_hls", design, timeout=1, run_full_cosim=True, log_tail_lines=20)
            finally:
                batch.run_vitis_command = original_run

            self.assertEqual(row["status"], "fail")
            self.assertEqual(row["failed_phase"], "cosim")
            self.assertEqual(row["returncode"], 0)
            self.assertEqual(row["phases"]["cosim"]["status"], "fail")
            self.assertEqual(row["failure_reason"], "explicit_cosim_failure_marker")
            self.assertEqual(row["failure_marker"], "co-simulation finished: fail")

    def test_explicit_cosim_pass_marker_remains_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            design_dir = Path(tmp)
            design = {"record_id": 35, "source_file": "35_hls.txt", "top": "tiny", "path": str(design_dir)}
            original_run = batch.run_vitis_command

            def fake_run(command, cwd, timeout):
                if command[-1] == "run_csynth.tcl":
                    rtl = design_dir / "hls_nl_project" / "solution1" / "syn" / "verilog" / "tiny.v"
                    rtl.parent.mkdir(parents=True)
                    rtl.write_text("module tiny; endmodule\n", encoding="utf-8")
                return batch.VitisProcessResult(0, "C/RTL co-simulation finished: PASS")

            batch.run_vitis_command = fake_run
            try:
                row = batch.run_design("vitis_hls", design, timeout=1, run_full_cosim=True, log_tail_lines=20)
            finally:
                batch.run_vitis_command = original_run

            self.assertEqual(row["status"], "pass")
            self.assertEqual(row["phases"]["cosim"]["status"], "pass")

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


HLS_CPP = """
#include <ap_int.h>
void tiny_add(ap_uint<8> a, ap_uint<8> b, ap_uint<8>& out) {
  out = a + b;
}
"""


def write_corpus(path: Path, record_ids: list[int]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record_id in record_ids:
            f.write(json.dumps({"record_id": record_id, "original_file": f"{record_id}_hls.txt", "hls_cpp": HLS_CPP}) + "\n")


def make_fake_vitis(tmp: Path) -> Path:
    fake = tmp / "vitis_hls"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


class ExecuteDesignsTests(unittest.TestCase):
    def _args(self, workers: int, stop_on_fail: bool = False):
        return type("Args", (), {
            "workers": workers,
            "stop_on_fail": stop_on_fail,
            "timeout_seconds": 1,
            "run_full_cosim": False,
            "log_tail_lines": 0,
        })()

    def test_parallel_matches_sequential(self):
        designs = [{"record_id": i, "top": "tiny_add", "path": f"/nonexistent/{i}"} for i in range(8)]

        def fake_run_design(vitis_hls, design, timeout, run_full_cosim, log_tail_lines):
            return {**design, "status": "pass"}

        original = batch.run_design
        batch.run_design = fake_run_design
        try:
            collected: dict[int, list[int]] = {}
            for workers in (1, 4):
                rows: list[dict] = []
                batch.execute_designs("vitis_hls", designs, self._args(workers), rows.append)
                collected[workers] = sorted(row["record_id"] for row in rows)
        finally:
            batch.run_design = original

        self.assertEqual(collected[1], list(range(8)))
        self.assertEqual(collected[1], collected[4])

    def test_one_record_exception_does_not_kill_the_sweep(self):
        designs = [{"record_id": i, "top": "t", "path": "x"} for i in range(4)]

        def fake_run_design(vitis_hls, design, timeout, run_full_cosim, log_tail_lines):
            if design["record_id"] == 2:
                raise OSError("disk full")
            return {**design, "status": "pass"}

        original = batch.run_design
        batch.run_design = fake_run_design
        try:
            rows: list[dict] = []
            batch.execute_designs("vitis_hls", designs, self._args(2), rows.append)
        finally:
            batch.run_design = original

        by_id = {row["record_id"]: row for row in rows}
        self.assertEqual(set(by_id), {0, 1, 2, 3})
        self.assertEqual(by_id[2]["status"], "error")
        self.assertIn("disk full", by_id[2]["error"])
        self.assertTrue(all(by_id[i]["status"] == "pass" for i in (0, 1, 3)))

    def test_stop_on_fail_sequential_stops_at_first_failure(self):
        designs = [{"record_id": i, "top": "t", "path": "x"} for i in range(5)]

        def fake_run_design(vitis_hls, design, timeout, run_full_cosim, log_tail_lines):
            return {**design, "status": "fail" if design["record_id"] == 1 else "pass"}

        original = batch.run_design
        batch.run_design = fake_run_design
        try:
            rows: list[dict] = []
            batch.execute_designs("vitis_hls", designs, self._args(1, stop_on_fail=True), rows.append)
        finally:
            batch.run_design = original
        self.assertEqual([row["record_id"] for row in rows], [0, 1])


class LoadPriorResultsTests(unittest.TestCase):
    def test_tolerates_torn_line_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vitis_batch_results.jsonl"
            path.write_text(
                json.dumps({"record_id": 1, "status": "pass"}) + "\n"
                + json.dumps({"record_id": 1, "status": "fail"}) + "\n"
                + json.dumps({"record_id": 2, "status": "pass"}) + "\n"
                + '{"record_id": 3, "status": "pa',  # torn final line from a crash
                encoding="utf-8",
            )
            rows = batch.load_prior_results(path)
            by_id = {row["record_id"]: row for row in rows}
            self.assertEqual(set(by_id), {1, 2})
            self.assertEqual(by_id[1]["status"], "fail")  # last occurrence wins

    def test_error_rows_are_retried_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vitis_batch_results.jsonl"
            path.write_text(
                json.dumps({"record_id": 1, "status": "pass"}) + "\n"
                + json.dumps({"record_id": 2, "status": "error", "error": "OSError: disk full"}) + "\n",
                encoding="utf-8",
            )
            rows = batch.load_prior_results(path)
            self.assertEqual([row["record_id"] for row in rows], [1])

    def test_generated_only_rows_do_not_count_as_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vitis_batch_results.jsonl"
            path.write_text(
                json.dumps({"record_id": 1, "status": "generated_only"}) + "\n"
                + json.dumps({"record_id": 2, "status": "timeout"}) + "\n",
                encoding="utf-8",
            )
            rows = batch.load_prior_results(path)
            self.assertEqual([row["record_id"] for row in rows], [2])

    def test_string_record_ids_normalize_to_int(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vitis_batch_results.jsonl"
            path.write_text(json.dumps({"record_id": "7", "status": "pass"}) + "\n", encoding="utf-8")
            rows = batch.load_prior_results(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(batch.normalize_record_id(rows[0]["record_id"]), 7)


class ResumeCompatibilityTests(unittest.TestCase):
    def _write_report(self, out_dir: Path, summary: dict) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "vitis_batch_report.json").write_text(json.dumps({"summary": summary}), encoding="utf-8")

    def _args(self, input_path: Path, run_full_cosim: bool = False):
        return type("Args", (), {"input": input_path, "run_full_cosim": run_full_cosim})()

    def test_mismatched_input_digest_refuses_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            corpus = Path(tmp) / "in.jsonl"
            corpus.write_text('{"record_id": 1}\n', encoding="utf-8")
            self._write_report(out_dir, {
                "input": str(corpus), "mode": "verilog_csynth", "input_sha256": "not-the-real-digest",
            })
            with self.assertRaises(SystemExit) as ctx:
                batch.check_resume_compatibility(out_dir, self._args(corpus), batch.input_digest(corpus))
            self.assertIn("input_sha256 mismatch", str(ctx.exception))

    def test_matching_prior_run_allows_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            corpus = Path(tmp) / "in.jsonl"
            corpus.write_text('{"record_id": 1}\n', encoding="utf-8")
            digest = batch.input_digest(corpus)
            self._write_report(out_dir, {"input": str(corpus), "mode": "verilog_csynth", "input_sha256": digest})
            batch.check_resume_compatibility(out_dir, self._args(corpus), digest)  # must not raise

    def test_mode_mismatch_refuses_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            corpus = Path(tmp) / "in.jsonl"
            corpus.write_text('{"record_id": 1}\n', encoding="utf-8")
            digest = batch.input_digest(corpus)
            self._write_report(out_dir, {"input": str(corpus), "mode": "full_cosim", "input_sha256": digest})
            with self.assertRaises(SystemExit):
                batch.check_resume_compatibility(out_dir, self._args(corpus, run_full_cosim=False), digest)

    def test_missing_file_is_empty(self):
        self.assertEqual(batch.load_prior_results(Path("/nonexistent/results.jsonl")), [])

    def test_repair_trailing_newline_fixes_torn_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            path.write_text(json.dumps({"record_id": 1, "status": "pass"}) + "\n" + '{"record_id": 2, "sta', encoding="utf-8")
            batch.repair_trailing_newline(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"record_id": 3, "status": "pass"}) + "\n")
            rows = batch.load_prior_results(path)
            self.assertEqual(sorted(row["record_id"] for row in rows), [1, 3])


class DuplicateRecordIdTests(unittest.TestCase):
    def test_duplicate_record_ids_fail_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "input.jsonl"
            write_corpus(corpus, [1, 2, 1])
            args = type("Args", (), {
                "input": corpus, "offset": 0, "limit": None,
                "out_dir": Path(tmp) / "out", "part": "xc7z020clg484-1", "clock": "10",
            })()
            with self.assertRaises(SystemExit) as ctx:
                batch.generate_designs(args)
            self.assertIn("duplicate record_id", str(ctx.exception))


class ResumeMainTests(unittest.TestCase):
    def test_incremental_write_and_resume_skips_done_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            corpus = tmp_path / "input.jsonl"
            out_dir = tmp_path / "out"
            write_corpus(corpus, [1, 2, 3])
            fake_vitis = make_fake_vitis(tmp_path)

            executed: list[int] = []

            def fake_run_design(vitis_hls, design, timeout, run_full_cosim, log_tail_lines):
                executed.append(design["record_id"])
                return {**design, "status": "pass", "verilog_files": ["x.v"], "cosim_artifacts": []}

            argv = [
                "run_hls_nl_vitis_batch.py",
                "--input", str(corpus),
                "--out-dir", str(out_dir),
                "--vitis-hls-bin", str(fake_vitis),
                "--workers", "2",
            ]
            original_run, original_argv = batch.run_design, sys.argv
            batch.run_design = fake_run_design
            sys.argv = argv
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = batch.main()
                self.assertEqual(rc, 0)
                self.assertEqual(sorted(executed), [1, 2, 3])
                results = batch.load_prior_results(out_dir / "vitis_batch_results.jsonl")
                self.assertEqual(sorted(row["record_id"] for row in results), [1, 2, 3])
                self.assertTrue((out_dir / "vitis_batch_report.json").exists())

                # Second run with --resume: nothing left to execute.
                executed.clear()
                sys.argv = argv + ["--resume"]
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = batch.main()
                self.assertEqual(rc, 0)
                self.assertEqual(executed, [])
                results = batch.load_prior_results(out_dir / "vitis_batch_results.jsonl")
                self.assertEqual(sorted(row["record_id"] for row in results), [1, 2, 3])
            finally:
                batch.run_design = original_run
                sys.argv = original_argv

    def test_fresh_run_backs_up_previous_results_and_generate_only_refuses_clobber(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            corpus = tmp_path / "input.jsonl"
            out_dir = tmp_path / "out"
            write_corpus(corpus, [1, 2])
            fake_vitis = make_fake_vitis(tmp_path)

            def fake_run_design(vitis_hls, design, timeout, run_full_cosim, log_tail_lines):
                return {**design, "status": "pass", "verilog_files": ["x.v"], "cosim_artifacts": []}

            argv = [
                "run_hls_nl_vitis_batch.py",
                "--input", str(corpus),
                "--out-dir", str(out_dir),
                "--vitis-hls-bin", str(fake_vitis),
            ]
            original_run, original_argv = batch.run_design, sys.argv
            batch.run_design = fake_run_design
            try:
                sys.argv = argv
                with contextlib.redirect_stdout(io.StringIO()):
                    batch.main()
                first = (out_dir / "vitis_batch_results.jsonl").read_text(encoding="utf-8")

                # A fresh (non-resume) rerun must preserve the old rows in .prev.
                with contextlib.redirect_stdout(io.StringIO()):
                    batch.main()
                prev = out_dir / "vitis_batch_results.jsonl.prev"
                self.assertTrue(prev.exists())
                self.assertEqual(prev.read_text(encoding="utf-8"), first)

                # --generate-only must refuse to overwrite executed results.
                sys.argv = argv + ["--generate-only"]
                with self.assertRaises(SystemExit) as ctx:
                    with contextlib.redirect_stdout(io.StringIO()):
                        batch.main()
                self.assertIn("refusing to overwrite", str(ctx.exception))
            finally:
                batch.run_design = original_run
                sys.argv = original_argv


if __name__ == "__main__":
    unittest.main()
