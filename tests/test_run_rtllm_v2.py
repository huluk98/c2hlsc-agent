"""Offline tests for the RTLLM v2.0 driver.

Everything here is hermetic: ``rtllm_bench.discover_designs`` /
``rtllm_bench.evaluate_reference`` and ``rtllm_agent.run_design`` are replaced by fakes, so no
``claude`` CLI, no ``iverilog``, and no benchmark checkout is required. The driver lives in
``scripts/`` rather than the package, so it is loaded from its path the way
``tests/test_hls_nl_vitis_batch.py`` loads its script.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Sequence
from unittest import mock

from c2hlsc_agent import rtllm_agent, rtllm_bench
from c2hlsc_agent.rtllm_agent import AttemptRecord, DesignResult, SampleResult
from c2hlsc_agent.rtllm_bench import RtllmDesign, SimResult


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "run_rtllm_v2.py"
sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location("run_rtllm_v2", SCRIPT_PATH)
assert spec and spec.loader
driver = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = driver
spec.loader.exec_module(driver)


RTL = "module {name}(input a, output b);\n  assign b = a;\nendmodule\n"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def make_design(root: Path, name: str, category: str = "Arithmetic/Adder") -> RtllmDesign:
    directory = root / category / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "design_description.txt").write_text(f"spec for {name}\n", encoding="utf-8")
    (directory / "testbench.v").write_text("// testbench\n", encoding="utf-8")
    return RtllmDesign(
        name=name,
        category=category,
        directory=directory,
        description=f"spec for {name}\n",
        testbench=directory / "testbench.v",
        reference_files=(),
    )


def make_sim(
    name: str,
    *,
    syntax: bool = True,
    func: bool = True,
    family: "str | None" = None,
    duration: float = 1.5,
    shim: bool = False,
) -> SimResult:
    return SimResult(
        design=name,
        syntax_pass=syntax,
        func_pass=func,
        func_pass_strict=func,
        timed_out=False,
        compile_log=f"compile log for {name}",
        sim_log=f"sim log for {name}",
        duration_s=duration,
        failure_family=family,
        shim_applied=shim,
    )


def make_design_result(
    name: str,
    category: str,
    verdicts: "Sequence[bool]",
    families: "Sequence[str] | None" = None,
    rounds_per_sample: int = 1,
) -> DesignResult:
    """A ``DesignResult`` with one sample per entry in ``verdicts`` (True == func pass)."""

    samples: "list[SampleResult]" = []
    for index, ok in enumerate(verdicts):
        family = None if ok else ((families or ["functional_mismatch"] * len(verdicts))[index])
        rounds = []
        for round_index in range(rounds_per_sample):
            last = round_index == rounds_per_sample - 1
            sim = make_sim(name, func=ok and last, family=None if (ok and last) else family)
            rounds.append(
                AttemptRecord(
                    round=round_index,
                    role="rtl_generator" if round_index == 0 else "rtl_repair_agent",
                    sim=sim,
                    rtl=RTL.format(name=name),
                )
            )
        samples.append(
            SampleResult(
                design=name,
                sample=index,
                syntax_pass=True,
                func_pass=bool(ok),
                func_pass_strict=bool(ok),
                rounds=rounds,
                contract=f"contract for {name}",
                final_rtl=RTL.format(name=name),
            )
        )
    return DesignResult(
        design=name,
        category=category,
        samples=samples,
        syntax_success=len(samples),
        func_success=sum(1 for ok in verdicts if ok),
    )


def run_main(argv: "list[str]") -> "tuple[int, str]":
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = driver.main(argv)
    return code, buffer.getvalue()


def read_jsonl(path: Path) -> "list[dict[str, Any]]":
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class DriverTestCase(unittest.TestCase):
    """Shared scaffolding: a fake benchmark tree and a patchable discovery hook."""

    design_names = ("aaa_adder", "bbb_counter", "ccc_fifo")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bench = self.tmp / "RTLLM"
        self.bench.mkdir(parents=True, exist_ok=True)
        self.out = self.tmp / "out"
        self.designs = [make_design(self.bench, name) for name in self.design_names]
        self.addCleanup(self._tmp.cleanup)

    def patch_discovery(self) -> Any:
        designs = self.designs

        def fake_discover(root: Path, include: Sequence[str] = (), exclude: Sequence[str] = ()):
            selected = list(designs)
            if include:
                selected = [d for d in selected if d.name in set(include)]
            if exclude:
                selected = [d for d in selected if d.name not in set(exclude)]
            return sorted(selected, key=lambda d: d.name)

        return mock.patch.object(rtllm_bench, "discover_designs", fake_discover)

    def patch_run_design(self, hook: "Callable[[RtllmDesign], DesignResult] | None" = None) -> Any:
        calls: "list[str]" = []

        def fake_run_design(design, client, config, workdir):
            calls.append(design.name)
            if hook is not None:
                return hook(design)
            return make_design_result(design.name, design.category, [True] * max(1, config.samples))

        patcher = mock.patch.object(rtllm_agent, "run_design", fake_run_design)
        patcher.calls = calls  # type: ignore[attr-defined]
        return patcher


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #


class ParserTests(unittest.TestCase):
    def test_parser_accepts_every_documented_flag(self):
        args = driver.build_parser().parse_args(
            [
                "--benchmark", "/bench",
                "--clone",
                "--out-dir", "/out",
                "--designs", "adder_8bit", "alu",
                "--designs", "asyn_fifo",
                "--exclude", "clkgenerator",
                "--limit", "7",
                "--workers", "4",
                "--samples", "5",
                "--max-repair-rounds", "3",
                "--no-plan",
                "--evidence-policy", "none",
                "--reference",
                "--resume",
                "--sim-timeout", "11",
                "--compile-timeout", "22",
                "--no-shims",
                "--llm-backend", "anthropic",
                "--llm-model", "claude-opus-5",
                "--llm-cli-cmd", "claude",
                "--verbose",
            ]
        )
        self.assertEqual(args.benchmark, Path("/bench"))
        self.assertEqual(args.clone, rtllm_bench.DEFAULT_BENCHMARK_URL)
        self.assertEqual(args.out_dir, Path("/out"))
        self.assertEqual(args.designs, ["adder_8bit", "alu", "asyn_fifo"])
        self.assertEqual(args.exclude, ["clkgenerator"])
        self.assertEqual((args.limit, args.workers, args.samples, args.max_repair_rounds), (7, 4, 5, 3))
        self.assertTrue(args.no_plan and args.reference and args.resume and args.no_shims and args.verbose)
        self.assertEqual(args.evidence_policy, "none")
        self.assertEqual((args.sim_timeout, args.compile_timeout), (11, 22))
        self.assertEqual((args.llm_backend, args.llm_model, args.llm_cli_cmd), ("anthropic", "claude-opus-5", "claude"))

    def test_clone_takes_an_explicit_url_and_defaults_to_upstream(self):
        parser = driver.build_parser()
        self.assertIsNone(parser.parse_args(["--out-dir", "/o"]).clone)
        self.assertEqual(parser.parse_args(["--out-dir", "/o", "--clone", "https://x/y.git"]).clone, "https://x/y.git")

    def test_defaults_match_the_bench_module(self):
        args = driver.build_parser().parse_args(["--out-dir", "/o"])
        self.assertEqual(args.sim_timeout, rtllm_bench.DEFAULT_SIM_TIMEOUT)
        self.assertEqual(args.compile_timeout, rtllm_bench.DEFAULT_COMPILE_TIMEOUT)
        self.assertEqual((args.samples, args.workers), (1, 1))


class BenchmarkResolutionTests(DriverTestCase):
    def test_missing_checkout_without_clone_exits_with_a_message(self):
        args = driver.build_parser().parse_args(["--out-dir", str(self.out), "--benchmark", str(self.tmp / "nope")])
        with self.assertRaises(SystemExit) as ctx:
            driver.resolve_benchmark(args)
        message = str(ctx.exception)
        self.assertIn("not found", message)
        self.assertIn("--clone", message)

    def test_clone_without_git_exits_with_a_message_not_a_traceback(self):
        args = driver.build_parser().parse_args(
            ["--out-dir", str(self.out), "--benchmark", str(self.tmp / "nope"), "--clone"]
        )
        with mock.patch.object(driver.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                driver.resolve_benchmark(args)
        self.assertIn("git is not on PATH", str(ctx.exception))

    def test_failed_clone_reports_git_output(self):
        args = driver.build_parser().parse_args(
            ["--out-dir", str(self.out), "--benchmark", str(self.tmp / "nope"), "--clone"]
        )
        completed = mock.Mock(returncode=128, stdout="", stderr="fatal: repository not found")
        with mock.patch.object(driver.shutil, "which", return_value="/usr/bin/git"), mock.patch.object(
            driver.subprocess, "run", return_value=completed
        ), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                driver.resolve_benchmark(args)
        self.assertIn("fatal: repository not found", str(ctx.exception))

    def test_existing_checkout_is_used_as_is(self):
        args = driver.build_parser().parse_args(["--out-dir", str(self.out), "--benchmark", str(self.bench)])
        with mock.patch.object(driver.subprocess, "run", side_effect=AssertionError("must not clone")):
            self.assertEqual(driver.resolve_benchmark(args), self.bench)


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


class MetricsTests(unittest.TestCase):
    def test_pass_at_k_is_the_unbiased_estimator(self):
        self.assertEqual(driver.pass_at_k(4, 0, 1), 0.0)
        self.assertEqual(driver.pass_at_k(4, 2, 1), 0.5)
        self.assertEqual(driver.pass_at_k(4, 4, 1), 1.0)
        # 4 samples, 2 correct: every 4-subset of 4 contains a correct one.
        self.assertEqual(driver.pass_at_k(4, 2, 4), 1.0)
        # 5 samples, 1 correct, k=2 -> 1 - C(4,2)/C(5,2) = 1 - 6/10.
        self.assertAlmostEqual(driver.pass_at_k(5, 1, 2), 0.4)

    def test_pass_at_k_is_undefined_rather_than_clamped_when_n_lt_k(self):
        self.assertIsNone(driver.pass_at_k(1, 0, 5))
        self.assertIsNone(driver.pass_at_k(0, 0, 1))
        self.assertIsNone(driver.pass_at_k(3, 3, 0))


class ReportMathTests(unittest.TestCase):
    """Hand-built fixture: 3 designs x 4 samples, one of them on the broken-oracle list."""

    def setUp(self) -> None:
        self.assertTrue(rtllm_bench.KNOWN_ORACLE_ISSUES, "the bench module must ship known oracle issues")
        self.broken = sorted(rtllm_bench.KNOWN_ORACLE_ISSUES)[0]
        rows = [
            # 2 of 4 samples pass; the 2 failures are compile errors.
            make_design_result(
                "aaa_adder",
                "Arithmetic/Adder",
                [True, False, True, False],
                ["-", "compile_error", "-", "compile_error"],
            ),
            # never passes: 4 functional mismatches.
            make_design_result("bbb_counter", "Control/Counter", [False] * 4, ["functional_mismatch"] * 4),
            # never passes either, but its oracle is known broken -> excluded from "adjusted".
            make_design_result(self.broken, "Misc/Clock", [False] * 4, ["functional_mismatch"] * 4),
        ]
        self.report = driver.build_report(
            [row.to_dict() for row in rows],
            mode="llm",
            k=4,
            selected=["aaa_adder", "bbb_counter", self.broken],
            backend="claude-cli",
            model="opus",
        )

    def test_totals_count_designs_and_samples(self):
        totals = self.report["totals"]
        self.assertEqual(totals["designs"], 3)
        self.assertEqual(totals["samples"], 12)
        self.assertEqual(totals["designs_func_success"], 1)
        self.assertEqual(totals["samples_func_success"], 2)
        self.assertAlmostEqual(totals["designs_func_rate"], 1 / 3)
        self.assertAlmostEqual(totals["samples_func_rate"], 2 / 12)
        self.assertEqual(totals["designs_syntax_success"], 3)

    def test_pass_at_1_and_pass_at_k_average_the_estimator_over_designs(self):
        totals = self.report["totals"]
        self.assertEqual(totals["k"], 4)
        # (0.5 + 0 + 0) / 3 and (1.0 + 0 + 0) / 3.
        self.assertAlmostEqual(totals["pass@1"], 0.5 / 3)
        self.assertAlmostEqual(totals["pass@k"], 1 / 3)
        self.assertEqual(totals["pass@1_designs_scored"], 3)

    def test_adjusted_metrics_drop_only_the_broken_oracle_design(self):
        adjusted = self.report["adjusted"]
        self.assertEqual(adjusted["designs"], 2)
        self.assertEqual(adjusted["excluded_designs"], [self.broken])
        self.assertAlmostEqual(adjusted["designs_func_rate"], 0.5)
        self.assertAlmostEqual(adjusted["pass@1"], 0.25)
        self.assertAlmostEqual(adjusted["pass@k"], 0.5)
        # The flattering number never replaces the raw one.
        self.assertAlmostEqual(self.report["totals"]["designs_func_rate"], 1 / 3)

    def test_failure_family_counts(self):
        self.assertEqual(
            self.report["failure_families"],
            {"compile_error": 2, "functional_mismatch": 8},
        )
        # Per design: only the two designs that never passed contribute.
        self.assertEqual(self.report["failure_families_by_design"], {"functional_mismatch": 2})

    def test_oracle_section_lists_known_issues_and_affected_designs(self):
        oracle = self.report["oracle"]
        self.assertEqual(oracle["affected_selected_designs"], [self.broken])
        self.assertEqual(oracle["sound_selected_designs"], 2)
        self.assertIn(self.broken, oracle["known_issues"])
        self.assertTrue(oracle["known_issues"][self.broken])

    def test_designs_table_is_sorted_by_category_then_name(self):
        rows = self.report["designs"]
        self.assertEqual([row["design"] for row in rows], ["aaa_adder", "bbb_counter", self.broken])
        self.assertEqual([row["category"] for row in rows], ["Arithmetic/Adder", "Control/Counter", "Misc/Clock"])

    def test_markdown_reports_both_raw_and_adjusted_and_names_the_broken_oracle(self):
        text = driver.render_markdown(self.report)
        self.assertIn("| design | category | syntax | func | repair rounds | failure family |", text)
        self.assertIn("aaa_adder", text)
        self.assertIn("## Caveats", text)
        self.assertIn(self.broken, text)
        self.assertIn("broken oracle", text)
        self.assertIn("pass@1", text)
        self.assertIn("pass@4", text)
        self.assertIn("adjusted", text.lower())

    def test_empty_report_is_still_writable(self):
        report = driver.build_report([], mode="llm", k=1, selected=[])
        self.assertEqual(report["totals"]["designs"], 0)
        self.assertIsNone(report["totals"]["pass@1"])
        self.assertIn("# RTLLM v2.0 report", driver.render_markdown(report))


class SummaryTests(unittest.TestCase):
    def test_repair_rounds_and_family_come_from_the_final_round(self):
        row = make_design_result(
            "adder_8bit", "Arithmetic/Adder", [False], ["port_mismatch"], rounds_per_sample=3
        ).to_dict()
        summary = driver.summarize_row(row)
        self.assertEqual(summary["repair_rounds_used"], 2)
        self.assertEqual(summary["failure_family"], "port_mismatch")
        self.assertFalse(summary["func_pass"])
        self.assertTrue(summary["syntax_pass"])

    def test_a_sample_whose_model_call_failed_is_bucketed_as_llm_error(self):
        sample = SampleResult(
            design="alu",
            sample=0,
            syntax_pass=False,
            func_pass=False,
            func_pass_strict=False,
            rounds=[],
            contract=None,
            final_rtl="",
        )
        row = DesignResult(design="alu", category="Arithmetic/ALU", samples=[sample]).to_dict()
        row["samples"][0]["llm_error"] = "backend timeout"
        self.assertEqual(driver.summarize_row(row)["failure_family"], driver.LLM_ERROR_FAMILY)

    def test_a_driver_error_row_counts_as_a_failed_design_not_as_no_samples(self):
        row = {
            "design": "alu",
            "category": "Arithmetic/ALU",
            "n_samples": 2,
            "syntax_success": 0,
            "func_success": 0,
            "samples": [],
            "error": "RuntimeError: boom",
        }
        report = driver.build_report([row], mode="llm", k=1, selected=["alu"])
        self.assertEqual(report["totals"]["designs"], 1)
        self.assertEqual(report["totals"]["pass@1"], 0.0)
        self.assertEqual(report["failure_families"], {driver.DRIVER_ERROR_FAMILY: 1})


# --------------------------------------------------------------------------- #
# end-to-end driver behaviour
# --------------------------------------------------------------------------- #


class ReferenceModeTests(DriverTestCase):
    def test_reference_mode_never_builds_an_llm_client(self):
        seen: "list[str]" = []

        def fake_evaluate_reference(design, workdir, **kwargs):
            seen.append(design.name)
            Path(workdir).mkdir(parents=True, exist_ok=True)
            return make_sim(design.name, func=design.name != "ccc_fifo",
                            family=None if design.name != "ccc_fifo" else "functional_mismatch")

        no_llm = mock.Mock(side_effect=AssertionError("--reference must not build an LLM client"))
        handler_before = signal.getsignal(signal.SIGINT)
        with self.patch_discovery(), mock.patch.object(
            rtllm_bench, "evaluate_reference", fake_evaluate_reference
        ), mock.patch.object(driver, "build_llm_client", no_llm), mock.patch.object(
            rtllm_agent, "run_design", mock.Mock(side_effect=AssertionError("no agent in reference mode"))
        ):
            code, _ = run_main(["--benchmark", str(self.bench), "--out-dir", str(self.out), "--reference"])

        self.assertEqual(code, 0)
        no_llm.assert_not_called()
        self.assertEqual(sorted(seen), sorted(self.design_names))
        self.assertIs(signal.getsignal(signal.SIGINT), handler_before)

        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["mode"], "reference")
        self.assertIsNone(report["backend"])
        self.assertIsNone(report["model"])
        self.assertEqual(report["totals"]["designs"], 3)
        self.assertEqual(report["totals"]["designs_func_success"], 2)
        self.assertEqual(len(read_jsonl(self.out / "results.jsonl")), 3)
        self.assertTrue((self.out / "report.md").exists())
        self.assertIn("reference", (self.out / "report.md").read_text(encoding="utf-8"))

    def test_reference_mode_writes_per_design_artifacts(self):
        def fake_evaluate_reference(design, workdir, **kwargs):
            return make_sim(design.name)

        with self.patch_discovery(), mock.patch.object(
            rtllm_bench, "evaluate_reference", fake_evaluate_reference
        ), mock.patch.object(driver, "build_llm_client", mock.Mock(side_effect=AssertionError)):
            run_main(
                ["--benchmark", str(self.bench), "--out-dir", str(self.out), "--reference",
                 "--designs", "aaa_adder"]
            )

        design_dir = self.out / "designs" / "aaa_adder"
        self.assertEqual((design_dir / "compile.log").read_text(encoding="utf-8"), "compile log for aaa_adder")
        self.assertEqual((design_dir / "sim.log").read_text(encoding="utf-8"), "sim log for aaa_adder")
        trace = json.loads((design_dir / "trace.json").read_text(encoding="utf-8"))
        self.assertEqual(trace["design"], "aaa_adder")
        self.assertEqual(trace["mode"], "reference")
        self.assertTrue((design_dir / "rtl.v").exists())


class LlmModeTests(DriverTestCase):
    def _argv(self, *extra: str) -> "list[str]":
        return ["--benchmark", str(self.bench), "--out-dir", str(self.out), *extra]

    def test_missing_llm_backend_exits_2_instead_of_scoring_zero(self):
        with self.patch_discovery(), mock.patch.object(
            driver, "build_llm_client", return_value=None
        ), mock.patch.object(driver, "missing_llm_reason", return_value="no backend for you"), mock.patch.object(
            rtllm_agent, "run_design", mock.Mock(side_effect=AssertionError("must not run"))
        ):
            code, output = run_main(self._argv())
        self.assertEqual(code, 2)
        self.assertIn("no backend for you", output)
        self.assertFalse((self.out / "report.json").exists())

    def test_results_jsonl_is_appended_as_each_design_finishes(self):
        results_path = self.out / "results.jsonl"
        observed: "list[int]" = []

        def hook(design: RtllmDesign) -> DesignResult:
            observed.append(len(read_jsonl(results_path)) if results_path.exists() else 0)
            return make_design_result(design.name, design.category, [True])

        run_patch = self.patch_run_design(hook)
        with self.patch_discovery(), run_patch, mock.patch.object(
            driver, "build_llm_client", return_value=object()
        ):
            code, _ = run_main(self._argv())

        self.assertEqual(code, 0)
        # Design i sees the i rows already flushed by its predecessors.
        self.assertEqual(observed, [0, 1, 2])
        rows = read_jsonl(results_path)
        self.assertEqual([row["design"] for row in rows], list(self.design_names))
        self.assertTrue(all(row["mode"] == "llm" for row in rows))

    def test_resume_skips_recorded_designs_and_appends_the_rest(self):
        first = self.patch_run_design()
        with self.patch_discovery(), first, mock.patch.object(driver, "build_llm_client", return_value=object()):
            run_main(self._argv("--designs", "aaa_adder"))
        self.assertEqual(first.calls, ["aaa_adder"])  # type: ignore[attr-defined]
        self.assertEqual(len(read_jsonl(self.out / "results.jsonl")), 1)

        second = self.patch_run_design()
        with self.patch_discovery(), second, mock.patch.object(driver, "build_llm_client", return_value=object()):
            code, output = run_main(self._argv("--resume"))

        self.assertEqual(code, 0)
        self.assertEqual(second.calls, ["bbb_counter", "ccc_fifo"])  # type: ignore[attr-defined]
        self.assertIn("resume: 1 designs already done", output)
        rows = read_jsonl(self.out / "results.jsonl")
        self.assertEqual([row["design"] for row in rows], list(self.design_names))
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["completed_designs"], 3)
        self.assertEqual(report["resumed_designs"], 1)

    def test_resume_refuses_to_mix_reference_rows_with_agent_rows(self):
        def fake_evaluate_reference(design, workdir, **kwargs):
            return make_sim(design.name)

        with self.patch_discovery(), mock.patch.object(rtllm_bench, "evaluate_reference", fake_evaluate_reference):
            run_main(self._argv("--reference", "--designs", "aaa_adder"))

        with self.patch_discovery(), self.patch_run_design(), mock.patch.object(
            driver, "build_llm_client", return_value=object()
        ):
            with self.assertRaises(SystemExit) as ctx:
                run_main(self._argv("--resume"))
        self.assertIn("mode mismatch", str(ctx.exception))

    def test_a_fresh_run_preserves_the_previous_results_file(self):
        with self.patch_discovery(), self.patch_run_design(), mock.patch.object(
            driver, "build_llm_client", return_value=object()
        ):
            run_main(self._argv("--designs", "aaa_adder"))
            run_main(self._argv("--designs", "bbb_counter"))
        self.assertTrue((self.out / "results.jsonl.prev").exists())
        self.assertEqual([row["design"] for row in read_jsonl(self.out / "results.jsonl")], ["bbb_counter"])

    def test_limit_and_exclude_narrow_the_selection(self):
        patcher = self.patch_run_design()
        with self.patch_discovery(), patcher, mock.patch.object(driver, "build_llm_client", return_value=object()):
            run_main(self._argv("--exclude", "aaa_adder", "--limit", "1"))
        self.assertEqual(patcher.calls, ["bbb_counter"])  # type: ignore[attr-defined]

    def test_a_design_that_raises_is_recorded_and_the_sweep_continues(self):
        def hook(design: RtllmDesign) -> DesignResult:
            if design.name == "bbb_counter":
                raise RuntimeError("agent exploded")
            return make_design_result(design.name, design.category, [True])

        with self.patch_discovery(), self.patch_run_design(hook), mock.patch.object(
            driver, "build_llm_client", return_value=object()
        ):
            code, _ = run_main(self._argv())

        self.assertEqual(code, 0)
        rows = {row["design"]: row for row in read_jsonl(self.out / "results.jsonl")}
        self.assertEqual(len(rows), 3)
        self.assertIn("agent exploded", rows["bbb_counter"]["error"])
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["totals"]["designs_func_success"], 2)
        self.assertEqual(report["failure_families"].get(driver.DRIVER_ERROR_FAMILY), 1)

    def test_samples_flag_drives_k_and_the_agent_config(self):
        seen: "list[rtllm_agent.RtllmAgentConfig]" = []

        def fake_run_design(design, client, config, workdir):
            seen.append(config)
            return make_design_result(design.name, design.category, [True, False, False, False])

        with self.patch_discovery(), mock.patch.object(
            rtllm_agent, "run_design", fake_run_design
        ), mock.patch.object(driver, "build_llm_client", return_value=object()):
            run_main(self._argv("--samples", "4", "--max-repair-rounds", "3", "--no-plan",
                                "--evidence-policy", "none", "--no-shims", "--workers", "2"))

        self.assertEqual(len(seen), 3)
        config = seen[0]
        self.assertEqual(config.samples, 4)
        self.assertEqual(config.max_repair_rounds, 3)
        self.assertFalse(config.plan)
        self.assertFalse(config.apply_shims)
        self.assertEqual(config.evidence_policy, "none")

        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["totals"]["k"], 4)
        self.assertAlmostEqual(report["totals"]["pass@1"], 0.25)
        self.assertAlmostEqual(report["totals"]["pass@k"], 1.0)
        self.assertEqual(report["agent_config"]["samples"], 4)


if __name__ == "__main__":
    unittest.main()
