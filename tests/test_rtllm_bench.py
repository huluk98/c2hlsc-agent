import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.rtllm_bench import (
    FAILURE_FAMILIES,
    ILLEGAL_TASK_MARKER,
    IVERILOG_STANDARD,
    KNOWN_ORACLE_ISSUES,
    LOG_TAIL_CHARS,
    TESTBENCH_SHIMS,
    VACUOUS_ORACLE_DESIGNS,
    RtllmDesign,
    SimResult,
    apply_testbench_shims,
    classify_failure,
    classify_output,
    discover_designs,
    empty_stub_rtl,
    evaluate_empty_stub,
    evaluate_reference,
    evaluate_rtl,
    find_illegal_system_tasks,
    reference_rtl_text,
    shim_rationale,
)

HAS_IVERILOG = shutil.which("iverilog") is not None and shutil.which("vvp") is not None

PASS_BANNER = "===========Your Design Passed==========="

# Exact source lines from the RTLLM v2.0 testbenches the shims target. Kept verbatim so
# the shim regexes cannot silently rot away from the benchmark they are written for.
UPSTREAM_RING_COUNTER_DECL = (
    "    reg [7:0] data [0:9] = {8'b00000001, 8'b00000001, 8'b00000010, 8'b00000100, "
    "8'b00001000,8'b00010000, 8'b00100000, 8'b01000000, 8'b10000000, 8'b00000001};"
)
UPSTREAM_ASYN_FIFO_LOOP = """  initial begin
  repeat (17) begin
    #20;
    if (wfull) begin
      // $display("FIFO is full (wfull=1) at depth %d", $time);
      break;
    end
    winc = 1; // Enable write
    wdata = wdata + 1; // Write data
    #10;
    winc = 0; // Disable write
  end
  end
"""

TINY_ADDER_RTL = """module tiny_adder(input [3:0] a, input [3:0] b, output [4:0] sum);
    assign sum = a + b;
endmodule
"""

TINY_ADDER_TB = """module testbench;
    reg [3:0] a, b;
    wire [4:0] sum;
    integer i;
    integer error = 0;

    tiny_adder dut(.a(a), .b(b), .sum(sum));

    initial begin
        for (i = 0; i < 16; i = i + 1) begin
            a = i;
            b = 4'd3;
            #5;
            if (sum !== (a + b)) begin
                error = error + 1;
                $display("Failed at a=%d b=%d sum=%d", a, b, sum);
            end
        end
        if (error == 0) begin
            $display("__PASS_BANNER__");
        end else begin
            $display("===========Test completed with %d failures===========", error);
        end
        $finish;
    end
endmodule
""".replace("__PASS_BANNER__", PASS_BANNER)

TINY_ROM_RTL = """module tiny_rom(input [1:0] addr, output [7:0] q);
    assign q = (addr == 2'd0) ? 8'h11 :
               (addr == 2'd1) ? 8'h22 :
               (addr == 2'd2) ? 8'h33 : 8'h44;
endmodule
"""

TINY_ROM_TB = """module testbench;
    reg [7:0] golden [0:3];
    reg [1:0] addr;
    wire [7:0] q;
    integer i;
    integer error = 0;

    tiny_rom dut(.addr(addr), .q(q));

    initial begin
        $readmemh("golden.dat", golden);
        for (i = 0; i < 4; i = i + 1) begin
            addr = i;
            #5;
            if (q !== golden[i]) begin
                error = error + 1;
                $display("Failed at addr=%d", addr);
            end
        end
        if (error == 0) begin
            $display("__PASS_BANNER__");
        end else begin
            $display("===========Error===========");
        end
        $finish;
    end
endmodule
""".replace("__PASS_BANNER__", PASS_BANNER)


def _write_design(root, category, name, testbench, reference, support=None):
    directory = root / category / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "design_description.txt").write_text(
        "Please implement %s.\n" % name, encoding="utf-8"
    )
    (directory / "testbench.v").write_text(testbench, encoding="utf-8")
    (directory / ("verified_%s.v" % name)).write_text(reference, encoding="utf-8")
    (directory / "makefile").write_text("vcs:\n\tvcs %s.v testbench.v\n" % name, encoding="utf-8")
    for filename, text in (support or {}).items():
        (directory / filename).write_text(text, encoding="utf-8")
    return directory


def _make_benchmark(root):
    """A two-design stand-in for the real RTLLM tree, plus a _chatgpt decoy."""

    _write_design(
        root,
        "Arithmetic/Adder",
        "tiny_adder",
        TINY_ADDER_TB,
        TINY_ADDER_RTL.replace("tiny_adder", "verified_tiny_adder"),
    )
    _write_design(
        root,
        "Memory/ROM",
        "tiny_rom",
        TINY_ROM_TB,
        TINY_ROM_RTL.replace("tiny_rom", "verified_tiny_rom"),
        support={"golden.dat": "11\n22\n33\n44\n"},
    )
    _write_design(
        root,
        "_chatgpt4/Arithmetic/Adder",
        "tiny_adder",
        TINY_ADDER_TB,
        TINY_ADDER_RTL,
    )
    return root


