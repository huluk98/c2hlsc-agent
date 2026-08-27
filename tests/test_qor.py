"""Tests for the QoR improver (qor.py parsers/scoring/reports + qor_optimizer loop)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.cli import build_parser
from c2hlsc_agent.config import AgentConfig
from c2hlsc_agent.convert import generate_hls_sources
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.hls_project import write_project
from c2hlsc_agent.hls_runner import run_vitis
from c2hlsc_agent.qor import (
    CSYNTH_XML_RELPATH,
    QoRMetrics,
    objective_score,
    parse_csynth_xml,
    parse_sta_report,
    parse_yosys_area,
    qor_delta,
    render_latex_table,
    render_markdown,
)
from c2hlsc_agent.qor_optimizer import (
    PRE_QOR_BACKUP,
    _pipeline_innermost_loops,
    optimize_project,
)


def _csynth_xml(latency: int = 230, interval: int = 231, lut: int = 800, ff: int = 450, dsp: int = 3,
                bram: int = 2, est_clock: float = 7.3) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<profile>
  <PerformanceEstimates>
    <SummaryOfTimingAnalysis>
      <unit>ns</unit>
      <TargetClockPeriod>10.00</TargetClockPeriod>
      <EstimatedClockPeriod>{est_clock}</EstimatedClockPeriod>
    </SummaryOfTimingAnalysis>
    <SummaryOfOverallLatency>
      <Best-caseLatency>{latency}</Best-caseLatency>
      <Worst-caseLatency>{latency}</Worst-caseLatency>
      <Interval-min>{interval}</Interval-min>
      <Interval-max>{interval}</Interval-max>
    </SummaryOfOverallLatency>
  </PerformanceEstimates>
  <AreaEstimates>
    <Resources>
      <BRAM_18K>{bram}</BRAM_18K>
      <DSP>{dsp}</DSP>
      <FF>{ff}</FF>
      <LUT>{lut}</LUT>
      <URAM>0</URAM>
    </Resources>
    <AvailableResources>
      <BRAM_18K>624</BRAM_18K>
      <DSP>1728</DSP>
      <FF>460800</FF>
      <LUT>230400</LUT>
    </AvailableResources>
  </AreaEstimates>
</profile>
"""


YOSYS_RPT = """
   Number of cells:                711
        6     7.98   AOI211_X1
       34  153.748   DFF_X1

   Chip area for module '\\cnn_conv3x3': 958.398000
     of which used for sequential elements: 153.748000 (16.04%)
"""

STA_RPT = """
                              5.849   slack (MET)
worst slack max 5.85
worst slack min 0.05
Group     Internal Switching Leakage   Total
Total     1.00e-09 2.00e-10  3.00e-11  1.23e-09 100.0%
"""

VECTOR_ADD = """#include <stdint.h>

void vector_add(const int32_t *a, const int32_t *b, int32_t *out, int n) {
  for (int i = 0; i < 16; ++i) {
    out[i] = a[i] + b[i];
  }
}
"""


