import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.agent_loop import multi_agent_procedures
from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.convert import generate_hls_sources
from c2hlsc_agent.hls_project import write_project
from c2hlsc_agent.verilog_testgen import (
    RTL_TESTBENCH_POLICY_ID,
    RTL_TESTBENCH_SYSTEM_PROMPT,
    build_spec,
    generate_verilog_testbenches,
    get_rtl_testbench_contract,
)


# Correct depth-4 stand-in for Vitis-synthesized RTL: ap_ctrl_hs block control with
# single-port ap_memory arrays (registered one-cycle read latency) and an ap_none scalar.
CORRECT_VECTOR_ADD_RTL = """
module vector_add (
  ap_clk, ap_rst, ap_start, ap_done, ap_idle, ap_ready,
  a_address0, a_ce0, a_q0,
  b_address0, b_ce0, b_q0,
  out_address0, out_ce0, out_we0, out_d0,
  n
);
  input ap_clk;
  input ap_rst;
  input ap_start;
  output reg ap_done;
  output reg ap_idle;
  output reg ap_ready;
  output reg [1:0] a_address0;
  output reg a_ce0;
  input [31:0] a_q0;
  output reg [1:0] b_address0;
  output reg b_ce0;
  input [31:0] b_q0;
  output reg [1:0] out_address0;
  output reg out_ce0;
  output reg out_we0;
  output reg [31:0] out_d0;
  input [31:0] n;

  localparam IDLE = 2'd0, ADDR = 2'd1, WRITE = 2'd2, DONE = 2'd3;
  reg [1:0] state;
  reg [31:0] cnt, len;

  always @(*) begin
    a_ce0 = 1'b0; b_ce0 = 1'b0;
    a_address0 = cnt[1:0]; b_address0 = cnt[1:0];
    out_ce0 = 1'b0; out_we0 = 1'b0;
    out_address0 = cnt[1:0]; out_d0 = a_q0 + b_q0;
    ap_done = 1'b0; ap_idle = 1'b0; ap_ready = 1'b0;
    case (state)
      IDLE:  ap_idle = 1'b1;
      ADDR:  begin a_ce0 = 1'b1; b_ce0 = 1'b1; end
      WRITE: begin out_ce0 = 1'b1; out_we0 = 1'b1; end
      DONE:  begin ap_done = 1'b1; ap_idle = 1'b1; ap_ready = 1'b1; end
    endcase
  end

  always @(posedge ap_clk) begin
    if (ap_rst) begin
      state <= IDLE; cnt <= 0; len <= 0;
    end else case (state)
      IDLE:  if (ap_start) begin len <= n; cnt <= 0; state <= (n == 0) ? DONE : ADDR; end
      ADDR:  state <= WRITE;
      WRITE: if (cnt + 1 >= len) state <= DONE; else begin cnt <= cnt + 1; state <= ADDR; end
      DONE:  state <= IDLE;
    endcase
  end
endmodule
"""


IDLE_FLOAT_RTL = """
module scale (ap_clk, ap_rst, ap_start, ap_done, ap_idle, ap_ready,
  x_address0, x_ce0, x_q0, y_address0, y_ce0, y_we0, y_d0, n);
  input ap_clk; input ap_rst; input ap_start;
  output ap_done; output ap_idle; output ap_ready;
  output [1:0] x_address0; output x_ce0; input [31:0] x_q0;
  output [1:0] y_address0; output y_ce0; output y_we0; output [31:0] y_d0;
  input [31:0] n;
  reg done_r = 1'b0;
  assign ap_done = done_r;
  assign ap_idle = ~done_r;
  assign ap_ready = done_r;
  assign x_address0 = 2'd0; assign x_ce0 = 1'b0;
  assign y_address0 = 2'd0; assign y_ce0 = 1'b0; assign y_we0 = 1'b0; assign y_d0 = 32'd0;
  always @(posedge ap_clk) begin
    if (ap_rst) done_r <= 1'b0;
    else done_r <= ap_start;
  end
endmodule
"""


def _tools(*names):
    return all(shutil.which(name) for name in names)


