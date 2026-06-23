import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.convert import generate_hls_sources
from c2hlsc_agent.hls_project import render_run_csim, render_run_cosim, render_run_csynth, render_run_hls
from c2hlsc_agent.testgen import generate_testbench


class ConvertTests(unittest.TestCase):
    def _analysis(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "input.c"
        path.write_text(
            """
            #include <stdint.h>
            void vector_add(const int32_t *a, const int32_t *b, int32_t *out, int n) {
              for (int i = 0; i < n; ++i) out[i] = a[i] + b[i];
            }
            """,
            encoding="utf-8",
        )
        cfg = AgentConfig(
            top="vector_add",
            arguments={
                "a": ArgumentConfig(direction="input", length=4),
                "b": ArgumentConfig(direction="input", length=4),
                "out": ArgumentConfig(direction="output", length=4),
                "n": ArgumentConfig(range=(0, 4)),
            },
        )
        return analyze_source(path, "vector_add", cfg), cfg

    def test_generated_source_separates_header_and_body(self):
        analysis, cfg = self._analysis()
        generated = generate_hls_sources(analysis, cfg)
        self.assertIn("void vector_add", generated.header)
        self.assertIn('#include "hls_top.hpp"', generated.source)
        self.assertIn("out[i] = a[i] + b[i]", generated.source)

    def test_tcl_generation_contains_required_vitis_phases(self):
        analysis, cfg = self._analysis()
        cfg.cosim_tool = "xsim"
        tcl = render_run_hls(analysis, cfg)
        self.assertIn("csim_design", tcl)
        self.assertIn("csynth_design", tcl)
        self.assertIn("cosim_design -tool xsim -rtl verilog", tcl)
        self.assertNotIn("add_files -tb input.c", tcl)

    def test_split_tcl_generation_is_phase_specific(self):
        analysis, cfg = self._analysis()
        cfg.cosim_tool = "xsim"
        csim = render_run_csim(analysis, cfg)
        csynth = render_run_csynth()
        cosim = render_run_cosim(cfg)
        self.assertIn("open_project -reset c2hlsc_project", csim)
        self.assertIn("csim_design", csim)
        self.assertNotIn("csynth_design", csim)
        self.assertIn("csynth_design", csynth)
        self.assertNotIn("csim_design", csynth)
        self.assertIn("cosim_design -tool xsim -rtl verilog", cosim)
        self.assertNotIn("csynth_design", cosim)

    def test_generated_testbench_compares_output_arrays(self):
        analysis, cfg = self._analysis()
        testbench = generate_testbench(analysis, cfg)
        self.assertIn("if (ref_out[i] != hls_out[i])", testbench)
        self.assertIn('"Mismatch test=" << test_idx << " arg=out index="', testbench)


if __name__ == "__main__":
    unittest.main()
