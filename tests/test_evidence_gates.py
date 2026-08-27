"""Every tier must refuse to pass on an empty examination.

The workflow's original definition of success was the absence of a failure: `final_status`
returned pass when no required phase reported fail, and `PhaseResult` recorded no measure
of how much any phase had examined. A tier that compared 3600 elements and one that
compared none were indistinguishable. These tests pin the gates that close that gap.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig
from c2hlsc_agent.leveri_testgen import generate_leveri_testbenches

SOURCE = "void inc(const int a[4], int out[4]) {\n  for (int i = 0; i < 4; ++i) out[i] = a[i] + 1;\n}\n"


def _analysis(num_tests: int = 100, source: str = SOURCE, top: str = "inc"):
    work = Path(tempfile.mkdtemp())
    (work / "in.c").write_text(source, encoding="utf-8")
    config = AgentConfig(top=top, input_files=[work / "in.c"], num_tests=num_tests)
    return analyze_source(work / "in.c", top, config), config


def _errors(analysis) -> list[str]:
    return [item.code for item in analysis.diagnostics.items if item.severity == "error"]


class OracleEvidenceTest(unittest.TestCase):
    def test_unobservable_top_is_an_error(self) -> None:
        """A void top with no output argument would pass for any implementation."""

        analysis, _ = _analysis(
            source="void sink(const int a[4]) {\n  volatile int t = 0;\n  for (int i = 0; i < 4; ++i) t += a[i];\n}\n",
            top="sink",
        )
        self.assertIn("nothing-to-compare", _errors(analysis))

    def test_multidimensional_output_is_observable(self) -> None:
        """`table[i][j] = ...` is a write; missing it emptied the compare set."""

        analysis, _ = _analysis(
            source="void mm(char s[8], int table[8][8]) {\n"
            "  for (int i = 0; i < 8; ++i) for (int j = 0; j < 8; ++j) table[i][j] = s[i] + j;\n}\n",
            top="mm",
        )
        self.assertEqual(_errors(analysis), [])
        table = next(arg for arg in analysis.function.args if arg.name == "table")
        self.assertEqual(table.direction, "output")

    def test_zero_tests_is_an_error(self) -> None:
        analysis, _ = _analysis(num_tests=0)
        self.assertIn("no-tests-scheduled", _errors(analysis))

    def test_ordinary_design_is_accepted(self) -> None:
        analysis, _ = _analysis()
        self.assertEqual(_errors(analysis), [])


class TraceEvidenceTest(unittest.TestCase):
    """The dynamic tier must not report consistency having compared nothing."""

    def setUp(self) -> None:
        analysis, config = _analysis()
        self.work = Path(tempfile.mkdtemp())
        self.compare = self.work / "leveri_compare.py"
        self.compare.write_text(generate_leveri_testbenches(analysis, config).compare_script, encoding="utf-8")

    def _run(self, header: str, roles: str, rows: list[str]) -> subprocess.CompletedProcess:
        body = header + "\n" + roles + "\n" + "".join(rows)
        for name in ("golden.csv", "hls.csv"):
            (self.work / name).write_text(body, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(self.compare), str(self.work / "golden.csv"), str(self.work / "hls.csv")],
            capture_output=True,
            text=True,
        )

    def test_trace_with_outputs_and_cycles_passes(self) -> None:
        header = "cycle," + ",".join([f"a[{i}]" for i in range(4)] + [f"out[{i}]" for i in range(4)])
        roles = "meta," + ",".join(["in"] * 4 + ["out"] * 4)
        result = self._run(header, roles, ["0,1,2,3,4,2,3,4,5\n"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4 output columns", result.stdout)

    def test_trace_without_output_columns_fails(self) -> None:
        header = "cycle," + ",".join(f"a[{i}]" for i in range(4))
        roles = "meta," + ",".join(["in"] * 4)
        result = self._run(header, roles, ["0,1,2,3,4\n"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("insufficient evidence", result.stderr)

    def test_trace_without_cycles_fails(self) -> None:
        header = "cycle," + ",".join([f"a[{i}]" for i in range(4)] + [f"out[{i}]" for i in range(4)])
        roles = "meta," + ",".join(["in"] * 4 + ["out"] * 4)
        result = self._run(header, roles, [])
        self.assertEqual(result.returncode, 1)
        self.assertIn("insufficient evidence", result.stderr)


if __name__ == "__main__":
    unittest.main()
