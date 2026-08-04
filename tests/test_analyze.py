import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig, ArgumentConfig


class AnalyzeTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "input.c"
        path.write_text(text, encoding="utf-8")
        return path

    def test_type_mapping_preserves_signed_and_unsigned(self):
        path = self._write(
            """
            #include <stdint.h>
            uint32_t mix(int32_t a, uint32_t b) { return ((uint32_t)a) ^ b; }
            """
        )
        result = analyze_source(path, "mix", AgentConfig(top="mix"))
        mapping = {row["name"]: row for row in result.type_mappings}
        self.assertEqual(mapping["return"]["generated"], "uint32_t")
        self.assertEqual(mapping["a"]["generated"], "int32_t")
        self.assertEqual(mapping["b"]["generated"], "uint32_t")

    def test_pointer_direction_inference(self):
        path = self._write(
            """
            void kernel(const int *a, int *out, int n) {
              for (int i = 0; i < n; ++i) out[i] = a[i] + 1;
            }
            """
        )
        cfg = AgentConfig(top="kernel", arguments={"a": ArgumentConfig(length=8), "out": ArgumentConfig(length=8)})
        result = analyze_source(path, "kernel", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertEqual(directions["a"], "input")
        self.assertEqual(directions["out"], "output")

    def test_pointer_direction_not_fooled_by_equality_comparison(self):
        # Regression: an array used only in an `==` comparison must stay an input.
        # Previously the write-detection regex matched the first `=` of `==`.
        path = self._write(
            """
            void cmp(const int a[8], const int b[8], int out[8]) {
              for (int i = 0; i < 8; ++i) {
                if (a[i] == b[i]) out[i] = 1; else out[i] = 0;
              }
            }
            """
        )
        cfg = AgentConfig(
            top="cmp",
            arguments={
                "a": ArgumentConfig(length=8),
                "b": ArgumentConfig(length=8),
                "out": ArgumentConfig(length=8),
            },
        )
        result = analyze_source(path, "cmp", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertEqual(directions["a"], "input")
        self.assertEqual(directions["b"], "input")
        self.assertEqual(directions["out"], "output")

    def test_mutable_read_only_pointer_remains_observable(self):
        path = self._write(
            """
            int sum_values(int *a, int n) {
              int sum = 0;
              for (int i = 0; i < n; ++i) sum += a[i];
              return sum;
            }
            """
        )
        cfg = AgentConfig(top="sum_values", arguments={"a": ArgumentConfig(length=8)})
        result = analyze_source(path, "sum_values", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertEqual(directions["a"], "inout")

    def test_explicit_pointer_direction_wins_over_inference(self):
        path = self._write(
            """
            int sum_values(int *a, int n) {
              int sum = 0;
              for (int i = 0; i < n; ++i) sum += a[i];
              return sum;
            }
            """
        )
        for direction in ("input", "output"):
            with self.subTest(direction=direction):
                cfg = AgentConfig(
                    top="sum_values",
                    arguments={"a": ArgumentConfig(direction=direction, length=8)},
                )
                result = analyze_source(path, "sum_values", cfg)
                directions = {arg.name: arg.direction for arg in result.function.args}
                self.assertEqual(directions["a"], direction)

    def test_pointer_const_qualifier_applies_to_pointee(self):
        path = self._write(
            """
            int qualifiers(const int *a, int const *b, int * const c, const int * const d) {
              return a[0] + b[0] + c[0] + d[0];
            }
            """
        )
        cfg = AgentConfig(
            top="qualifiers",
            arguments={name: ArgumentConfig(length=4) for name in ("a", "b", "c", "d")},
        )
        result = analyze_source(path, "qualifiers", cfg)
        args = {arg.name: arg for arg in result.function.args}
        self.assertEqual(
            {name: args[name].direction for name in args},
            {"a": "input", "b": "input", "c": "inout", "d": "input"},
        )
        self.assertEqual(
            {name: args[name].is_const for name in args},
            {"a": True, "b": True, "c": False, "d": True},
        )

    def test_explicit_direction_wins_for_const_pointee(self):
        path = self._write("int first(const int *a) { return a[0]; }")
        cfg = AgentConfig(
            top="first",
            arguments={"a": ArgumentConfig(direction="output", length=4)},
        )
        result = analyze_source(path, "first", cfg)
        self.assertEqual(result.function.args[0].direction, "output")

    def test_restrict_qualifier_is_stripped_from_type(self):
        # Regression: the C `restrict` keyword must not leak into the C++ type,
        # which would produce invalid declarations in the generated testbench.
        path = self._write(
            """
            void scale(const int *restrict src, int *restrict dst, int n) {
              for (int i = 0; i < n; ++i) dst[i] = src[i] * 2;
            }
            """
        )
        cfg = AgentConfig(
            top="scale",
            arguments={"src": ArgumentConfig(length=8), "dst": ArgumentConfig(length=8)},
        )
        result = analyze_source(path, "scale", cfg)
        types = {arg.name: arg.c_type for arg in result.function.args}
        self.assertNotIn("restrict", types["src"])
        self.assertNotIn("restrict", types["dst"])
        self.assertEqual(types["src"], "const int")
        self.assertEqual(types["dst"], "int")
        # The generated signature/definition are built from arg.raw, so it must be
        # sanitized too — otherwise the emitted hls_top.hpp/.cpp are invalid C++.
        raws = {arg.name: arg.raw for arg in result.function.args}
        self.assertNotIn("restrict", raws["src"])
        self.assertNotIn("restrict", raws["dst"])
        self.assertNotIn("restrict", result.function.signature)

    def test_multi_dimensional_parameter_is_rejected(self):
        path = self._write(
            """
            void blur(const int in[3][3], int out[3][3]) {
              for (int r = 0; r < 3; ++r)
                for (int c = 0; c < 3; ++c) out[r][c] = in[r][c] + 1;
            }
            """
        )
        result = analyze_source(path, "blur", AgentConfig(top="blur"))
        codes = [diag.code for diag in result.unsupported_constructs]
        self.assertEqual(codes.count("multi-dimensional-parameter"), 2)
        self.assertTrue(result.diagnostics.has_errors)

    def test_qualified_return_type_is_normalized(self):
        path = self._write(
            """
            static void top(const int *a, int *out, int n) {
              for (int i = 0; i < n; ++i) out[i] = a[i];
            }
            """
        )
        cfg = AgentConfig(
            top="top",
            arguments={"a": ArgumentConfig(length=4), "out": ArgumentConfig(length=4)},
        )
        result = analyze_source(path, "top", cfg)
        self.assertEqual(result.function.return_type, "void")
        self.assertTrue(result.function.signature.startswith("void top("))

    def test_apostrophe_in_comment_does_not_break_body_extraction(self):
        path = self._write(
            """
            void guard(const int *a, int *out, int n) {
              // don't let prose hide the closing brace
              for (int i = 0; i < n; ++i) out[i] = a[i];
            }
            """
        )
        cfg = AgentConfig(
            top="guard",
            arguments={"a": ArgumentConfig(length=4), "out": ArgumentConfig(length=4)},
        )
        result = analyze_source(path, "guard", cfg)
        self.assertIn("out[i] = a[i]", result.function.body)
        self.assertEqual({arg.name: arg.direction for arg in result.function.args}["out"], "output")

    def test_comment_markers_inside_string_literal_are_not_comments(self):
        path = self._write(
            """
            void tricky(const int *a, int *out, int n) {
              const char *u = "http://x"; out[0] = a[0] + n;
              (void)u;
            }
            """
        )
        cfg = AgentConfig(
            top="tricky",
            arguments={"a": ArgumentConfig(length=4), "out": ArgumentConfig(length=4)},
        )
        result = analyze_source(path, "tricky", cfg)
        self.assertEqual({arg.name: arg.direction for arg in result.function.args}["out"], "output")

    def test_unsupported_construct_diagnostics(self):
        path = self._write(
            """
            #include <stdlib.h>
            int bad(int n) { int *p = malloc(sizeof(int) * n); free(p); return n; }
            """
        )
        result = analyze_source(path, "bad", AgentConfig(top="bad"))
        codes = {diag.code for diag in result.unsupported_constructs}
        self.assertIn("dynamic-allocation", codes)
        self.assertTrue(result.diagnostics.has_errors)

    def test_pointer_arithmetic_and_stdlib_are_reported(self):
        path = self._write(
            """
            #include <stdlib.h>
            int bad(int *p, int n) {
              int acc = rand();
              for (int i = 0; i < n; ++i) acc += *(p + i);
              return acc;
            }
            """
        )
        cfg = AgentConfig(top="bad", arguments={"p": ArgumentConfig(length=8)})
        result = analyze_source(path, "bad", cfg)
        codes = {diag.code for diag in result.unsupported_constructs}
        self.assertIn("pointer-arithmetic", codes)
        self.assertIn("unsupported-stdlib-call", codes)


if __name__ == "__main__":
    unittest.main()
