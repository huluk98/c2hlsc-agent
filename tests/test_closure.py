"""Tests for file-scope closure extraction and the bound/direction inference it feeds.

These cover the shapes that actually broke the CHStone and Rosetta agent rungs, so each
test names the benchmark it came from.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from c2hlsc_agent.analyze import (
    _infer_pointer_directions,
    analyze_source,
    collect_constants,
    evaluate_constant,
)
from c2hlsc_agent.closure import extract_closure, libclang_status
from c2hlsc_agent.config import AgentConfig

_AVAILABLE, _WHY = libclang_status()
requires_libclang = unittest.skipUnless(_AVAILABLE, f"libclang unavailable: {_WHY}")


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


@requires_libclang
class ClosureTests(unittest.TestCase):
    def test_hoists_the_types_macros_and_helpers_the_top_closes_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write(
                root,
                "input.c",
                """
                #define N 4
                typedef int word;
                static word table[N] = {1, 2, 3, 4};
                int helper(int v) { return v + 1; }
                int top(void) {
                  int total = 0;
                  for (int i = 0; i < N; i++) total += helper(table[i]);
                  return total;
                }
                """,
            )
            result = extract_closure(source, "top")
            self.assertTrue(result.available, result.diagnostics)
            self.assertIn("N", result.macros)
            self.assertIn("word", result.symbols)
            self.assertIn("table", result.symbols)
            self.assertIn("helper", result.symbols)

    def test_a_kr_definition_is_re_emitted_in_ansi_form(self):
        """CHStone's blowfish and motion: legal C89 that g++ rejects outright."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write(
                root,
                "input.c",
                """
                int scale (value, factor)
                     int value;
                     int factor;
                {
                  return value * factor;
                }
                int top(void) { return scale(6, 7); }
                """,
            )
            result = extract_closure(source, "top")
            self.assertIn("scale", result.symbols)
            self.assertIn("int scale(int value, int factor)", result.preamble)
            self.assertTrue(
                any("K&R" in note for note in result.normalizations), result.normalizations
            )

    def test_the_emitted_translation_unit_compiles_as_cpp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write(
                root,
                "input.c",
                """
                #define WIDTH 3
                typedef unsigned char byte;
                static byte lut[WIDTH] = {1, 2, 3};
                int add (a, b) int a; int b; { return a + b; }
                int top(void) {
                  int total = 0;
                  for (int i = 0; i < WIDTH; i++) total = add(total, lut[i]);
                  return total;
                }
                """,
            )
            result = extract_closure(source, "top")
            unit = root / "unit.cpp"
            unit.write_text(
                result.preamble + "\nint top(void);\nint main() { return top() == 6 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            compiled = subprocess.run(
                ["g++", "-std=c++17", "-w", "-c", str(unit), "-o", str(root / "unit.o")],
                capture_output=True,
                text=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def test_locals_are_not_hoisted_to_file_scope(self):
        """A hoisted local's initializer references parameters that do not exist there."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write(
                root,
                "input.c",
                """
                int scale(int v) { return v * 2; }
                int top(int seed) {
                  int scratch = scale(seed);
                  return scratch;
                }
                """,
            )
            result = extract_closure(source, "top")
            self.assertIn("scale", result.symbols)
            self.assertNotIn("scratch", result.symbols)

    def test_reports_unavailable_rather_than_raising_without_libclang(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _write(Path(tmp), "input.c", "int top(void) { return 0; }")
            result = extract_closure(source, "no_such_function")
            self.assertTrue(result.available)
            self.assertEqual(result.preamble, "")
            self.assertTrue(result.diagnostics)


class ConstantResolutionTests(unittest.TestCase):
    def test_named_bounds_resolve(self):
        """Rosetta writes every bound as a constant, never as a literal."""

        constants = collect_constants(
            """
            #define __TYPEDEFS_H__
            #define NUM_TEST 2000
            const int NUM_FEATURES  = 1024;
            const int NUM_TRAINING  = 4500;
            """
        )
        self.assertEqual(constants["NUM_FEATURES"], 1024)
        self.assertEqual(constants["NUM_TRAINING"], 4500)
        # a valueless guard macro must not swallow the definition on the next line
        self.assertEqual(constants["NUM_TEST"], 2000)
        self.assertEqual(
            evaluate_constant("NUM_FEATURES * NUM_TRAINING", constants), 1024 * 4500
        )

    def test_an_unknown_name_leaves_the_bound_unresolved(self):
        self.assertIsNone(evaluate_constant("SOME_UNKNOWN", {}))
        self.assertIsNone(evaluate_constant("compute_size()", {"compute_size": 1}))


class DirectionInferenceTests(unittest.TestCase):
    def test_an_argument_written_only_by_a_callee_is_still_compared(self):
        """Rosetta's SgdLR_sw never assigns theta; it calls updateParameter(theta, ...).

        Classifying that as an input left the kernel's only output uncompared, so the
        equivalence check passed without testing anything.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write(
                root,
                "input.c",
                """
                void updateParameter(float p[8], float g[8]) {
                  for (int i = 0; i < 8; i++) p[i] += g[i];
                }
                void top(float theta[8], float grad[8]) {
                  updateParameter(theta, grad);
                }
                """,
            )
            analysis = analyze_source(source, "top", AgentConfig(top="top"))
            directions = {arg.name: arg.direction for arg in analysis.function.args}
            self.assertNotEqual(directions["theta"], "input", directions)

    def test_a_const_argument_stays_an_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write(
                root,
                "input.c",
                """
                int sum(const int v[4]) { return v[0] + v[1]; }
                int top(const int values[4]) { return sum(values); }
                """,
            )
            analysis = analyze_source(source, "top", AgentConfig(top="top"))
            directions = {arg.name: arg.direction for arg in analysis.function.args}
            self.assertEqual(directions["values"], "input", directions)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
