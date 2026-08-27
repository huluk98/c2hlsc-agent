"""The direct-RTL tier must not report a pass having compared nothing.

A compare length that fails to load reads as X, so `for (i = 0; i < len; ...)` is false on
the first iteration and every comparison is skipped. The bench then printed
"RTL_TB: PASS 64 tests" and the tier wrote status "pass", with the simulator's $readmemh
error sitting unread in the captured stdout. These tests pin both guards: the counter in
the generated Verilog, and the independent check in the generated Python runner.
"""

from __future__ import annotations

import unittest

from c2hlsc_agent.verilog_testgen import _GEN_RTL_TB, _RUN_RTL_SIM


def _testbench_template() -> str:
    """The gen_rtl_tb.py script emitted into every project; it renders the SystemVerilog."""

    return _GEN_RTL_TB


def _runner_template() -> str:
    """The run_rtl_sim.py script emitted into every project; it decides the tier's status."""

    return _RUN_RTL_SIM


class GeneratedTestbenchTest(unittest.TestCase):
    def test_counts_what_it_compared(self) -> None:
        tb = _testbench_template()
        self.assertIn("integer compares = 0;", tb)
        self.assertIn("compares = compares + 1;", tb)
        self.assertIn('$display("RTL_TB: COMPARED %0d", compares);', tb)

    def test_pass_requires_a_comparison(self) -> None:
        """With observable outputs, zero comparisons must not read as agreement."""

        tb = _testbench_template()
        self.assertIn("errors == 0 && compares > 0", tb)
        self.assertIn("RTL_TB: FAIL compared nothing", tb)

    def test_unloadable_compare_length_is_caught(self) -> None:
        tb = _testbench_template()
        self.assertIn("=== 1'bx", tb)
        self.assertIn("compare length did not load", tb)

    def test_top_without_outputs_still_says_so(self) -> None:
        """A top with nothing observable keeps its explicit NOTE rather than a bare PASS."""

        tb = _testbench_template()
        self.assertIn("RTL_TB: NOTE no observable outputs", tb)
        # ...and that branch is the only one allowed to pass with zero comparisons.
        self.assertIn("if has_observable:", tb)


class RunnerPredicateTest(unittest.TestCase):
    """The Python runner re-checks independently, so neither layer alone must be right."""

    def _runner(self) -> str:
        return _runner_template()

    def test_runner_parses_and_records_the_count(self) -> None:
        runner = self._runner()
        self.assertIn('RTL_TB: COMPARED (\\d+)', runner)
        self.assertIn('"comparisons_performed"', runner)

    def test_runner_treats_a_failed_vector_load_as_fatal(self) -> None:
        runner = self._runner()
        self.assertIn("Unable to open", runner)
        self.assertIn("Not enough words", runner)
        self.assertIn("load_failed", runner)

    def test_runner_rejects_a_vacuous_pass(self) -> None:
        runner = self._runner()
        self.assertIn("vacuous", runner)
        self.assertIn("not load_failed and not vacuous", runner)


if __name__ == "__main__":
    unittest.main()