class VerilogTestgenTests(unittest.TestCase):
    def _analysis(self, num_tests=8):
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
            num_tests=num_tests,
            interface_mode="ap_memory",
            arguments={
                "a": ArgumentConfig(direction="input", length=4),
                "b": ArgumentConfig(direction="input", length=4),
                "out": ArgumentConfig(direction="output", length=4),
                "n": ArgumentConfig(range=(0, 4)),
            },
        )
        return analyze_source(path, "vector_add", cfg), cfg

    def _write_project(self, num_tests=8):
        analysis, cfg = self._analysis(num_tests=num_tests)
        generated = generate_hls_sources(analysis, cfg)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name) / "project"
        write_project(project, analysis, generated, cfg)
        return project

    def test_contract_and_prompt(self):
        contract = get_rtl_testbench_contract()
        self.assertEqual(contract.policy_id, RTL_TESTBENCH_POLICY_ID)
        self.assertEqual(contract.owner_agent, "shift_left_testbench_agent")
        self.assertFalse(contract.owns_hlsc_generation)
        self.assertIn("ap_ctrl_hs", RTL_TESTBENCH_SYSTEM_PROMPT)
        self.assertIn("registered one-cycle read latency", RTL_TESTBENCH_SYSTEM_PROMPT)
        self.assertIn("Keep the original C in the oracle path", RTL_TESTBENCH_SYSTEM_PROMPT)

    def test_agent_loop_lists_rtl_testbench_output(self):
        agent = [p for p in multi_agent_procedures() if p.name == "shift_left_testbench_agent"][0]
        self.assertIn("standalone RTL self-checking testbench", agent.outputs)

    def test_spec_captures_interface_contract(self):
        analysis, cfg = self._analysis()
        spec = build_spec(analysis, cfg)
        self.assertEqual(spec["top"], "vector_add")
        self.assertEqual(spec["reset"], {"name": "ap_rst", "active_low": False, "cycles": 4})
        by_name = {a["name"]: a for a in spec["arrays"]}
        self.assertEqual(by_name["a"]["ports"], ["address0", "ce0", "q0"])
        self.assertEqual(by_name["out"]["ports"], ["address0", "ce0", "we0", "d0"])
        self.assertEqual(by_name["a"]["addr_bits"], 2)
        self.assertEqual(by_name["a"]["bits"], 32)
        self.assertEqual(by_name["out"]["cmp_scalar"], "n")
        self.assertIsNone(spec["ret"])
        self.assertEqual([s["name"] for s in spec["scalars"]], ["n"])

    def test_bundle_contents(self):
        analysis, cfg = self._analysis()
        bundle = generate_verilog_testbenches(analysis, cfg)
        self.assertIn("vector_add_ref", bundle.vectors_tb)
        self.assertIn('#include "../input.c"', bundle.vectors_tb)
        self.assertIn("rtl_vec_a.mem", bundle.vectors_tb)
        self.assertIn("rtl_exp_out.mem", bundle.vectors_tb)
        self.assertIn("rtl_cmp_out.mem", bundle.vectors_tb)
        self.assertIn("--from-rtl", bundle.gen_script)
        self.assertIn("parse_rtl_ports", bundle.gen_script)
        self.assertIn("RTL_TB: PASS", bundle.gen_script)
        self.assertIn("rtl_tb_report.json", bundle.run_script)
        self.assertIn("iverilog", bundle.run_script)
        self.assertIn("xvlog", bundle.run_script)
        self.assertIn("vector_add", bundle.manifest_json)

    def test_written_project_has_rtl_bundle(self):
        project = self._write_project()
        for rel in ("tb/rtl_vectors_tb.cpp", "tb/gen_rtl_tb.py", "tb/run_rtl_sim.py", "tb/rtl_tb_manifest.json"):
            self.assertTrue((project / rel).exists(), rel)
        makefile = (project / "Makefile").read_text(encoding="utf-8")
        for target in ("rtl-vectors:", "rtl-testbench:", "rtl-cosim:"):
            self.assertIn(target, makefile)

    @unittest.skipUnless(_tools("python3"), "python3 required")
    def test_gen_from_contract_renders_sv(self):
        project = self._write_project()
        run = subprocess.run(
            ["python3", "tb/gen_rtl_tb.py", "--from-contract"],
            cwd=project, text=True, capture_output=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        sv = (project / "tb" / "vector_add_tb.sv").read_text(encoding="utf-8")
        self.assertIn("module vector_add_tb;", sv)
        self.assertIn("vector_add dut (", sv)
        self.assertIn(".ap_start(ap_start)", sv)
        self.assertIn(".out_we0(out_we0)", sv)
        self.assertIn(".a_q0(a_q0)", sv)
        self.assertIn(".n(n)", sv)
        self.assertIn('$readmemh("rtl_vectors/rtl_vec_a.mem"', sv)
        self.assertIn("ap_rst = 1'b1;", sv)  # active-high reset asserted by default

    @unittest.skipUnless(_tools("python3"), "python3 required")
    def test_gen_from_rtl_detects_reset_and_dual_port(self):
        project = self._write_project()
        rtl = project / "fake_top.v"
        rtl.write_text(
            """
            module vector_add (ap_clk, ap_rst_n, ap_start, ap_done, ap_idle, ap_ready,
              a_address0, a_ce0, a_q0, a_address1, a_ce1, a_q1,
              b_address0, b_ce0, b_q0,
              out_address0, out_ce0, out_we0, out_d0, n);
              input ap_clk; input ap_rst_n; input ap_start;
              output ap_done; output ap_idle; output ap_ready;
              output [1:0] a_address0; output a_ce0; input [31:0] a_q0;
              output [1:0] a_address1; output a_ce1; input [31:0] a_q1;
              output [1:0] b_address0; output b_ce0; input [31:0] b_q0;
              output [1:0] out_address0; output out_ce0; output out_we0; output [31:0] out_d0;
              input [31:0] n;
            endmodule
            """,
            encoding="utf-8",
        )
        run = subprocess.run(
            ["python3", "tb/gen_rtl_tb.py", "--from-rtl", str(rtl)],
            cwd=project, text=True, capture_output=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        sv = (project / "tb" / "vector_add_tb.sv").read_text(encoding="utf-8")
        self.assertIn("ap_rst_n", sv)
        self.assertNotIn(".ap_rst(", sv)
        self.assertIn("a_address1", sv)  # dual-port detected
        self.assertIn("if (a_ce1) a_q1 <= a_ram[a_address1];", sv)

    @unittest.skipUnless(_tools("python3"), "python3 required")
    def test_gen_from_rtl_parses_ansi_header(self):
        project = self._write_project()
        rtl = project / "fake_ansi.v"
        rtl.write_text(
            """
            module vector_add (
              input ap_clk, input ap_rst_n, input ap_start,
              output ap_done, output ap_idle, output ap_ready,
              output [1:0] a_address0, output a_ce0, input [31:0] a_q0,
              output [1:0] b_address0, output b_ce0, input [31:0] b_q0,
              output [1:0] out_address0, output out_ce0, output out_we0, output [31:0] out_d0,
              input [31:0] n
            );
            endmodule
            """,
            encoding="utf-8",
        )
        run = subprocess.run(
            ["python3", "tb/gen_rtl_tb.py", "--from-rtl", str(rtl)],
            cwd=project, text=True, capture_output=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        sv = (project / "tb" / "vector_add_tb.sv").read_text(encoding="utf-8")
        self.assertIn("ap_rst_n", sv)  # ANSI header was actually parsed
        self.assertIn(".a_q0(a_q0)", sv)
        self.assertIn(".out_we0(out_we0)", sv)

    @unittest.skipUnless(_tools("python3"), "python3 required")
    def test_gen_from_rtl_ties_ap_continue_and_widens_we_bus(self):
        project = self._write_project()
        rtl = project / "fake_chain.v"
        rtl.write_text(
            """
            module vector_add (ap_clk, ap_rst, ap_start, ap_done, ap_idle, ap_ready, ap_continue,
              a_address0, a_ce0, a_q0, b_address0, b_ce0, b_q0,
              out_address0, out_ce0, out_we0, out_d0, n);
              input ap_clk; input ap_rst; input ap_start; input ap_continue;
              output ap_done; output ap_idle; output ap_ready;
              output [1:0] a_address0; output a_ce0; input [31:0] a_q0;
              output [1:0] b_address0; output b_ce0; input [31:0] b_q0;
              output [1:0] out_address0; output out_ce0; output [3:0] out_we0; output [31:0] out_d0;
              input [31:0] n;
            endmodule
            """,
            encoding="utf-8",
        )
        run = subprocess.run(
            ["python3", "tb/gen_rtl_tb.py", "--from-rtl", str(rtl)],
            cwd=project, text=True, capture_output=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        sv = (project / "tb" / "vector_add_tb.sv").read_text(encoding="utf-8")
        self.assertIn(".ap_continue(1'b1)", sv)  # tied high so a chain top does not stall
        self.assertIn("wire [3:0] out_we0;", sv)  # byte-enable we bus widened from the netlist

    @unittest.skipUnless(_tools("python3"), "python3 required")
    def test_scalar_named_verilog_keyword_is_escaped(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "input.c"
        path.write_text(
            "#include <stdint.h>\n"
            "void fill(int32_t *o, int type, int n) { for (int i=0;i<n;++i) o[i]=type; }\n",
            encoding="utf-8",
        )
        cfg = AgentConfig(
            top="fill",
            num_tests=4,
            interface_mode="ap_memory",
            arguments={
                "o": ArgumentConfig(direction="output", length=4),
                "type": ArgumentConfig(range=(-50, 50)),
                "n": ArgumentConfig(range=(0, 4)),
            },
        )
        analysis = analyze_source(path, "fill", cfg)
        spec = build_spec(analysis, cfg)
        self.assertTrue(any("reserved word" in n for n in spec["notes"]))

        generated = generate_hls_sources(analysis, cfg)
        project = Path(tmp.name) / "project"
        write_project(project, analysis, generated, cfg)
        run = subprocess.run(
            ["python3", "tb/gen_rtl_tb.py", "--from-contract"],
            cwd=project, text=True, capture_output=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        sv = (project / "tb" / "fill_tb.sv").read_text(encoding="utf-8")
        self.assertIn("reg [31:0] \\type ;", sv)  # escaped declaration
        self.assertIn(".\\type (\\type )", sv)  # escaped port connection

    @unittest.skipUnless(_tools("g++", "make", "python3"), "g++, make, python3 required")
    def test_vectors_build_and_counts(self):
        project = self._write_project(num_tests=8)
        run = subprocess.run(["make", "-C", str(project), "rtl-vectors"], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        vdir = project / "rtl_vectors"
        self.assertEqual(len((vdir / "rtl_vec_a.mem").read_text().split()), 8 * 4)
        self.assertEqual(len((vdir / "rtl_scalar_n.mem").read_text().split()), 8)
        self.assertEqual(len((vdir / "rtl_exp_out.mem").read_text().split()), 8 * 4)
        self.assertEqual(len((vdir / "rtl_cmp_out.mem").read_text().split()), 8)

    @unittest.skipUnless(_tools("python3", "g++"), "python3 and g++ required")
    def test_rtl_cosim_skips_without_synthesized_rtl(self):
        project = self._write_project()
        run = subprocess.run(["python3", "tb/run_rtl_sim.py"], cwd=project, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = project / "coverage" / "rtl_tb_report.json"
        self.assertTrue(report.exists())
        self.assertIn('"status": "skipped"', report.read_text(encoding="utf-8"))

    @unittest.skipUnless(_tools("iverilog", "vvp", "g++", "python3"), "iverilog, vvp, g++, python3 required")
    def test_rtl_cosim_end_to_end_pass_and_fail(self):
        project = self._write_project(num_tests=16)
        rtl_dir = project / "c2hlsc_project" / "solution1" / "syn" / "verilog"
        rtl_dir.mkdir(parents=True, exist_ok=True)
        rtl_file = rtl_dir / "vector_add.v"
        rtl_file.write_text(CORRECT_VECTOR_ADD_RTL, encoding="utf-8")

        ok = subprocess.run(["python3", "tb/run_rtl_sim.py"], cwd=project, text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        report = (project / "coverage" / "rtl_tb_report.json").read_text(encoding="utf-8")
        self.assertIn('"status": "pass"', report)

        # A functional bug in the RTL must be caught.
        rtl_file.write_text(CORRECT_VECTOR_ADD_RTL.replace("a_q0 + b_q0", "a_q0 - b_q0"), encoding="utf-8")
        shutil.rmtree(project / "coverage")
        bad = subprocess.run(["python3", "tb/run_rtl_sim.py"], cwd=project, text=True, capture_output=True)
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        report = (project / "coverage" / "rtl_tb_report.json").read_text(encoding="utf-8")
        self.assertIn('"status": "fail"', report)

    @unittest.skipUnless(_tools("iverilog", "vvp", "g++", "python3"), "iverilog, vvp, g++, python3 required")
    def test_rtl_tb_fails_closed_when_a_golden_vector_file_is_missing(self):
        project = self._write_project(num_tests=8)
        rtl_dir = project / "c2hlsc_project" / "solution1" / "syn" / "verilog"
        rtl_dir.mkdir(parents=True, exist_ok=True)
        (rtl_dir / "vector_add.v").write_text(CORRECT_VECTOR_ADD_RTL, encoding="utf-8")
        ok = subprocess.run(
            ["python3", "tb/run_rtl_sim.py"], cwd=project, text=True, capture_output=True
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

        (project / "rtl_vectors" / "rtl_cmp_out.mem").unlink()
        vvp = project / "coverage" / "missing_vec.vvp"
        compiled = subprocess.run(
            [
                "iverilog",
                "-g2012",
                "-o",
                str(vvp),
                "-s",
                "vector_add_tb",
                "tb/vector_add_tb.sv",
                "c2hlsc_project/solution1/syn/verilog/vector_add.v",
            ],
            cwd=project,
            text=True,
            capture_output=True,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        sim = subprocess.run(["vvp", str(vvp)], cwd=project, text=True, capture_output=True)
        self.assertIn("RTL_TB: FAIL", sim.stdout)
        self.assertNotIn("RTL_TB: PASS", sim.stdout)
        self.assertIn("compares=0", sim.stdout)

        again = subprocess.run(
            ["python3", "tb/run_rtl_sim.py"], cwd=project, text=True, capture_output=True
        )
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertTrue((project / "rtl_vectors" / "rtl_cmp_out.mem").is_file())
        report = (project / "coverage" / "rtl_tb_report.json").read_text(encoding="utf-8")
        self.assertIn('"status": "pass"', report)

    @unittest.skipUnless(_tools("iverilog", "vvp", "g++", "python3"), "iverilog, vvp, g++, python3 required")
    def test_all_advisory_comparisons_report_advisory_not_pass(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "input.c"
        path.write_text(
            "void scale(const float *x, float *y, int n) {\n"
            "  for (int i = 0; i < n; ++i) y[i] = x[i] * 2.0f;\n"
            "}\n",
            encoding="utf-8",
        )
        cfg = AgentConfig(
            top="scale",
            num_tests=8,
            interface_mode="ap_memory",
            arguments={
                "x": ArgumentConfig(direction="input", length=4),
                "y": ArgumentConfig(direction="output", length=4),
                "n": ArgumentConfig(range=(1, 4)),
            },
        )
        analysis = analyze_source(path, "scale", cfg)
        project = Path(tmp.name) / "project"
        write_project(project, analysis, generate_hls_sources(analysis, cfg), cfg)
        rtl_dir = project / "c2hlsc_project" / "solution1" / "syn" / "verilog"
        rtl_dir.mkdir(parents=True, exist_ok=True)
        (rtl_dir / "scale.v").write_text(IDLE_FLOAT_RTL, encoding="utf-8")

        run = subprocess.run(
            ["python3", "tb/run_rtl_sim.py"], cwd=project, text=True, capture_output=True
        )
        report = (project / "coverage" / "rtl_tb_report.json").read_text(encoding="utf-8")
        self.assertIn('"status": "advisory"', report)
        self.assertNotIn('"status": "pass"', report)
        self.assertIn("RTL_TB: ADVISORY", report)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