class ClassifyOutputTests(unittest.TestCase):
    def test_pass_banner_is_official_and_strict(self):
        self.assertEqual(classify_output(PASS_BANNER), (True, True))

    def test_failed_banner_is_not_a_pass(self):
        # Regression: "===========Failed===========" contains no "Pass"/"pass" and must
        # never be read as a pass, in either oracle.
        self.assertEqual(classify_output("===========Failed===========          3"), (False, False))

    def test_failure_banner_plus_stray_pass_word_splits_the_oracles(self):
        # Regression: several RTLLM testbenches print per-vector failures and still reach a
        # banner containing "Passed". Official must stay True (that is literally the
        # benchmark rule) while strict must be False.
        text = "Failed at i=3, out=00000000, expected=00000100\n" + PASS_BANNER
        self.assertEqual(classify_output(text), (True, False))

    def test_timeout_never_counts_as_strict(self):
        self.assertEqual(classify_output(PASS_BANNER, timed_out=True), (True, False))

    def test_test_completed_with_failures_is_a_failure(self):
        text = "=========== Test completed with          20 failures ==========="
        self.assertEqual(classify_output(text), (False, False))

    def test_error_lines_defeat_strict(self):
        text = "Error: dividend=156, divisor= 10, expected=00f6, got=faf1\n" + PASS_BANNER
        official, strict = classify_output(text)
        self.assertTrue(official)
        self.assertFalse(strict)

    def test_empty_output(self):
        self.assertEqual(classify_output(""), (False, False))
        self.assertEqual(classify_output(None), (False, False))

    def test_official_rule_is_the_benchmark_rule_warts_and_all(self):
        # auto_run.py greps for the bare substring; documenting that we did not "fix" it.
        self.assertEqual(classify_output("the bus is bypassed")[0], True)


class ClassifyFailureTests(unittest.TestCase):
    def test_all_returned_families_are_declared(self):
        cases = [
            ("tb.v:2: error: Unknown module type: dut", "", False, False),
            ("tb.v:20: sorry: break statements not supported.", "", False, False),
            ("tb.v:2: error: port ``zzz'' is not a port of u.", "", False, False),
            ("tb.v:4: syntax error", "", False, False),
            ("", "starting up\n", True, True),
            ("", "ERROR: tb.v:2: $readmemh: Unable to open golden.dat for reading.", True, False),
            ("", "", True, False),
            ("", "Failed at i=0", True, False),
        ]
        for compile_log, sim_log, syntax_pass, timed_out in cases:
            family = classify_failure(compile_log, sim_log, syntax_pass, timed_out)
            self.assertIn(family, FAILURE_FAMILIES, msg=repr((compile_log, sim_log)))

    def test_missing_module(self):
        log = "tb.v:2: error: Unknown module type: tiny_adder\n2 error(s) during elaboration."
        self.assertEqual(classify_failure(log, "", False, False), "missing_module")

    def test_simulator_unsupported(self):
        log = "testbench.v:102: sorry: break statements not supported."
        self.assertEqual(classify_failure(log, "", False, False), "simulator_unsupported")

    def test_port_mismatch(self):
        log = "testbench.v:2: error: port ``zzz'' is not a port of u."
        self.assertEqual(classify_failure(log, "", False, False), "port_mismatch")

    def test_generic_compile_error(self):
        self.assertEqual(classify_failure("testbench.v:9: syntax error", "", False, False), "compile_error")

    def test_timeout_beats_other_sim_evidence(self):
        self.assertEqual(classify_failure("", "starting up\n", True, True), "timeout")

    def test_missing_golden_data(self):
        log = "ERROR: testbench.v:43: $readmemh: Unable to open reference.dat for reading."
        self.assertEqual(classify_failure("", log, True, False), "missing_golden_data")

    def test_no_output(self):
        self.assertEqual(classify_failure("", "   \n", True, False), "no_output")

    def test_functional_mismatch(self):
        log = "=========== Test completed with 20 failures ==========="
        self.assertEqual(classify_failure("", log, True, False), "functional_mismatch")

    def test_strict_pass_returns_none(self):
        self.assertIsNone(classify_failure("", PASS_BANNER, True, False))

    def test_official_but_not_strict_still_reports_a_family(self):
        # The repair agent needs evidence even when the generous oracle says "pass".
        log = "Failed at i=3\n" + PASS_BANNER
        self.assertEqual(classify_failure("", log, True, False), "functional_mismatch")


