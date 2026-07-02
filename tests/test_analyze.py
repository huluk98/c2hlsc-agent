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
