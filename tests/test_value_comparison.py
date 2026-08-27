"""Behaviour of the generated comparison helpers.

These compile the helpers the testbench actually emits and run them, rather than asserting
on the generated text. The defects they cover were both invisible to string assertions:
``values_equal(inf, inf)`` returned false because the relative test computes inf - inf =
NaN, and every diagnostic value was cast to ``long long`` so a float divergence printed as
a pair of identical integers.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig
from c2hlsc_agent.testgen import generate_testbench

PROBE = r"""
int main() {
  const double inf = std::numeric_limits<double>::infinity();
  const double nan = std::numeric_limits<double>::quiet_NaN();
  int failures = 0;
  auto check = [&](const char* what, bool got, bool want) {
    if (got != want) { std::cerr << "FAIL " << what << "\n"; ++failures; }
  };
  check("inf==inf agrees", values_equal(inf, inf), true);
  check("-inf==-inf agrees", values_equal(-inf, -inf), true);
  check("inf vs -inf differs", values_equal(inf, -inf), false);
  check("nan==nan agrees", values_equal(nan, nan), true);
  check("nan vs finite differs", values_equal(nan, 1.0), false);
  check("close doubles agree", values_equal(1.0, 1.0 + 1e-12), true);
  check("far doubles differ", values_equal(1.0, 2.0), false);
  check("ints exact", values_equal(3, 3), true);
  check("float value is shown, not truncated", c2hlsc_show(1.5) == std::string("1.5"), true);
  check("nan is shown by name", c2hlsc_show(nan).find("nan") != std::string::npos, true);
  check("int is shown plainly", c2hlsc_show(7) == std::string("7"), true);
  return failures;
}
"""


@unittest.skipIf(shutil.which("g++") is None, "g++ unavailable")
class ValueComparisonTest(unittest.TestCase):
    def _helpers(self) -> str:
        """The generated helper block, up to the testbench's own main()."""

        work = Path(tempfile.mkdtemp())
        (work / "in.c").write_text(
            "void scale(const double a[4], double out[4]) {\n"
            "  for (int i = 0; i < 4; ++i) out[i] = a[i] * 2.0;\n"
            "}\n",
            encoding="utf-8",
        )
        cfg = AgentConfig(top="scale", input_files=[work / "in.c"])
        testbench = generate_testbench(analyze_source(work / "in.c", "scale", cfg), cfg)
        head, _, _ = testbench.partition("int main()")
        # Drop the golden include: the probe only needs the helpers.
        return "\n".join(
            line
            for line in head.splitlines()
            if not line.startswith('#include "') and not line.startswith("#define main")
        )

    def test_generated_helpers_behave(self) -> None:
        work = Path(tempfile.mkdtemp())
        source = work / "probe.cpp"
        source.write_text(self._helpers() + PROBE, encoding="utf-8")
        binary = work / "probe"
        build = subprocess.run(
            ["g++", "-std=c++17", "-O0", str(source), "-o", str(binary)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(build.returncode, 0, build.stderr[-2000:])
        run = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