class ReferenceRtlTextTests(unittest.TestCase):
    def _design(self, tmp, name, text, extra=None):
        directory = tmp / name
        directory.mkdir(parents=True, exist_ok=True)
        first = directory / "verified_something.v"
        first.write_text(text, encoding="utf-8")
        files = [first]
        if extra is not None:
            second = directory / "zz_extra.v"
            second.write_text(extra, encoding="utf-8")
            files.append(second)
        return RtllmDesign(
            name=name,
            category="X",
            directory=directory,
            description="",
            testbench=directory / "testbench.v",
            reference_files=tuple(files),
        )

    def test_renames_top_module_that_is_not_named_after_the_design(self):
        # Regression: adder_pipe_64bit ships `module verified_adder_64bit`, so a naive
        # "strip the verified_ prefix" or "verified_<design>" rule leaves the testbench
        # unable to find the DUT.
        with tempfile.TemporaryDirectory() as tmp:
            design = self._design(
                Path(tmp),
                "adder_pipe_64bit",
                "module verified_adder_64bit(input clk, output reg [63:0] sum);\n"
                "// verified_adder_64bit pipeline\nendmodule\n",
            )
            text = reference_rtl_text(design)
        self.assertIn("module adder_pipe_64bit(", text)
        self.assertNotIn("verified_adder_64bit", text)

    def test_renames_multi_pipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            design = self._design(
                Path(tmp),
                "multi_pipe_4bit",
                "module verified_multi_pipe(input clk);\nendmodule\n",
            )
            text = reference_rtl_text(design)
        self.assertIn("module multi_pipe_4bit(", text)

    def test_top_is_the_verified_module_not_the_first_module(self):
        # asyn_fifo declares a helper (dual_port_RAM) before the top.
        with tempfile.TemporaryDirectory() as tmp:
            design = self._design(
                Path(tmp),
                "asyn_fifo",
                "module dual_port_RAM(input clk);\nendmodule\n"
                "module verified_asyn_fifo(input wclk);\n dual_port_RAM r(.clk(wclk));\nendmodule\n",
            )
            text = reference_rtl_text(design)
        self.assertIn("module dual_port_RAM(", text)
        self.assertIn("module asyn_fifo(", text)
        self.assertNotIn("verified_asyn_fifo", text)

    def test_reference_without_verified_prefix_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            design = self._design(
                Path(tmp),
                "clkgenerator",
                "module clkgenerator(output reg clk);\nendmodule\n",
            )
            text = reference_rtl_text(design)
        self.assertIn("module clkgenerator(", text)

    def test_rename_applies_across_every_concatenated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            design = self._design(
                Path(tmp),
                "widget",
                "module verified_widget(input a);\nendmodule\n",
                extra="module wrapper(input a);\n verified_widget u(.a(a));\nendmodule\n",
            )
            text = reference_rtl_text(design)
        self.assertNotIn("verified_widget", text)
        self.assertIn("widget u(.a(a));", text)

    def test_no_reference_files(self):
        design = RtllmDesign(
            name="x",
            category="X",
            directory=Path("/nonexistent"),
            description="",
            testbench=Path("/nonexistent/testbench.v"),
            reference_files=(),
        )
        self.assertEqual(reference_rtl_text(design), "")