class SeqLLM:
    def __init__(self, responses: list[str], model: str = "fake") -> None:
        self.responses = list(responses)
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> str:
        self.calls.append((system, user))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class QoRParsingTests(unittest.TestCase):
    def test_parse_csynth_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "csynth.xml"
            path.write_text(_csynth_xml(), encoding="utf-8")
            m = parse_csynth_xml(path)
        self.assertEqual(m.latency_worst, 230)
        self.assertEqual(m.interval_max, 231)
        self.assertEqual(m.lut, 800)
        self.assertEqual(m.dsp, 3)
        self.assertEqual(m.target_clock_ns, 10.0)
        self.assertAlmostEqual(m.estimated_clock_ns, 7.3)
        self.assertTrue(m.timing_met)
        self.assertEqual(m.available["LUT"], 230400)

    def test_parse_local_ppa_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            yosys = Path(tmp) / "yosys_area.rpt"
            yosys.write_text(YOSYS_RPT, encoding="utf-8")
            sta = Path(tmp) / "sta_report.txt"
            sta.write_text(STA_RPT, encoding="utf-8")
            m = parse_yosys_area(yosys)
            m = parse_sta_report(sta, m)
        self.assertAlmostEqual(m.yosys_area_um2, 958.398)
        self.assertEqual(m.yosys_cells, 711)
        self.assertAlmostEqual(m.sta_worst_slack_max_ns, 5.85)
        self.assertAlmostEqual(m.sta_total_power_w, 1.23e-09)

    def test_delta_and_objectives(self):
        base = QoRMetrics(latency_worst=200, lut=1000, ff=500, dsp=2, bram=1,
                          target_clock_ns=10.0, estimated_clock_ns=8.0)
        cand = QoRMetrics(latency_worst=100, lut=1400, ff=600, dsp=4, bram=1,
                          target_clock_ns=10.0, estimated_clock_ns=9.0)
        delta = qor_delta(base, cand)
        self.assertEqual(delta["latency_worst"]["delta"], -100)
        self.assertAlmostEqual(delta["latency_worst"]["pct"], -50.0)
        self.assertEqual(objective_score(base, "latency"), 200.0)
        self.assertLess(objective_score(cand, "latency"), objective_score(base, "latency"))
        self.assertGreater(objective_score(cand, "area"), objective_score(base, "area"))
        balanced = objective_score(cand, "balanced", base)
        self.assertIsNotNone(balanced)
        self.assertLess(balanced, 1.0)  # 2x latency win outweighs the area increase
        self.assertIsNone(objective_score(QoRMetrics(), "latency"))

    def test_render_latex_and_markdown(self):
        base = QoRMetrics(latency_worst=200, lut=1000)
        cand = QoRMetrics(latency_worst=100, lut=1200)
        delta = qor_delta(base, cand)
        tex = render_latex_table(delta, caption="cap")
        self.assertIn("\\toprule", tex)
        self.assertIn("Latency (worst, cycles) & 200 & 100 & -50.0", tex)
        md = render_markdown(delta, "t")
        self.assertIn("| Latency (worst, cycles) | 200 | 100 | -50.0 |", md)


