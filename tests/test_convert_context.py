import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.convert import generate_hls_sources


SOURCE = """
#include <stdint.h>

#define TAPS 4

typedef int32_t acc_t;

static const int16_t kWeights[TAPS] = {1, 2, 3, 4};

static acc_t weighted(const int16_t *src, int base) {
  acc_t sum = 0;
  for (int t = 0; t < TAPS; ++t) sum += (acc_t)src[base] * (acc_t)kWeights[t];
  return sum;
}

void ctx_kernel(const int16_t x[64], acc_t y[64], int n) {
  for (int i = 0; i < n; ++i) y[i] = weighted(x, i);
}
"""


class CarriedFileScopeTests(unittest.TestCase):
    def _generate(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "input.c"
        path.write_text(SOURCE, encoding="utf-8")
        cfg = AgentConfig(
            top="ctx_kernel",
            arguments={"x": ArgumentConfig(length=64), "y": ArgumentConfig(length=64)},
        )
        analysis = analyze_source(path, "ctx_kernel", cfg)
        return generate_hls_sources(analysis, cfg, llm=None)

    def test_macros_tables_and_helpers_reach_the_generated_unit(self):
        # Regression: the generator emitted only the top function body, so everything
        # it depended on at file scope vanished and the unit failed to compile.
        generated = self._generate()
        self.assertIn("#define TAPS 4", generated.source)
        self.assertIn("kWeights", generated.source)
        self.assertIn("weighted", generated.source)

    def test_carried_context_has_internal_linkage(self):
        # The testbench compiles the original input.c into the same program for its
        # golden oracle, so external definitions here would collide at link time.
        generated = self._generate()
        self.assertIn("namespace {", generated.source)

    def test_signature_typedef_is_hoisted_into_the_header(self):
        # The header declares the top, so a typedef used by the signature has to be
        # visible there -- and must not also sit in the anonymous namespace, where the
        # two declarations would be ambiguous.
        generated = self._generate()
        self.assertIn("typedef int32_t acc_t;", generated.header)
        namespace_block = generated.source.split("namespace {", 1)[1]
        self.assertNotIn("typedef int32_t acc_t;", namespace_block)

    def test_includes_stay_outside_the_namespace(self):
        generated = self._generate()
        prologue = generated.source.split("namespace {", 1)[0]
        self.assertIn("#include <stdint.h>", prologue)


if __name__ == "__main__":
    unittest.main()