class ShimTests(unittest.TestCase):
    def test_shim_table_covers_only_the_two_known_designs(self):
        self.assertEqual(set(TESTBENCH_SHIMS), {"ring_counter", "asyn_fifo"})
        for name in TESTBENCH_SHIMS:
            self.assertTrue(shim_rationale(name))

    def test_unknown_design_is_untouched(self):
        text, applied = apply_testbench_shims("adder_8bit", "module tb; endmodule\n")
        self.assertFalse(applied)
        self.assertEqual(text, "module tb; endmodule\n")

    def test_ring_counter_shim_matches_the_upstream_declaration(self):
        text, applied = apply_testbench_shims("ring_counter", UPSTREAM_RING_COUNTER_DECL)
        self.assertTrue(applied)
        self.assertNotIn("] = {", text)
        expected = [
            "8'b00000001",
            "8'b00000001",
            "8'b00000010",
            "8'b00000100",
            "8'b00001000",
            "8'b00010000",
            "8'b00100000",
            "8'b01000000",
            "8'b10000000",
            "8'b00000001",
        ]
        for index, value in enumerate(expected):
            self.assertIn("data[%d] = %s;" % (index, value), text)
        # ...and in the original order.
        positions = [text.index("data[%d] =" % index) for index in range(10)]
        self.assertEqual(positions, sorted(positions))

    def test_asyn_fifo_shim_matches_the_upstream_loop(self):
        text, applied = apply_testbench_shims("asyn_fifo", UPSTREAM_ASYN_FIFO_LOOP)
        self.assertTrue(applied)
        self.assertIn("initial begin : rtllm_write_burst", text)
        self.assertIn("disable rtllm_write_burst;", text)
        self.assertNotIn("break;", text)
        # The write sequence itself is untouched.
        self.assertIn("wdata = wdata + 1;", text)
        self.assertIn("repeat (17)", text)

    @unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
    def test_ring_counter_shim_compiles_and_still_catches_bad_rtl(self):
        checker = (
            "module tb;\n"
            "    integer i;\n"
            "    integer error = 0;\n"
            "    reg [7:0] out;\n"
            + UPSTREAM_RING_COUNTER_DECL
            + "\n    initial begin\n"
            "        for (i = 0; i < 10; i = i + 1) begin\n"
            "            out = `DUT_VALUE;\n"
            "            if (out !== data[i]) begin\n"
            "                error = error + 1;\n"
            "                $display(\"Failed at i=%0d\", i);\n"
            "            end\n"
            "            #1;\n"
            "        end\n"
            "        if (error == 0) $display(\"" + PASS_BANNER + "\");\n"
            "        $finish;\n"
            "    end\n"
            "endmodule\n"
        )
        shimmed, applied = apply_testbench_shims("ring_counter", checker)
        self.assertTrue(applied)
        good = self._run_snippet(shimmed, "`define DUT_VALUE data[i]\n")
        self.assertIn(PASS_BANNER, good)
        bad = self._run_snippet(shimmed, "`define DUT_VALUE 8'b0\n")
        self.assertNotIn(PASS_BANNER, bad)
        self.assertIn("Failed at i=", bad)

    @unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
    def test_asyn_fifo_shim_compiles_and_preserves_the_write_sequence(self):
        loop = (
            "module tb;\n"
            "  reg wfull = 0;\n"
            "  reg [7:0] wdata = 0;\n"
            "  reg winc = 0;\n"
            "  initial #95 wfull = 1;\n"
            + UPSTREAM_ASYN_FIFO_LOOP
            + "  initial begin\n"
            "    #400;\n"
            "    $display(\"writes=%0d\", wdata);\n"
            "    $finish;\n"
            "  end\n"
            "endmodule\n"
        )
        shimmed, applied = apply_testbench_shims("asyn_fifo", loop)
        self.assertTrue(applied)
        # wfull rises at t=95; iterations complete at t=30, 60, 90 -> 3 writes, then the
        # 4th iteration sees wfull at t=110 and leaves the loop. `break` behaves the same.
        self.assertIn("writes=3", self._run_snippet(shimmed, ""))

    def _run_snippet(self, source, prologue):
        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / "snippet.v"
            path.write_text(prologue + source, encoding="utf-8")
            compiled = subprocess.run(
                ["iverilog", IVERILOG_STANDARD, "-o", "sim", "snippet.v"],
                cwd=str(tmp),
                capture_output=True,
                text=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            run = subprocess.run(["vvp", "sim"], cwd=str(tmp), capture_output=True, text=True, timeout=60)
            return run.stdout + run.stderr
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class IllegalSystemTaskTests(unittest.TestCase):
    """The oracle greps one stream that the design under test can also write to.

    Every test here is a way for a candidate to score without implementing anything. They
    all have to fail, and they have to fail BEFORE the simulator runs -- the verifier cannot
    tell a self-reported pass from a real one after the fact.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = _make_benchmark(self.tmp / "bench")
        self.design = {d.name: d for d in discover_designs(self.root)}["tiny_adder"]
        self.addCleanup(self._tmp.cleanup)

    def _evaluate(self, rtl, name="work"):
        return evaluate_rtl(self.design, rtl, self.tmp / name)

    def test_detects_output_and_control_tasks_with_line_numbers(self):
        rtl = "module m;\n  initial $display(\"hi\");\n  initial $finish;\nendmodule\n"
        self.assertEqual(find_illegal_system_tasks(rtl), ((2, "$display"), (3, "$finish")))

    def test_detects_the_whole_output_family_including_file_variants(self):
        # $fdisplay(1, ...) writes to stdout, so the $f* forms are hazards too.
        for token in (
            "$display", "$displayb", "$write", "$writeh", "$monitor", "$monitoron",
            "$strobe", "$fdisplay", "$fwrite", "$fmonitor", "$fstrobe",
            "$finish", "$stop", "$dumpvars", "$dumpfile",
        ):
            with self.subTest(token=token):
                found = find_illegal_system_tasks("module m;\n  initial %s;\nendmodule\n" % token)
                self.assertEqual([name for _line, name in found], [token])

    def test_legitimate_system_functions_are_not_flagged(self):
        rtl = (
            "module m;\n"
            "  reg [$clog2(64)-1:0] idx;\n"
            "  wire signed [7:0] s = $signed(8'hff);\n"
            "  wire [31:0] u = $unsigned(1), n = $bits(idx), t = $time, r = $random;\n"
            "endmodule\n"
        )
        self.assertEqual(find_illegal_system_tasks(rtl), ())

    def test_comments_and_string_literals_are_not_flagged(self):
        rtl = (
            "module m;\n"
            "  // no $display here\n"
            "  /* and no $finish here either */\n"
            '  parameter NOTE = "$display($finish)";  // a string, not a call\n'
            '  // a quote " inside a comment must not swallow the rest of the file\n'
            "endmodule\n"
        )
        self.assertEqual(find_illegal_system_tasks(rtl), ())

    @unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
    def test_a_candidate_that_prints_the_pass_banner_scores_zero(self):
        # Without the gate this scored syntax_pass=func_pass=func_pass_strict=True: $finish
        # at t=0 ends the run before the testbench can print its failure banner.
        rtl = (
            "module tiny_adder(input [3:0] a, input [3:0] b, output [4:0] sum);\n"
            "  assign sum = 5'b0;\n"
            "  initial begin\n"
            '    $display("%s");\n'
            "    $finish;\n"
            "  end\n"
            "endmodule\n" % PASS_BANNER
        )
        result = self._evaluate(rtl)
        self.assertFalse(result.syntax_pass)
        self.assertFalse(result.func_pass)
        self.assertFalse(result.func_pass_strict)
        self.assertEqual(result.failure_family, "illegal_system_task")
        self.assertNotIn(PASS_BANNER, result.sim_log)

    @unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
    def test_a_display_only_bypass_candidate_scores_zero(self):
        # The official oracle is a bare substring test, so ANY line containing "pass" from
        # the design would satisfy it -- the design must not be able to print at all.
        rtl = (
            "module tiny_adder(input [3:0] a, input [3:0] b, output [4:0] sum);\n"
            "  assign sum = 5'b0;\n"
            '  initial $display("bypass mode enabled");\n'
            "endmodule\n"
        )
        result = self._evaluate(rtl)
        self.assertFalse(result.func_pass)
        self.assertEqual(result.failure_family, "illegal_system_task")

    @unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
    def test_an_honest_candidate_is_unaffected(self):
        result = self._evaluate(TINY_ADDER_RTL, name="honest")
        self.assertTrue(result.syntax_pass)
        self.assertTrue(result.func_pass)
        self.assertTrue(result.func_pass_strict)

    def test_the_refusal_is_recoverable_from_the_stored_result(self):
        # classify_failure must be a pure function of the serialized SimResult, so a report
        # re-analyzed later reproduces the label instead of degrading it to compile_error.
        result = self._evaluate("module tiny_adder; initial $display(\"x\"); endmodule\n", name="stored")
        payload = result.to_dict()
        self.assertIn(ILLEGAL_TASK_MARKER, payload["compile_log"])
        self.assertEqual(
            classify_failure(payload["compile_log"], payload["sim_log"], payload["syntax_pass"], False),
            "illegal_system_task",
        )

    def test_the_refusal_survives_log_truncation(self):
        # Many violations must not push the marker out of the 4000-char tail slice.
        rtl = "module m;\n" + "  initial $display(\"x\");\n" * 500 + "endmodule\n"
        result = self._evaluate(rtl, name="many")
        payload = result.to_dict()
        self.assertLessEqual(len(payload["compile_log"]), LOG_TAIL_CHARS)
        self.assertIn(ILLEGAL_TASK_MARKER, payload["compile_log"])
        self.assertEqual(result.failure_family, "illegal_system_task")

    def test_the_benchmarks_own_reference_rtl_contains_no_illegal_task(self):
        # If a golden file did, the gate would break the reference baseline.
        design = {d.name: d for d in discover_designs(self.root)}["tiny_adder"]
        self.assertEqual(find_illegal_system_tasks(reference_rtl_text(design)), ())


class VacuousOracleTests(unittest.TestCase):
    """The mirror of KNOWN_ORACLE_ISSUES: oracles an EMPTY module passes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = _make_benchmark(self.tmp / "bench")
        self.addCleanup(self._tmp.cleanup)

    def test_every_vacuous_entry_is_documented(self):
        self.assertTrue(VACUOUS_ORACLE_DESIGNS)
        for name, reason in VACUOUS_ORACLE_DESIGNS.items():
            self.assertIsInstance(name, str)
            self.assertGreater(len(reason), 40, msg=name)

    def test_the_two_catalogues_are_disjoint(self):
        # A design cannot be both unpassable and passed-by-nothing; an overlap would mean
        # one of them was guessed rather than measured.
        self.assertEqual(set(VACUOUS_ORACLE_DESIGNS) & set(KNOWN_ORACLE_ISSUES), set())

    def test_empty_stub_keeps_the_ports_and_drops_every_statement(self):
        design = {d.name: d for d in discover_designs(self.root)}["tiny_adder"]
        stub = empty_stub_rtl(design)
        self.assertIn("module tiny_adder", stub)
        for port in ("a", "b", "sum"):
            self.assertIn(port, stub)
        self.assertNotIn("assign", stub)
        self.assertEqual(find_illegal_system_tasks(stub), ())

    def test_empty_stub_keeps_a_parameter_block_and_reaches_the_port_list(self):
        source = (
            "module verified_p #(\n  parameter W = 8\n) (\n  input [W-1:0] d,\n"
            "  output reg [W-1:0] q\n);\n  always @* q = d;\nendmodule\n"
        )
        directory = _write_design(self.tmp / "b2", "Misc", "p", "module testbench; endmodule\n", source)
        design = discover_designs(directory.parents[1])[0]
        stub = empty_stub_rtl(design)
        self.assertIn("parameter W = 8", stub)
        self.assertIn("input [W-1:0] d", stub)
        self.assertNotIn("always", stub)

    def test_empty_stub_falls_back_to_the_last_module_when_the_name_differs(self):
        # fixed_point_substractor ships `module fixed_point_subtractor` (helper modules come
        # first in RTLLM references), so an exact-name-only lookup would find nothing.
        source = "module helper(input h);\nendmodule\n\nmodule spelled_differently(input x, output y);\n  assign y = x;\nendmodule\n"
        directory = _write_design(self.tmp / "b3", "Misc", "q", "module testbench; endmodule\n", source)
        design = discover_designs(directory.parents[1])[0]
        stub = empty_stub_rtl(design)
        self.assertIn("module spelled_differently", stub)
        self.assertNotIn("helper", stub)

    def test_empty_stub_returns_empty_when_there_is_no_reference(self):
        design = RtllmDesign(
            name="x", category="", directory=self.tmp, description="", testbench=self.tmp / "tb.v",
            reference_files=(),
        )
        self.assertEqual(empty_stub_rtl(design), "")

    @unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
    def test_a_sound_oracle_rejects_the_empty_stub(self):
        design = {d.name: d for d in discover_designs(self.root)}["tiny_adder"]
        result = evaluate_empty_stub(design, self.tmp / "empty")
        self.assertTrue(result.syntax_pass, result.compile_log)
        self.assertFalse(result.func_pass)
        self.assertFalse(result.func_pass_strict)

    def test_design_to_dict_reports_both_oracle_flags(self):
        payload = discover_designs(self.root)[0].to_dict()
        self.assertIsNone(payload["known_oracle_issue"])
        self.assertIsNone(payload["vacuous_oracle"])


class RunawayOutputTests(unittest.TestCase):
    """A simulation must not be able to buy unbounded RAM or unbounded wall clock."""

    @unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
    def test_a_flooding_testbench_is_killed_and_bucketed(self):
        # Measured before the fix: 683 MB captured in one Python str and 29.4 s wall for a
        # 10 s sim timeout, i.e. an OOM kill of the sweep at --workers 8 with no report.
        flooding_tb = (
            "module testbench;\n"
            "    reg [3:0] a, b;\n"
            "    wire [4:0] sum;\n"
            "    integer n = 0;\n"
            "    tiny_adder dut(.a(a), .b(b), .sum(sum));\n"
            "    initial forever begin\n"
            "        #1 n = n + 1;\n"
            '        $display("%0d %s", n, "'
            + "X" * 200
            + '");\n'
            "    end\n"
            "endmodule\n"
        )
        tmp = Path(tempfile.mkdtemp())
        try:
            root = tmp / "bench"
            _write_design(
                root, "Misc", "tiny_adder", flooding_tb,
                TINY_ADDER_RTL.replace("tiny_adder", "verified_tiny_adder"),
            )
            design = discover_designs(root)[0]
            started = time.time()
            result = evaluate_rtl(design, TINY_ADDER_RTL, tmp / "work", sim_timeout=30)
            elapsed = time.time() - started
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertTrue(result.runaway_output)
        self.assertEqual(result.failure_family, "runaway_output")
        self.assertFalse(result.func_pass_strict)
        self.assertLessEqual(len(result.sim_log), LOG_TAIL_CHARS)
        # Killed on the output budget, well inside the 30 s watchdog it never reached.
        self.assertLess(elapsed, 20.0)

    def test_runaway_defeats_the_strict_oracle_and_is_stored(self):
        self.assertEqual(classify_output(PASS_BANNER, False, True), (True, False))
        self.assertEqual(classify_failure("", PASS_BANNER, True, False, True), "runaway_output")
        payload = SimResult(
            design="d", syntax_pass=True, func_pass=True, func_pass_strict=False,
            timed_out=False, compile_log="", sim_log=PASS_BANNER, duration_s=0.1,
            failure_family="runaway_output", runaway_output=True,
        ).to_dict()
        self.assertTrue(payload["runaway_output"])


class DiscoverDesignsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _make_benchmark(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_designs_and_skips_chatgpt_copies(self):
        designs = discover_designs(self.root)
        self.assertEqual([design.name for design in designs], ["tiny_adder", "tiny_rom"])
        for design in designs:
            self.assertNotIn("_chatgpt", str(design.directory))

    def test_category_is_the_path_without_the_design_directory(self):
        by_name = {design.name: design for design in discover_designs(self.root)}
        self.assertEqual(by_name["tiny_adder"].category, "Arithmetic/Adder")
        self.assertEqual(by_name["tiny_rom"].category, "Memory/ROM")

    def test_description_testbench_and_references(self):
        design = discover_designs(self.root)[0]
        self.assertIn("tiny_adder", design.description)
        self.assertEqual(design.testbench.name, "testbench.v")
        self.assertEqual([path.name for path in design.reference_files], ["verified_tiny_adder.v"])
        self.assertNotIn(design.testbench, design.reference_files)

    def test_support_files_exclude_rtl_makefile_and_description(self):
        by_name = {design.name: design for design in discover_designs(self.root)}
        names = [path.name for path in by_name["tiny_rom"].support_files]
        self.assertEqual(names, ["golden.dat"])
        self.assertEqual(list(by_name["tiny_adder"].support_files), [])

    def test_include_and_exclude_filter_on_name(self):
        self.assertEqual(
            [design.name for design in discover_designs(self.root, include=["tiny_rom"])],
            ["tiny_rom"],
        )
        self.assertEqual(
            [design.name for design in discover_designs(self.root, exclude=["tiny_rom"])],
            ["tiny_adder"],
        )
        self.assertEqual(discover_designs(self.root, include=["Tiny_Rom"]), [])

    def test_to_dict_is_json_friendly(self):
        payload = discover_designs(self.root)[0].to_dict()
        self.assertEqual(payload["name"], "tiny_adder")
        self.assertIsInstance(payload["directory"], str)
        self.assertIsInstance(payload["reference_files"], list)
        self.assertIsNone(payload["known_oracle_issue"])

    def test_known_oracle_issues_are_documented(self):
        self.assertTrue(KNOWN_ORACLE_ISSUES)
        for name, reason in KNOWN_ORACLE_ISSUES.items():
            self.assertIsInstance(name, str)
            self.assertGreater(len(reason), 40, msg=name)


class SimResultTests(unittest.TestCase):
    def test_to_dict_truncates_logs(self):
        result = SimResult(
            design="d",
            syntax_pass=True,
            func_pass=False,
            func_pass_strict=False,
            timed_out=False,
            compile_log="c" * (LOG_TAIL_CHARS + 500),
            sim_log="s" * (LOG_TAIL_CHARS + 500),
            duration_s=1.23456,
            failure_family="functional_mismatch",
        )
        payload = result.to_dict()
        self.assertEqual(len(payload["compile_log"]), LOG_TAIL_CHARS)
        self.assertEqual(len(payload["sim_log"]), LOG_TAIL_CHARS)
        self.assertEqual(payload["duration_s"], 1.235)
        self.assertFalse(payload["shim_applied"])

    @unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
    def test_long_sim_log_keeps_its_tail_and_truncation_marker(self):
        # The noise comes from the TESTBENCH: a candidate that prints is refused outright
        # (see IllegalSystemTaskTests), so it can no longer be used to produce a long log.
        noisy_tb = TINY_ADDER_TB.replace(
            "    initial begin\n",
            "    integer k;\n"
            "    initial for (k = 0; k < 4000; k = k + 1)\n"
            '        $display("noise line %0d aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", k);\n'
            "    initial begin\n",
            1,
        )
        tmp = Path(tempfile.mkdtemp())
        try:
            root = tmp / "bench"
            _write_design(
                root,
                "Arithmetic/Adder",
                "tiny_adder",
                noisy_tb,
                TINY_ADDER_RTL.replace("tiny_adder", "verified_tiny_adder"),
            )
            design = discover_designs(root)[0]
            result = evaluate_rtl(design, TINY_ADDER_RTL, tmp / "work")
            payload = result.to_dict()
            self.assertEqual(len(payload["sim_log"]), LOG_TAIL_CHARS)
            self.assertTrue(payload["sim_log"].startswith("...[log truncated]..."))
            self.assertIn("$finish called", payload["sim_log"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
class EvaluateRtlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = _make_benchmark(self.tmp / "bench")
        self.designs = {design.name: design for design in discover_designs(self.root)}

    def tearDown(self):
        self._tmp.cleanup()

    def test_correct_rtl_passes_both_oracles(self):
        result = evaluate_rtl(self.designs["tiny_adder"], TINY_ADDER_RTL, self.tmp / "w1")
        self.assertTrue(result.syntax_pass, result.compile_log)
        self.assertTrue(result.func_pass, result.sim_log)
        self.assertTrue(result.func_pass_strict, result.sim_log)
        self.assertIsNone(result.failure_family)
        self.assertFalse(result.shim_applied)
        self.assertGreaterEqual(result.duration_s, 0.0)

    def test_wrong_rtl_is_a_functional_mismatch(self):
        rtl = "module tiny_adder(input [3:0] a, input [3:0] b, output [4:0] sum);\n" \
              "    assign sum = 5'b0;\nendmodule\n"
        result = evaluate_rtl(self.designs["tiny_adder"], rtl, self.tmp / "w2")
        self.assertTrue(result.syntax_pass)
        self.assertFalse(result.func_pass)
        self.assertEqual(result.failure_family, "functional_mismatch")
        self.assertIn("Failed at", result.sim_log)

    def test_syntax_error_is_reported_not_raised(self):
        result = evaluate_rtl(self.designs["tiny_adder"], "module tiny_adder(;\n", self.tmp / "w3")
        self.assertFalse(result.syntax_pass)
        self.assertFalse(result.func_pass)
        self.assertEqual(result.sim_log, "")
        self.assertIn("error", result.compile_log.lower())
        self.assertIn(result.failure_family, ("compile_error", "port_mismatch"))

    def test_missing_module_family(self):
        rtl = "module not_the_dut(input a);\nendmodule\n"
        result = evaluate_rtl(self.designs["tiny_adder"], rtl, self.tmp / "w4")
        self.assertFalse(result.syntax_pass)
        self.assertEqual(result.failure_family, "missing_module")

    def test_port_mismatch_family(self):
        rtl = "module tiny_adder(input [3:0] a, input [3:0] b, output [4:0] total);\n" \
              "    assign total = a + b;\nendmodule\n"
        result = evaluate_rtl(self.designs["tiny_adder"], rtl, self.tmp / "w5")
        self.assertFalse(result.syntax_pass)
        self.assertEqual(result.failure_family, "port_mismatch")

    def test_readmemh_support_files_are_copied_into_the_sandbox(self):
        workdir = self.tmp / "w6"
        result = evaluate_rtl(self.designs["tiny_rom"], TINY_ROM_RTL, workdir)
        self.assertTrue((workdir / "golden.dat").is_file())
        self.assertTrue(result.func_pass_strict, result.sim_log)
        self.assertNotIn("Unable to open", result.sim_log)

    def test_sandbox_never_receives_the_golden_rtl(self):
        workdir = self.tmp / "w7"
        evaluate_rtl(self.designs["tiny_adder"], TINY_ADDER_RTL, workdir)
        verilog = sorted(path.name for path in workdir.glob("*.v"))
        self.assertEqual(verilog, ["testbench.v", "tiny_adder.v"])
        self.assertNotIn("verified", (workdir / "tiny_adder.v").read_text(encoding="utf-8"))

    def test_unwritable_sandbox_returns_a_result_instead_of_raising(self):
        blocker = self.tmp / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        result = evaluate_rtl(self.designs["tiny_adder"], TINY_ADDER_RTL, blocker)
        self.assertFalse(result.syntax_pass)
        self.assertFalse(result.func_pass)
        self.assertEqual(result.failure_family, "compile_error")
        self.assertTrue(result.compile_log)

    def test_evaluate_reference_runs_the_golden_rtl(self):
        result = evaluate_reference(self.designs["tiny_adder"], self.tmp / "w8")
        self.assertTrue(result.syntax_pass, result.compile_log)
        self.assertTrue(result.func_pass_strict, result.sim_log)

    def test_hung_simulation_times_out_without_raising(self):
        directory = _write_design(
            self.root,
            "Misc/Hang",
            "hang",
            "module testbench;\n"
            "    wire y;\n"
            "    reg clk = 0;\n"
            "    integer n = 0;\n"
            "    hang dut(.clk(clk), .y(y));\n"
            "    always #5 clk = ~clk;\n"
            "    initial $display(\"starting\");\n"
            "    initial forever #10 n = n + 1;\n"
            "endmodule\n",
            "module verified_hang(input clk, output y);\n    assign y = clk;\nendmodule\n",
        )
        self.assertTrue(directory.is_dir())
        design = {d.name: d for d in discover_designs(self.root)}["hang"]
        result = evaluate_rtl(
            design,
            "module hang(input clk, output y);\n    assign y = clk;\nendmodule\n",
            self.tmp / "w9",
            sim_timeout=3,
        )
        self.assertTrue(result.syntax_pass, result.compile_log)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.func_pass)
        self.assertEqual(result.failure_family, "timeout")
        self.assertIn("starting", result.sim_log)
        self.assertLess(result.duration_s, 30)

    def test_parallel_evaluations_do_not_interfere(self):
        from concurrent.futures import ThreadPoolExecutor

        design = self.designs["tiny_adder"]
        jobs = [(TINY_ADDER_RTL, self.tmp / ("p%d" % index)) for index in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda job: evaluate_rtl(design, job[0], job[1]), jobs))
        self.assertTrue(all(result.func_pass_strict for result in results))


if __name__ == "__main__":
    unittest.main()
