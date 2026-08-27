import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.analyze import _parse_arg, analyze_source, unsupported_in_generated
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

    def test_output_written_through_alias_is_still_compared(self):
        # Regression: `out` is written only through the local alias `dst`, which the
        # write regex cannot see. Classifying it "input" excluded it from the
        # comparison entirely, so a wrong implementation passed every test.
        path = self._write(
            """
            void scale_kernel(const int *in, int *out, int n) {
              int *dst = out;
              for (int i = 0; i < n; ++i) dst[i] = in[i] * 3;
            }
            """
        )
        cfg = AgentConfig(
            top="scale_kernel",
            arguments={"in": ArgumentConfig(length=8), "out": ArgumentConfig(length=8)},
        )
        result = analyze_source(path, "scale_kernel", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertIn(directions["out"], {"output", "inout"})
        self.assertEqual(directions["in"], "input")
        codes = {diag.code for diag in result.diagnostics.items}
        self.assertIn("unprovable-pointer-direction", codes)

    def test_output_passed_to_helper_is_still_compared(self):
        # Same hazard via a different route: the write happens inside memcpy.
        path = self._write(
            """
            #include <string.h>
            void copy_kernel(const int *in, int *out) {
              memcpy(out, in, 8 * sizeof(int));
            }
            """
        )
        cfg = AgentConfig(
            top="copy_kernel",
            arguments={"in": ArgumentConfig(length=8), "out": ArgumentConfig(length=8)},
        )
        result = analyze_source(path, "copy_kernel", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertIn(directions["out"], {"output", "inout"})

    def test_const_pointer_passed_to_helper_stays_an_input(self):
        # The const qualifier is a real read-only guarantee, so escaping is not
        # enough to force a conservative direction.
        path = self._write(
            """
            #include <string.h>
            void copy_kernel(const int *in, int *out) {
              memcpy(out, in, 8 * sizeof(int));
            }
            """
        )
        cfg = AgentConfig(
            top="copy_kernel",
            arguments={"in": ArgumentConfig(length=8), "out": ArgumentConfig(length=8)},
        )
        result = analyze_source(path, "copy_kernel", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertEqual(directions["in"], "input")

    def test_no_observable_output_fails_closed(self):
        # With nothing to compare, every stimulus trivially agrees. That must be an
        # error rather than a silently passing run.
        path = self._write(
            """
            void sink_kernel(const int *in, int n) {
              volatile int acc = 0;
              for (int i = 0; i < n; ++i) acc += in[i];
            }
            """
        )
        cfg = AgentConfig(top="sink_kernel", arguments={"in": ArgumentConfig(length=8)})
        result = analyze_source(path, "sink_kernel", cfg)
        codes = {diag.code for diag in result.diagnostics.items if diag.severity == "error"}
        self.assertIn("no-observable-output", codes)
        self.assertTrue(result.diagnostics.has_errors)

    def test_return_value_counts_as_observable_output(self):
        path = self._write(
            """
            int sum_kernel(const int *in, int n) {
              int acc = 0;
              for (int i = 0; i < n; ++i) acc += in[i];
              return acc;
            }
            """
        )
        cfg = AgentConfig(top="sum_kernel", arguments={"in": ArgumentConfig(length=8)})
        result = analyze_source(path, "sum_kernel", cfg)
        codes = {diag.code for diag in result.diagnostics.items}
        self.assertNotIn("no-observable-output", codes)

    def test_macro_array_bound_is_not_a_variable_length_array(self):
        # A #define'd bound is a compile-time constant; flagging it as a VLA made
        # correctly transformed output look non-synthesizable.
        path = self._write(
            """
            #define MAX_N 16
            void k(const int *in, int *out, int n) {
              int scratch[MAX_N];
              for (int i = 0; i < MAX_N; ++i) scratch[i] = in[i];
              for (int i = 0; i < n; ++i) out[i] = scratch[i];
            }
            """
        )
        cfg = AgentConfig(
            top="k", arguments={"in": ArgumentConfig(length=16), "out": ArgumentConfig(length=16)}
        )
        result = analyze_source(path, "k", cfg)
        codes = {diag.code for diag in result.unsupported_constructs}
        self.assertNotIn("variable-length-array", codes)

    def test_runtime_array_bound_is_still_a_variable_length_array(self):
        path = self._write(
            """
            void k(const int *in, int *out, int n) {
              int scratch[n];
              for (int i = 0; i < n; ++i) scratch[i] = in[i];
              for (int i = 0; i < n; ++i) out[i] = scratch[i];
            }
            """
        )
        cfg = AgentConfig(
            top="k", arguments={"in": ArgumentConfig(length=16), "out": ArgumentConfig(length=16)}
        )
        result = analyze_source(path, "k", cfg)
        codes = {diag.code for diag in result.unsupported_constructs}
        self.assertIn("variable-length-array", codes)

    def test_generated_output_is_rescanned_independently_of_the_input(self):
        # Input diagnostics mean "this needs transforming"; only the generated
        # output decides whether the transformation succeeded.
        dirty = "void k(const int *in, int *out, int n) { int *p = malloc(4); free(p); }"
        clean = "void k(const int *in, int *out, int n) { for (int i=0;i<n;++i) out[i]=in[i]; }"
        cfg = AgentConfig(top="k")
        self.assertTrue(
            any(d.code == "dynamic-allocation" for d in unsupported_in_generated(dirty, "k", cfg, "src/hls_top.cpp"))
        )
        self.assertEqual(unsupported_in_generated(clean, "k", cfg, "src/hls_top.cpp"), [])

    def test_array_bound_expression_is_constant_folded(self):
        # Regression: a bound like 64*64 failed isdigit(), so length fell back to the
        # conservative default of 16 and the testbench indexed past its own buffers.
        path = self._write(
            """
            void gemm(const double m1[64*64], const double m2[64*64], double prod[64*64]) {
              for (int i = 0; i < 64*64; ++i) prod[i] = m1[i] + m2[i];
            }
            """
        )
        result = analyze_source(path, "gemm", AgentConfig(top="gemm"))
        lengths = {arg.name: arg.length for arg in result.function.args}
        self.assertEqual(lengths["m1"], 4096)
        self.assertEqual(lengths["prod"], 4096)

    def test_unranged_scalar_is_clamped_to_the_shortest_array(self):
        # Regression: an unranged scalar was generated as a full-range random int. Used
        # as a loop bound over a fixed-size array that segfaults the testbench, and the
        # crash surfaced only as a make failure with no diagnostic.
        path = self._write(
            """
            void k(const int x[64], int y[64], int n) {
              for (int i = 0; i < n; ++i) y[i] = x[i] + 1;
            }
            """
        )
        result = analyze_source(path, "k", AgentConfig(top="k"))
        ranges = {arg.name: arg.scalar_range for arg in result.function.args}
        self.assertEqual(ranges["n"], (0, 64))
        codes = {diag.code for diag in result.diagnostics.items}
        self.assertIn("unbounded-scalar-stimulus", codes)

    def test_configured_scalar_range_is_not_overridden(self):
        path = self._write(
            """
            void k(const int x[64], int y[64], int n) {
              for (int i = 0; i < n; ++i) y[i] = x[i] + 1;
            }
            """
        )
        cfg = AgentConfig(top="k", arguments={"n": ArgumentConfig(range=(0, 8))})
        result = analyze_source(path, "k", cfg)
        ranges = {arg.name: arg.scalar_range for arg in result.function.args}
        self.assertEqual(ranges["n"], (0, 8))

    def test_write_through_dereferenced_array_pointer_is_seen(self):
        # Regression: AES writes its state as `(*state)[i][j] ^= ...`. The write
        # pattern matched neither the parenthesised deref nor the subscripts after
        # it, so `state` was called an input, nothing was comparable, and the design
        # was refused as unevaluable rather than verified.
        path = self._write(
            """
            void AddRoundKey(unsigned char round, unsigned char (*state)[4][4], const unsigned char *rk) {
              for (int i = 0; i < 4; ++i)
                for (int j = 0; j < 4; ++j)
                  (*state)[i][j] ^= rk[i * 4 + j];
            }
            """
        )
        cfg = AgentConfig(top="AddRoundKey", arguments={"rk": ArgumentConfig(length=16)})
        result = analyze_source(path, "AddRoundKey", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertIn(directions["state"], {"output", "inout"})
        codes = {d.code for d in result.diagnostics.items if d.severity == "error"}
        self.assertNotIn("no-observable-output", codes)

    def test_write_through_struct_arrow_is_seen(self):
        path = self._write(
            """
            struct Acc { int total; };
            void k(struct Acc *out, const int *in, int n) {
              out->total = 0;
              for (int i = 0; i < n; ++i) out->total += in[i];
            }
            """
        )
        cfg = AgentConfig(top="k", arguments={"in": ArgumentConfig(length=8), "out": ArgumentConfig(length=1)})
        result = analyze_source(path, "k", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertIn(directions["out"], {"output", "inout"})

    def test_write_through_multi_dimensional_subscript_is_seen(self):
        path = self._write(
            """
            void k(int out[4][4], const int *in) {
              for (int i = 0; i < 4; ++i)
                for (int j = 0; j < 4; ++j) out[i][j] = in[0];
            }
            """
        )
        cfg = AgentConfig(top="k", arguments={"in": ArgumentConfig(length=4)})
        result = analyze_source(path, "k", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertIn(directions["out"], {"output", "inout"})

    def test_pointer_to_array_parameter_name_and_type(self):
        # Regression: the parentheses in `T (*p)[4][4]` bind the star to the name, so
        # a naive split left the closing paren stuck to it -- the argument was called
        # 'state)' and that name flowed into the config, the report and the testbench.
        arg = _parse_arg("unsigned char (*state)[4][4]")
        self.assertEqual(arg.name, "state")
        self.assertEqual(arg.c_type, "unsigned char")
        self.assertEqual(arg.pointer_depth, 1)

    def test_multi_dimensional_bound_uses_the_product(self):
        # The testbench models every array argument as one flat buffer, so taking
        # only the first dimension under-allocates and the test indexes past its own
        # array.
        self.assertEqual(_parse_arg("int out[4][4]").length, 16)
        self.assertEqual(_parse_arg("double a[40][40]").length, 1600)
        self.assertEqual(_parse_arg("unsigned char (*s)[4][4]").length, 16)
        self.assertEqual(_parse_arg("int a[16]").length, 16)

    def test_read_only_pointer_survives_the_wider_write_patterns(self):
        path = self._write(
            """
            int k(const int *in, int *out, int n) {
              int acc = 0;
              for (int i = 0; i < n; ++i) acc += in[i];
              out[0] = acc;
              return acc;
            }
            """
        )
        cfg = AgentConfig(
            top="k", arguments={"in": ArgumentConfig(length=8), "out": ArgumentConfig(length=1)}
        )
        result = analyze_source(path, "k", cfg)
        directions = {arg.name: arg.direction for arg in result.function.args}
        self.assertEqual(directions["in"], "input")
        self.assertIn(directions["out"], {"output", "inout"})

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
