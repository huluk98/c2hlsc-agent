import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.equivalence import Mismatch, format_mismatch, parse_mismatches


class EquivalenceTests(unittest.TestCase):
    def test_mismatch_reporting(self):
        mismatch = Mismatch(test_index=3, argument="out", element_index=2, expected="10", actual="11", seed=99)
        text = format_mismatch(mismatch)
        self.assertIn("test=3", text)
        self.assertIn("out[2]", text)
        self.assertIn("expected=10", text)
        self.assertIn("actual=11", text)
        self.assertIn("seed=99", text)

    def test_parse_testbench_mismatch_output(self):
        text = "Mismatch test=5 arg=out index=7 expected=12 actual=13 seed=123"
        [mismatch] = parse_mismatches(text)
        self.assertEqual(mismatch.test_index, 5)
        self.assertEqual(mismatch.argument, "out")
        self.assertEqual(mismatch.element_index, 7)
        self.assertEqual(mismatch.expected, "12")
        self.assertEqual(mismatch.actual, "13")
        self.assertEqual(mismatch.seed, 123)


if __name__ == "__main__":
    unittest.main()
