import tempfile
import unittest
import shutil
import subprocess
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.convert import generate_hls_sources
from c2hlsc_agent.hls_project import (
    render_makefile,
    render_run_all,
    render_run_csim,
    render_run_cosim,
    render_run_csynth,
    render_run_hls,
    write_project,
)
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

    def test_ap_memory_interface_pragmas_for_pointer_args(self):
        analysis, cfg = self._analysis()
        cfg.interface_mode = "ap_memory"
        generated = generate_hls_sources(analysis, cfg)
        self.assertIn("#pragma HLS INTERFACE ap_memory port=a", generated.source)
        self.assertIn("#pragma HLS INTERFACE ap_memory port=b", generated.source)
        self.assertIn("#pragma HLS INTERFACE ap_memory port=out", generated.source)
        self.assertIn("#pragma HLS INTERFACE s_axilite port=n", generated.source)

    def test_tcl_generation_contains_required_vitis_phases(self):
        analysis, cfg = self._analysis()
        cfg.cosim_tool = "xsim"
        tcl = render_run_hls(analysis, cfg)
        self.assertIn("csim_design", tcl)
        self.assertIn("csynth_design", tcl)
        self.assertIn("cosim_design -tool xsim -rtl verilog", tcl)
        self.assertNotIn("add_files -tb input.c", tcl)

    def test_tcl_defaults_to_vivado_ip_flow_for_legacy_hls_compatibility(self):
        analysis, cfg = self._analysis()
        for tcl in (render_run_hls(analysis, cfg), render_run_csim(analysis, cfg)):
            self.assertIn('open_solution "solution1"', tcl)
            self.assertNotIn('open_solution "solution1" -flow_target', tcl)

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

    def test_generated_helpers_use_unified_vitis_native_command(self):
        _analysis, cfg = self._analysis()
        cfg.vitis_bin = "/opt/AMD/Vitis/bin/vitis-run"
        expected = "/opt/AMD/Vitis/bin/vitis-run --mode hls --tcl run_hls.tcl"
        self.assertIn(expected, render_makefile(cfg))
        self.assertIn(expected, render_run_all(cfg))

    def test_generated_makefile_allows_windows_python_override(self):
        _analysis, cfg = self._analysis()
        makefile = render_makefile(cfg)
        self.assertIn("PYTHON ?= python3", makefile)
        self.assertIn('"$(PYTHON)" tb/leveri_compare.py', makefile)
        self.assertIn('"$(PYTHON)" tb/run_gcov.py', makefile)
        self.assertIn('"$(PYTHON)" tb/run_klee.py', makefile)
        self.assertIn('"$(CXX)" $(CXXFLAGS)', makefile)

    @unittest.skipUnless(shutil.which("make"), "make is required")
    def test_generated_makefile_preserves_python_path_with_spaces(self):
        _analysis, cfg = self._analysis()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text(render_makefile(cfg), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHON"] = "/tmp/Python With Spaces/python.exe"
            dry_run = subprocess.run(
                ["make", "-n", "gcov-coverage"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
            )
        self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
        self.assertIn(
            '"/tmp/Python With Spaces/python.exe" tb/run_gcov.py', dry_run.stdout
        )

    def test_generated_testbench_compares_output_arrays(self):
        analysis, cfg = self._analysis()
        testbench = generate_testbench(analysis, cfg)
        self.assertIn("// - out: direction=output length=4 compare all 4 elements; active prefix is clamp(n, 4)", testbench)
        self.assertIn("const int compare_len_out = 4;", testbench)
        self.assertIn("const int active_len_out = clamp_count(print_value(n), 4);", testbench)
        self.assertIn("for (int i = 0; i < compare_len_out; ++i)", testbench)
        self.assertIn("if (!values_equal(ref_out[i], hls_out[i]))", testbench)
        self.assertIn('<< " compare_len=" << compare_len_out', testbench)
        self.assertIn('<< " active_len=" << active_len_out', testbench)
        self.assertIn('<< " n=" << print_value(n)', testbench)
        self.assertIn('"Mismatch test=" << test_idx << " arg=out index="', testbench)
        self.assertIn("std::cerr.precision(17);", testbench)

    def test_generated_testbench_uses_vitis_friendly_stimulus(self):
        analysis, cfg = self._analysis()
        testbench = generate_testbench(analysis, cfg)
        self.assertIn("int n = bounded_scalar<int>(test_idx, rng, 0LL, 4LL);", testbench)
        self.assertIn("output_sentinel<int32_t>(test_idx, i)", testbench)
        self.assertIn("if (std::numeric_limits<T>::is_integer)", testbench)
        self.assertNotIn("if constexpr", testbench)
        self.assertIn("static_cast<long long>(rng() % 20001) - 10000", testbench)
        self.assertIn("if (std::isnan(", testbench)
        self.assertIn("!std::isfinite(", testbench)
        self.assertIn("struct print_as", testbench)

    def test_generated_testbench_bounds_unconfigured_length_from_pointer_capacity(self):
        analysis, cfg = self._analysis()
        length = next(arg for arg in analysis.function.args if arg.name == "n")
        length.scalar_range = None

        testbench = generate_testbench(analysis, cfg)

        self.assertIn("int n = bounded_scalar<int>(test_idx, rng, 0LL, 4LL);", testbench)
        self.assertIn("// - n: inferred scalar range=[0, 4] from related pointer capacity", testbench)

    def test_explicit_scalar_range_wins_over_inferred_pointer_capacity(self):
        analysis, cfg = self._analysis()
        length = next(arg for arg in analysis.function.args if arg.name == "n")
        length.scalar_range = (1, 2)

        testbench = generate_testbench(analysis, cfg)

        self.assertIn("int n = bounded_scalar<int>(test_idx, rng, 1LL, 2LL);", testbench)

    @unittest.skipUnless(shutil.which("g++") and shutil.which("make"), "g++ and make are required")
    def test_generated_testbench_rejects_writes_past_active_prefix(self):
        analysis, cfg = self._analysis()
        generated = generate_hls_sources(analysis, cfg)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name) / "project"
        write_project(project, analysis, generated, cfg)

        source_path = project / "src" / "hls_top.cpp"
        source = source_path.read_text(encoding="utf-8")
        source_path.write_text(source.replace("i < n", "i < 4"), encoding="utf-8")

        failing = subprocess.run(["make", "-C", str(project), "test"], text=True, capture_output=True)
        output = failing.stdout + failing.stderr
        self.assertNotEqual(failing.returncode, 0, output)
        self.assertIn("Mismatch test=0 arg=out", output)
        self.assertIn("active_len=0", output)

    @unittest.skipUnless(shutil.which("g++") and shutil.which("make"), "g++ and make are required")
    def test_generated_testbench_passes_and_rejects_mutated_hls(self):
        analysis, cfg = self._analysis()
        generated = generate_hls_sources(analysis, cfg)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name) / "project"
        write_project(project, analysis, generated, cfg)

        passing = subprocess.run(["make", "-C", str(project), "test"], text=True, capture_output=True)
        self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)

        source_path = project / "src" / "hls_top.cpp"
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("out[i] = a[i] + b[i];", source)
        source_path.write_text(source.replace("out[i] = a[i] + b[i];", "out[i] = a[i] - b[i];"), encoding="utf-8")

        failing = subprocess.run(["make", "-C", str(project), "clean", "test"], text=True, capture_output=True)
        output = failing.stdout + failing.stderr
        self.assertNotEqual(failing.returncode, 0, output)
        self.assertIn("Mismatch test=", output)
        self.assertIn("arg=out", output)
        self.assertIn("compare_len=", output)


if __name__ == "__main__":
    unittest.main()