class PipelineHeuristicTests(unittest.TestCase):
    def test_pipelines_only_innermost_loop(self):
        src = (
            "void f(int *a) {\n"
            "  for (int i = 0; i < 4; ++i) {\n"
            "    for (int j = 0; j < 4; ++j) {\n"
            "      a[i] += j;\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        out = _pipeline_innermost_loops(src)
        self.assertIsNotNone(out)
        self.assertEqual(out.count("#pragma HLS PIPELINE II=1"), 1)
        lines = out.splitlines()
        j_line = next(i for i, l in enumerate(lines) if "int j" in l)
        self.assertIn("PIPELINE", lines[j_line + 1])

    def test_skips_already_pipelined_and_no_loops(self):
        src = "void f(int *a) {\n  for (int i = 0; i < 4; ++i) {\n#pragma HLS PIPELINE\n    a[i] = i;\n  }\n}\n"
        self.assertIsNone(_pipeline_innermost_loops(src))
        self.assertIsNone(_pipeline_innermost_loops("int f(void) { return 1; }\n"))


class RunVitisUptoTests(unittest.TestCase):
    def test_upto_csynth_skips_cosim(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("c2hlsc_agent.hls_runner._run_vitis_phase",
                            side_effect=lambda d, p, r, **kw: PhaseResult(p, "pass")), \
                 mock.patch("c2hlsc_agent.hls_runner.shutil.which", return_value="/bin/vitis_hls"):
                phases = run_vitis(Path(tmp), True, upto="csynth")
        self.assertEqual(phases["csynth"].status, "pass")
        self.assertEqual(phases["cosim"].status, "skipped")

    def test_upto_validated(self):
        with self.assertRaises(ValueError):
            run_vitis(Path("/tmp"), False, upto="bogus")


def _optimized_cpp(latency_hint: str) -> str:
    return (
        "```cpp\n#include \"hls_top.hpp\"\n\n"
        f"// optimized: {latency_hint}\n"
        "void vector_add(const int32_t *a, const int32_t *b, int32_t *out, int n) {\n"
        "  for (int i = 0; i < 16; ++i) {\n"
        "#pragma HLS UNROLL factor=4\n"
        "    out[i] = a[i] + b[i];\n"
        "  }\n"
        "}\n```"
    )


class OptimizerLoopTests(unittest.TestCase):
    def _project(self, tmp: Path) -> tuple[Path, object, AgentConfig]:
        config = AgentConfig()
        source = tmp / "input.c"
        source.write_text(VECTOR_ADD, encoding="utf-8")
        config.input_files = [source]
        config.top = "vector_add"
        analysis = analyze_source(source, "vector_add", config)
        out_dir = tmp / "out"
        write_project(out_dir, analysis, generate_hls_sources(analysis, config), config)
        # pre-existing baseline synthesis report
        xml = out_dir / CSYNTH_XML_RELPATH
        xml.parent.mkdir(parents=True, exist_ok=True)
        xml.write_text(_csynth_xml(latency=230), encoding="utf-8")
        return out_dir, analysis, config

    @staticmethod
    def _fake_run_vitis(latencies: list[int]):
        """Each csynth-scoring call writes the next latency into the candidate's report."""

        def fake(project_dir: Path, run_requested: bool, remote=None, upto="cosim"):
            latency = latencies.pop(0) if latencies else 999
            xml = project_dir / CSYNTH_XML_RELPATH
            xml.parent.mkdir(parents=True, exist_ok=True)
            xml.write_text(_csynth_xml(latency=latency), encoding="utf-8")
            return {
                "csim": PhaseResult("csim", "pass"),
                "csynth": PhaseResult("csynth", "pass"),
                "cosim": PhaseResult("cosim", "skipped"),
            }

        return fake

    @staticmethod
    def _passing_state() -> VerificationState:
        state = VerificationState()
        for phase in ("software_equivalence", "csim", "csynth", "cosim"):
            state.add_phase(PhaseResult(phase, "pass"))
        return state

    def test_winner_accepted_and_reports_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            original = (out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8")
            llm = SeqLLM([_optimized_cpp("fast")])
            # candidate 0 = deterministic pipeline (latency 200), candidate 1 = llm (latency 120)
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([200, 120])), \
                 mock.patch("c2hlsc_agent.qor_optimizer.verify_project", return_value=self._passing_state()):
                outcome = optimize_project(out_dir, analysis, config, llm, None,
                                           objective="latency", iterations=1)
            self.assertTrue(outcome.accepted)
            self.assertEqual(outcome.winner_index, 1)
            self.assertIn("UNROLL", (out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8"))
            self.assertEqual((out_dir / "src" / PRE_QOR_BACKUP).read_text(encoding="utf-8"), original)
            report = json.loads((out_dir / "qor_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["objective"], "latency")
            self.assertEqual(len(report["candidates"]), 2)
            self.assertTrue((out_dir / "qor_table.tex").exists())
            self.assertIn("Latency", (out_dir / "qor_table.tex").read_text(encoding="utf-8"))

    def test_no_improvement_keeps_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            original = (out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8")
            llm = SeqLLM([_optimized_cpp("slow")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([300, 400])):
                outcome = optimize_project(out_dir, analysis, config, llm, None,
                                           objective="latency", iterations=1)
            self.assertFalse(outcome.accepted)
            self.assertIsNone(outcome.winner_index)
            self.assertIn("No candidate improved", outcome.summary)
            self.assertEqual((out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8"), original)
            self.assertTrue((out_dir / "qor_report.json").exists())

    def test_final_ladder_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            original = (out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8")
            failing = VerificationState()
            failing.add_phase(PhaseResult("software_equivalence", "pass"))
            failing.add_phase(PhaseResult("csim", "pass"))
            failing.add_phase(PhaseResult("csynth", "pass"))
            failing.add_phase(PhaseResult("cosim", "fail", summary="mismatch"))
            llm = SeqLLM([_optimized_cpp("fast but wrong")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([200, 120])), \
                 mock.patch("c2hlsc_agent.qor_optimizer.verify_project", return_value=failing):
                outcome = optimize_project(out_dir, analysis, config, llm, None,
                                           objective="latency", iterations=1)
            self.assertFalse(outcome.accepted)
            self.assertTrue(outcome.rolled_back)
            self.assertEqual((out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8"), original)
            report = json.loads((out_dir / "qor_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["candidates"][outcome.winner_index]["status"], "final_ladder_fail")

    def test_equiv_failing_candidate_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            llm = SeqLLM([_optimized_cpp("broken")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "fail", summary="Mismatch test=0")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([])):
                outcome = optimize_project(out_dir, analysis, config, llm, None,
                                           objective="latency", iterations=1)
            self.assertFalse(outcome.accepted)
            self.assertTrue(all(c.status == "equiv_fail" for c in outcome.candidates))


class ReviewFixTests(unittest.TestCase):
    """Regression tests for the adversarially-verified review findings."""

    def test_malformed_csynth_xml_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "csynth.xml"
            bad.write_text("<profile><unclosed>", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                parse_csynth_xml(bad)

    def test_yosys_modern_format_and_real_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpt = Path(tmp) / "y.rpt"
            rpt.write_text("      759  958.398 cells\n\n   Chip area for module '\\top': 958.398000\n", encoding="utf-8")
            m = parse_yosys_area(rpt)
        self.assertEqual(m.yosys_cells, 759)
        real = Path("build/cnn_3x3/syn/yosys_area.rpt")
        if real.exists():
            rm = parse_yosys_area(real)
            self.assertIsNotNone(rm.yosys_cells)
            self.assertIsNotNone(rm.yosys_area_um2)

    def test_sta_units_declaration_is_not_power(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpt = Path(tmp) / "sta.txt"
            rpt.write_text("worst slack max 5.85\n power 1nW\n", encoding="utf-8")
            m = parse_sta_report(rpt)
        self.assertIsNone(m.sta_total_power_w)  # 'power 1nW' is report_units, not a measurement
        self.assertAlmostEqual(m.sta_worst_slack_max_ns, 5.85)

    def test_pipeline_heuristic_handles_comments(self):
        src = (
            "/* for (int x = 0; x < 4; ++x) {  — commented out\n*/\n"
            "void f(int *a) {\n"
            "  for (int i = 0; i < 4; ++i) { // hot loop\n"
            "    a[i] = i;\n"
            "  }\n"
            "}\n"
        )
        out = _pipeline_innermost_loops(src)
        self.assertIsNotNone(out)
        self.assertEqual(out.count("#pragma HLS PIPELINE II=1"), 1)
        self.assertNotIn("PIPELINE II=1\n*/", out)  # never inserted inside the block comment

    def test_remote_push_excludes_qor(self):
        from c2hlsc_agent.remote import RemoteVitis

        remote = RemoteVitis(host="u@h")
        captured = {}

        def fake_run_command(command, cwd, phase, timeout=120):
            captured["cmd"] = command
            return PhaseResult(phase, "pass")

        import subprocess as sp
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("c2hlsc_agent.remote.subprocess.run",
                            return_value=sp.CompletedProcess([], 0, "", "")), \
                 mock.patch("c2hlsc_agent.remote.run_command", side_effect=fake_run_command):
                remote.push(Path(tmp))
        self.assertIn(".qor/", captured["cmd"])


class OptimizerReviewFixTests(OptimizerLoopTests):
    """End-to-end regressions on the optimizer loop for the review findings."""

    def test_stale_baseline_report_triggers_resynthesis(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            # age the report so it predates src/hls_top.cpp -> must NOT be trusted
            xml = out_dir / CSYNTH_XML_RELPATH
            old = xml.stat().st_mtime - 1000
            os.utime(xml, (old, old))
            calls = {"n": 0}

            def fake_run_vitis(project_dir, run_requested, remote=None, upto="cosim"):
                calls["n"] += 1
                p = project_dir / CSYNTH_XML_RELPATH
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_csynth_xml(latency=230), encoding="utf-8")
                return {"csim": PhaseResult("csim", "pass"), "csynth": PhaseResult("csynth", "pass"),
                        "cosim": PhaseResult("cosim", "skipped")}

            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=fake_run_vitis):
                optimize_project(out_dir, analysis, config, None, None, objective="latency", iterations=0)
            # first run_vitis call was the baseline re-synthesis (plus one per candidate)
            self.assertGreaterEqual(calls["n"], 1)

    def test_backup_not_clobbered_on_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            original = (out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8")
            llm = SeqLLM([_optimized_cpp("v1")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([200, 120])), \
                 mock.patch("c2hlsc_agent.qor_optimizer.verify_project", return_value=self._passing_state()):
                optimize_project(out_dir, analysis, config, llm, None, objective="latency", iterations=1)
            # second optimize run on the already-optimized project
            xml = out_dir / CSYNTH_XML_RELPATH
            xml.write_text(_csynth_xml(latency=120), encoding="utf-8")
            llm2 = SeqLLM([_optimized_cpp("v2 different") .replace("factor=4", "factor=8")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([110, 100])), \
                 mock.patch("c2hlsc_agent.qor_optimizer.verify_project", return_value=self._passing_state()):
                optimize_project(out_dir, analysis, config, llm2, None, objective="latency", iterations=1)
            # backup still holds the TRUE original, not run 1's optimized source
            self.assertEqual((out_dir / "src" / PRE_QOR_BACKUP).read_text(encoding="utf-8"), original)

    def test_no_cosim_winner_keeps_candidate_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            llm = SeqLLM([_optimized_cpp("fast")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([200, 120])):
                outcome = optimize_project(out_dir, analysis, config, llm, None,
                                           objective="latency", iterations=1, cosim_winner=False)
            self.assertTrue(outcome.accepted)
            # delta must reflect the candidate's 120, not the baseline xml's 230
            self.assertEqual(outcome.delta["latency_worst"]["candidate"], 120)
            self.assertLess(outcome.delta["latency_worst"]["delta"], 0)

    def test_rollback_deletes_stale_report_and_cli_maps_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            failing = VerificationState()
            for phase in ("software_equivalence", "csim", "csynth"):
                failing.add_phase(PhaseResult(phase, "pass"))
            failing.add_phase(PhaseResult("cosim", "fail", summary="mismatch"))
            llm = SeqLLM([_optimized_cpp("fast but wrong")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([200, 120])), \
                 mock.patch("c2hlsc_agent.qor_optimizer.verify_project", return_value=failing):
                outcome = optimize_project(out_dir, analysis, config, llm, None,
                                           objective="latency", iterations=1)
            self.assertTrue(outcome.rolled_back)
            # the rejected candidate's report was removed so the next run re-baselines
            self.assertFalse((out_dir / CSYNTH_XML_RELPATH).exists())

    def test_losing_candidates_cleaned_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            llm = SeqLLM([_optimized_cpp("fast")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([200, 120])), \
                 mock.patch("c2hlsc_agent.qor_optimizer.verify_project", return_value=self._passing_state()):
                outcome = optimize_project(out_dir, analysis, config, llm, None,
                                           objective="latency", iterations=1)
            qor_dir = out_dir / ".qor"
            dirs = sorted(p.name for p in qor_dir.iterdir() if p.is_dir())
            self.assertEqual(dirs, [f"cand_{outcome.winner_index}"])  # losers removed

    def test_stale_qor_table_removed_on_no_improvement_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            (out_dir / "qor_table.tex").write_text("stale", encoding="utf-8")
            llm = SeqLLM([_optimized_cpp("slow")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([300, 400])):
                optimize_project(out_dir, analysis, config, llm, None, objective="latency", iterations=1)
            self.assertFalse((out_dir / "qor_table.tex").exists())

    def test_history_carries_pragma_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            llm = SeqLLM([_optimized_cpp("a"), _optimized_cpp("b")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis", side_effect=self._fake_run_vitis([300, 400, 500])):
                optimize_project(out_dir, analysis, config, llm, None, objective="latency", iterations=2)
            # the second LLM call's prompt lists the first candidates' pragma strategies
            _system, user = llm.calls[1]
            self.assertIn("Already-tried candidates", user)
            self.assertIn("UNROLL", user)
            self.assertIn("attempt #2", user)


class TargetEvaluationTests(unittest.TestCase):
    def test_targets_met_and_gaps(self):
        from c2hlsc_agent.qor import PPATargets, evaluate_targets

        targets = PPATargets(max_latency_cycles=200, min_slack_ns=0.5, max_area_um2=1000.0, max_power_w=2e-3)
        self.assertTrue(targets.specified)
        self.assertTrue(targets.needs_local_ppa)
        good = QoRMetrics(latency_worst=150, sta_worst_slack_max_ns=1.2, yosys_area_um2=900.0, sta_total_power_w=1e-3)
        met, gaps, gap = evaluate_targets(good, targets)
        self.assertTrue(met)
        self.assertEqual(gaps, [])
        self.assertEqual(gap, 0.0)
        bad = QoRMetrics(latency_worst=300, sta_worst_slack_max_ns=0.1, yosys_area_um2=1500.0, sta_total_power_w=5e-3)
        met, gaps, gap = evaluate_targets(bad, targets)
        self.assertFalse(met)
        self.assertEqual(len(gaps), 4)
        self.assertGreater(gap, 0.0)

    def test_missing_measurement_counts_as_unmet(self):
        from c2hlsc_agent.qor import PPATargets, evaluate_targets

        targets = PPATargets(min_slack_ns=0.5)
        met, gaps, gap = evaluate_targets(QoRMetrics(latency_worst=100), targets)
        self.assertFalse(met)
        self.assertIn("no measurement", gaps[0])
        self.assertEqual(gap, 1.0)

    def test_latency_only_targets_do_not_need_local_ppa(self):
        from c2hlsc_agent.qor import PPATargets

        self.assertFalse(PPATargets(max_latency_cycles=100).needs_local_ppa)
        self.assertFalse(PPATargets().specified)


class LocalPPATests(unittest.TestCase):
    def test_scripts_reference_flow(self):
        from c2hlsc_agent.local_ppa import _sta_script, _yosys_script

        ys = _yosys_script([Path("/x/rtl/top.v")], "cnn_conv3x3", Path("/lib/n45.lib"), 10.0, Path("syn/net.v"))
        self.assertIn("read_verilog /x/rtl/top.v", ys)
        self.assertIn("dfflibmap -liberty /lib/n45.lib", ys)
        self.assertIn("abc -liberty /lib/n45.lib -D 10000", ys)
        self.assertIn("stat -liberty", ys)
        tcl = _sta_script(Path("/lib/n45.lib"), Path("syn/net.v"), "cnn_conv3x3", 10.0, "ap_clk")
        self.assertIn("create_clock -name ap_clk -period 10.0", tcl)
        self.assertIn("report_worst_slack -max", tcl)
        self.assertIn("report_power", tcl)
        self.assertIn("-group_path_count 3", tcl)  # non-deprecated OpenSTA flag

    def test_generate_cell_models_from_liberty(self):
        from c2hlsc_agent.local_ppa import generate_cell_models

        liberty = (
            'cell (INV_X1) {\n'
            '  pin (A) { direction : input; }\n'
            '  pin (ZN) { direction : output; function : "!A"; }\n'
            '}\n'
        )
        netlist = "module top(a, z);\n  INV_X1 u0 ( .A(a), .ZN(z) );\nendmodule\n"
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "n.lib"; lib.write_text(liberty, encoding="utf-8")
            net = Path(tmp) / "net.v"; net.write_text(netlist, encoding="utf-8")
            out = Path(tmp) / "cells_sim.v"
            n = generate_cell_models(lib, net, out)
            text = out.read_text(encoding="utf-8")
        self.assertEqual(n, 1)
        self.assertIn("module INV_X1(ZN, A);", text)
        self.assertIn("assign ZN = ~A;", text)

    def test_sta_failure_line_detects_critical_not_just_error(self):
        # OpenSTA aborts a report with "Critical <n>: ..." and can still exit 0, writing
        # every diagnostic to stdout while leaving stderr empty. Matching only "Error"
        # would treat the resulting partial report as valid measurements.
        from c2hlsc_agent.local_ppa import sta_failure_line

        critical = "==== WORST SETUP (max) PATHS ====\nCritical 242: TableModel.cc line 652, unsupported table axes\n"
        self.assertEqual(
            sta_failure_line(critical),
            "Critical 242: TableModel.cc line 652, unsupported table axes",
        )
        self.assertEqual(sta_failure_line("Error: no such command"), "Error: no such command")
        # Warnings are routine on a real liberty and must not invalidate the report.
        self.assertIsNone(sta_failure_line("Warning 1251: unsupported model axis."))
        self.assertIsNone(sta_failure_line("worst slack max 7.69\nworst slack min 0.07"))
        self.assertIsNone(sta_failure_line(""))

    def test_liberty_implicit_and_is_translated(self):
        # Liberty uses juxtaposition as AND. Nangate45 writes every NAND/AND/AOI/OAI cell
        # that way, so an untranslated "A1 A2" emits invalid Verilog and the gate-level
        # sim cannot compile. Whitespace has to be whitelisted by the unhandled-operator
        # guard, so this can only be caught by converting it, never by rejecting it.
        from c2hlsc_agent.local_ppa import _liberty_expr_to_verilog as to_verilog

        self.assertEqual(to_verilog("!(A1 A2)"), "~(A1 & A2)")  # NAND2_X1
        self.assertEqual(to_verilog("A1 A2"), "A1 & A2")  # AND2_X1 body
        self.assertEqual(to_verilog("A B C"), "A & B & C")  # 3-input
        self.assertEqual(to_verilog("!((A1 A2)+(B1 B2))"), "~((A1 & A2)|(B1 & B2))")  # AOI22
        # An explicitly separated AND must not gain a second operator.
        self.assertEqual(to_verilog("A * B"), "A & B")

    def test_generate_cell_models_compile_ready_for_nangate_style_nand(self):
        from c2hlsc_agent.local_ppa import generate_cell_models

        liberty = (
            'cell (NAND2_X1) {\n'
            '  pin (A1) { direction : input; }\n'
            '  pin (A2) { direction : input; }\n'
            '  pin (ZN) { direction : output; function : "!(A1 A2)"; }\n'
            '}\n'
        )
        netlist = "module top(a, b, z);\n  NAND2_X1 u0 ( .A1(a), .A2(b), .ZN(z) );\nendmodule\n"
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "n.lib"; lib.write_text(liberty, encoding="utf-8")
            net = Path(tmp) / "net.v"; net.write_text(netlist, encoding="utf-8")
            out = Path(tmp) / "cells_sim.v"
            modelled = generate_cell_models(lib, net, out)
            text = out.read_text(encoding="utf-8")
        # The cell must be modelled, not skipped, and the assignment must be legal Verilog.
        self.assertEqual(modelled, 1)
        self.assertIn("assign ZN = ~(A1 & A2);", text)

    def test_generate_cell_models_dff_qn_polarity(self):
        # QN must be the INVERTED state — the original gen_cell_models.py contract.
        from c2hlsc_agent.local_ppa import generate_cell_models

        liberty = (
            'cell (DFF_X1) {\n'
            '  ff ("IQ", "IQN") { next_state : "D"; clocked_on : "CK"; }\n'
            '  pin (D) { direction : input; }\n'
            '  pin (CK) { direction : input; }\n'
            '  pin (Q) { direction : output; function : "IQ"; }\n'
            '  pin (QN) { direction : output; function : "IQN"; }\n'
            '}\n'
        )
        netlist = "module top(d, ck, q, qn);\n  DFF_X1 u0 ( .D(d), .CK(ck), .Q(q), .QN(qn) );\nendmodule\n"
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "n.lib"; lib.write_text(liberty, encoding="utf-8")
            net = Path(tmp) / "net.v"; net.write_text(netlist, encoding="utf-8")
            out = Path(tmp) / "cells_sim.v"
            generate_cell_models(lib, net, out)
            text = out.read_text(encoding="utf-8")
        self.assertIn("reg IQ;", text)
        self.assertIn("IQ <= D;", text)
        self.assertIn("assign Q = IQ;", text)
        self.assertIn("assign QN = ~IQ;", text)

    def test_run_local_ppa_skips_without_rtl(self):
        from c2hlsc_agent.local_ppa import run_local_ppa

        with tempfile.TemporaryDirectory() as tmp:
            metrics, outcome = run_local_ppa(Path(tmp), "top", 10.0)
        self.assertIsNone(metrics)
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("no RTL", outcome.note)


def _fake_local_ppa_factory(slacks: list[float]):
    """Fake run_local_ppa: pops the next slack into the passed metrics."""

    from c2hlsc_agent.local_ppa import LocalPPAOutcome

    def fake(project_dir, top, clock_ns, liberty=None, sta_bin=None, clock_port="ap_clk",
             gate_sim=True, metrics=None, verbose=False):
        m = metrics or QoRMetrics()
        m.sta_worst_slack_max_ns = slacks.pop(0) if slacks else 9.9
        m.yosys_area_um2 = 900.0
        m.sta_total_power_w = 1e-3
        return m, LocalPPAOutcome(status="ok", gate_sim="pass")

    return fake


class TargetLoopTests(OptimizerLoopTests):
    def test_iterates_rounds_until_slack_target_met(self):
        from c2hlsc_agent.qor import PPATargets

        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            # round 0: det cand slack 0.5, llm cand slack 0.4 (det adopted, target unmet)
            # round 1: llm cand slack 1.2 -> target met, accepted
            slacks = [0.2, 0.5, 0.4, 1.2, 1.2]  # baseline, det, llm r0, llm r1, final
            variant = _optimized_cpp("v2").replace("factor=4", "factor=8")
            llm = SeqLLM([_optimized_cpp("r0"), variant])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis",
                            side_effect=self._fake_run_vitis([200, 210, 190])), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_local_ppa",
                            side_effect=_fake_local_ppa_factory(slacks)), \
                 mock.patch("c2hlsc_agent.qor_optimizer.verify_project", return_value=self._passing_state()):
                outcome = optimize_project(out_dir, analysis, config, llm, None,
                                           objective="latency", iterations=1,
                                           targets=PPATargets(min_slack_ns=1.0), max_rounds=3)
            self.assertTrue(outcome.accepted)
            self.assertTrue(outcome.targets_met)
            self.assertEqual(len(outcome.rounds), 2)  # round 0 adopted det, round 1 met target
            self.assertEqual(outcome.rounds[0]["adopted_candidate"], 0)
            # round-1 prompt carries the gap text AND the adopted working source
            _system, user = llm.calls[1]
            self.assertIn("PPA targets", user)
            self.assertIn("worst setup slack", user)
            self.assertIn("PIPELINE II=1", user)  # round 0's adopted deterministic source
            report = json.loads((out_dir / "qor_report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["targets_met"])
            self.assertEqual(report["targets"]["min_slack_ns"], 1.0)

    def test_no_progress_with_targets_reports_gaps(self):
        from c2hlsc_agent.qor import PPATargets

        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            # every candidate has WORSE slack than baseline -> no progress, stop
            slacks = [0.5, 0.2, 0.1]
            llm = SeqLLM([_optimized_cpp("worse")])
            with mock.patch("c2hlsc_agent.qor_optimizer.run_software_equivalence",
                            return_value=PhaseResult("software_equivalence", "pass")), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_vitis",
                            side_effect=self._fake_run_vitis([200, 210])), \
                 mock.patch("c2hlsc_agent.qor_optimizer.run_local_ppa",
                            side_effect=_fake_local_ppa_factory(slacks)):
                outcome = optimize_project(out_dir, analysis, config, llm, None,
                                           objective="latency", iterations=1,
                                           targets=PPATargets(min_slack_ns=1.0), max_rounds=3)
            self.assertFalse(outcome.accepted)
            self.assertIn("No candidate made progress", outcome.summary)
            self.assertIn("slack", outcome.summary)

    def test_baseline_already_meeting_targets_is_a_noop(self):
        from c2hlsc_agent.qor import PPATargets

        with tempfile.TemporaryDirectory() as tmp:
            out_dir, analysis, config = self._project(Path(tmp))
            with mock.patch("c2hlsc_agent.qor_optimizer.run_local_ppa",
                            side_effect=_fake_local_ppa_factory([5.0])):
                outcome = optimize_project(out_dir, analysis, config, None, None,
                                           objective="latency", iterations=0,
                                           targets=PPATargets(min_slack_ns=1.0), max_rounds=3)
            self.assertFalse(outcome.accepted)
            self.assertTrue(outcome.targets_met)
            self.assertIn("already meets", outcome.summary)


class OptimizeCliTests(unittest.TestCase):
    def test_parser_accepts_targets(self):
        args = build_parser().parse_args(
            ["optimize", "--project", "p", "--target-latency", "150", "--target-slack", "0.5",
             "--target-area", "1000", "--target-power", "2e-3", "--max-rounds", "3",
             "--local-ppa", "--liberty", "/lib/n45.lib", "--no-gate-sim"]
        )
        self.assertEqual(args.target_latency, 150)
        self.assertAlmostEqual(args.target_power, 2e-3)
        self.assertEqual(args.max_rounds, 3)
        self.assertTrue(args.local_ppa)

    def test_parser_accepts_optimize(self):
        args = build_parser().parse_args(
            ["optimize", "--project", "p", "--objective", "area", "--iterations", "2",
             "--vitis-ssh", "u@h", "--ppa-script", "syn/run_ppa.sh"]
        )
        self.assertEqual(args.command, "optimize")
        self.assertEqual(args.objective, "area")
        self.assertEqual(args.vitis_ssh, "u@h")


if __name__ == "__main__":
    unittest.main()
