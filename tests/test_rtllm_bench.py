import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.rtllm_bench import (
    DIAG_MARKER,
    FAILURE_FAMILIES,
    ILLEGAL_TASK_MARKER,
    IVERILOG_STANDARD,
    KNOWN_ORACLE_ISSUES,
    LOG_TAIL_CHARS,
    MAX_TRACE_SIGNALS,
    TESTBENCH_SHIMS,
    TRACE_MARKER,
    VACUOUS_ORACLE_DESIGNS,
    RtllmDesign,
    SimResult,
    TraceSample,
    apply_testbench_shims,
    bounded_stop_time,
    candidate_trace_signals,
    classify_failure,
    classify_output,
    diagnose_timeout,
    discover_designs,
    empty_stub_rtl,
    evaluate_empty_stub,
    evaluate_reference,
    evaluate_rtl,
    filter_trace_lines,
    find_illegal_system_tasks,
    instrument_rtl,
    oracle_behaviour_diff,
    parse_trace,
    reference_rtl_text,
    run_self_trace,
    shim_rationale,
    trace_digest,
)
from c2hlsc_agent.rtllm_bench import TimeoutDiagnosis
# Aliased: pytest collects any module-level name starting with "test" as a test function.
from c2hlsc_agent.rtllm_bench import testbench_timescale as read_testbench_timescale
from c2hlsc_agent.rtllm_bench import _BoundedCapture, _stuck_and_oscillating

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


# --------------------------------------------------------------------------- #
# self-instrumentation: signal discovery and the instrumented copy
# --------------------------------------------------------------------------- #


class CandidateTraceSignalTests(unittest.TestCase):
    """What the instrumentation decides to print, derived from the candidate ALONE."""

    def test_ansi_header_ports_then_body_registers(self):
        rtl = (
            "module dut(input clk, input [7:0] a, output reg [7:0] q);\n"
            "    reg [3:0] count;\n"
            "    integer ticks;\n"
            "    always @(posedge clk) q <= a;\n"
            "endmodule\n"
        )
        self.assertEqual(
            candidate_trace_signals(rtl, "dut"), ("clk", "a", "q", "count", "ticks")
        )

    def test_non_ansi_header_takes_directions_from_the_body(self):
        rtl = (
            "module dut(clk, a, q);\n"
            "    input clk;\n"
            "    input [7:0] a;\n"
            "    output [7:0] q;\n"
            "    reg [7:0] q;\n"
            "endmodule\n"
        )
        self.assertEqual(candidate_trace_signals(rtl, "dut"), ("clk", "a", "q"))

    def test_multiple_names_in_one_declaration(self):
        rtl = "module dut(input a);\n    reg [1:0] x, y, z;\nendmodule\n"
        self.assertEqual(candidate_trace_signals(rtl, "dut"), ("a", "x", "y", "z"))

    def test_memory_arrays_are_never_traced(self):
        # `$strobe(..., mem)` on an unpacked array does not elaborate, and one bad argument
        # kills the whole instrumented copy -- so the memory is dropped, not the trace.
        rtl = (
            "module dut(input clk, output reg [7:0] q);\n"
            "    reg [7:0] mem [0:255];\n"
            "    reg [7:0] shadow;\n"
            "endmodule\n"
        )
        signals = candidate_trace_signals(rtl, "dut")
        self.assertNotIn("mem", signals)
        self.assertEqual(signals, ("clk", "q", "shadow"))

    def test_a_parameter_block_is_stepped_over_to_reach_the_ports(self):
        rtl = (
            "module dut #(parameter WIDTH = 8) (input clk, output [WIDTH-1:0] q);\n"
            "endmodule\n"
        )
        self.assertEqual(candidate_trace_signals(rtl, "dut"), ("clk", "q"))

    def test_the_signal_list_is_capped(self):
        ports = ", ".join("input p%d" % index for index in range(MAX_TRACE_SIGNALS + 10))
        rtl = "module dut(%s);\nendmodule\n" % ports
        signals = candidate_trace_signals(rtl, "dut")
        self.assertEqual(len(signals), MAX_TRACE_SIGNALS)
        self.assertEqual(signals[0], "p0")  # ports first, in declaration order

    def test_a_missing_module_yields_no_signals(self):
        self.assertEqual(candidate_trace_signals("// just a comment\n", "dut"), ())


class InstrumentRtlTests(unittest.TestCase):
    RTL = (
        "module dut(input clk, input [7:0] a, output reg [7:0] q);\n"
        "    always @(posedge clk) q <= a;\n"
        "endmodule\n"
    )

    def test_the_original_text_is_preserved_inside_the_copy(self):
        instrumented = instrument_rtl(self.RTL, "dut")
        self.assertIn("always @(posedge clk) q <= a;", instrumented)
        self.assertIn(TRACE_MARKER, instrumented)
        self.assertIn("$strobe", instrumented)
        # and the input string is untouched -- the scored candidate is a separate object
        self.assertNotIn(TRACE_MARKER, self.RTL)

    def test_the_probe_is_strobe_not_monitor(self):
        # $monitor is a simulator-wide singleton and 8 of the 50 RTLLM testbenches install
        # one; a $monitor here would silently lose the race on those designs.
        instrumented = instrument_rtl(self.RTL, "dut")
        self.assertNotIn("$monitor", instrumented)

    def test_every_traced_signal_appears_in_the_probe(self):
        instrumented = instrument_rtl(self.RTL, "dut")
        for signal in ("clk", "a", "q"):
            self.assertIn("%s=%%b" % signal, instrumented)

    def test_no_timescale_directive_is_emitted_unless_one_is_asked_for(self):
        # A testbench that declares none must keep the compiler default, and so must the
        # copy: a directive here would fire the injected bound 250000x too early.
        instrumented = instrument_rtl(self.RTL, "dut")
        self.assertNotIn("`timescale", instrumented)
        self.assertNotIn("`resetall", instrumented)

    def test_a_supplied_timescale_wraps_the_copy_and_is_undone_after_it(self):
        # It must precede the module to apply to it, and must not survive into the testbench,
        # which iverilog compiles next.
        instrumented = instrument_rtl(self.RTL, "dut", timescale="`timescale 1ns/1ps")
        self.assertTrue(instrumented.startswith("`timescale 1ns/1ps"))
        self.assertTrue(instrumented.rstrip().endswith("`resetall"))
        self.assertLess(instrumented.index("`timescale"), instrumented.index("module dut"))
        self.assertLess(instrumented.index("endmodule"), instrumented.index("`resetall"))

    def test_a_time_limit_injects_a_bounded_stop(self):
        instrumented = instrument_rtl(self.RTL, "dut", time_limit=1234)
        self.assertIn("#1234;", instrumented)
        self.assertIn(DIAG_MARKER, instrumented)
        self.assertIn("$finish", instrumented)

    def test_no_bounded_stop_without_a_time_limit(self):
        self.assertNotIn("$finish", instrument_rtl(self.RTL, "dut"))

    def test_a_candidate_with_no_traceable_signals_still_instruments(self):
        instrumented = instrument_rtl("module dut;\nendmodule\n", "dut")
        self.assertIn(TRACE_MARKER, instrumented)
        self.assertIn("$strobe", instrumented)
        self.assertNotIn("always @()", instrumented)  # an empty sensitivity list is illegal

    def test_a_missing_module_yields_no_copy(self):
        self.assertEqual(instrument_rtl("not verilog at all", "dut"), "")

    def test_the_instrumented_copy_is_exactly_what_the_gate_refuses(self):
        # The load-bearing invariant of the whole self track: this text may NEVER be scored,
        # and the gate is what would catch it if a future refactor tried.
        violations = find_illegal_system_tasks(instrument_rtl(self.RTL, "dut", time_limit=10))
        tokens = {token.lower() for _line, token in violations}
        self.assertIn("$strobe", tokens)
        self.assertIn("$finish", tokens)
        # ... while the candidate it was built from is admissible.
        self.assertEqual(find_illegal_system_tasks(self.RTL), ())


class TraceTextTests(unittest.TestCase):
    """Filtering, digesting and parsing, all pure functions over trace text."""

    def test_only_the_instrumentations_own_lines_survive(self):
        # This filter IS the boundary of the strict track: the testbench knows the expected
        # answers and prints them, so nothing it printed may reach the repair agent here.
        raw = (
            "Failed at a= 0 b= 3 sum=29 expected=3\n"
            "%s time=0 a=0000 q=xxxx\n"
            "===========Your Design Passed===========\n"
            "%s time=5 a=0001 q=0000\n"
            "%s bounded_stop t=100\n"
        ) % (TRACE_MARKER, TRACE_MARKER, DIAG_MARKER)
        filtered = filter_trace_lines(raw)
        self.assertNotIn("Failed at", filtered)
        self.assertNotIn("expected=3", filtered)
        self.assertNotIn("Passed", filtered)
        self.assertEqual(len(filtered.splitlines()), 3)

    def test_consecutive_duplicate_snapshots_collapse(self):
        line = "%s time=5 a=0001 q=0000" % TRACE_MARKER
        raw = "\n".join([line, line, line, "%s time=6 a=0010 q=0001" % TRACE_MARKER, line])
        self.assertEqual(len(filter_trace_lines(raw).splitlines()), 3)

    def test_digest_keeps_the_head_and_samples_the_tail(self):
        lines = ["%s time=%d v=%d" % (TRACE_MARKER, index, index) for index in range(500)]
        digest = trace_digest("\n".join(lines), head=5, tail=4, limit=10_000)
        self.assertIn("time=0 ", digest)
        self.assertIn("time=4 ", digest)
        self.assertIn("transitions omitted", digest)
        self.assertIn("time=499 ", digest)  # the last transition is always kept
        self.assertLess(len(digest.splitlines()), 20)

    def test_a_short_trace_is_returned_whole(self):
        lines = ["%s time=%d v=%d" % (TRACE_MARKER, index, index) for index in range(4)]
        self.assertEqual(trace_digest("\n".join(lines), head=5, tail=4), "\n".join(lines))

    def test_digest_respects_the_character_limit(self):
        lines = ["%s time=%d %s" % (TRACE_MARKER, index, "x" * 200) for index in range(500)]
        self.assertLessEqual(len(trace_digest("\n".join(lines), limit=1000)), 1000)

    def test_empty_trace_digests_to_nothing(self):
        self.assertEqual(trace_digest(""), "")

    def test_parse_trace_reads_time_and_values(self):
        raw = "%s time=15 a=0001 q=1010\n%s time=20 a=0010 q=0001" % (TRACE_MARKER, TRACE_MARKER)
        samples = parse_trace(raw)
        self.assertEqual([s.time for s in samples], [15, 20])
        self.assertEqual(samples[0].values, {"a": "0001", "q": "1010"})

    def test_parse_trace_ignores_anything_untagged(self):
        self.assertEqual(parse_trace("Failed at a=1 b=2\nsomething else"), ())

    def test_the_bounded_stop_time_is_an_independent_witness_that_time_moved(self):
        # One snapshot at t=0 plus a stop at t=20000 means "time advanced but nothing in the
        # design ever changed", which is a different fault from "time never advanced".
        trace = "%s time=0 clk=0\n%s bounded_stop time=20000" % (TRACE_MARKER, DIAG_MARKER)
        self.assertEqual(bounded_stop_time(trace), 20000)
        self.assertEqual(len(parse_trace(trace)), 1)

    def test_no_bounded_stop_line_means_no_witness(self):
        self.assertIsNone(bounded_stop_time("%s time=5 a=1" % TRACE_MARKER))
        self.assertIsNone(bounded_stop_time(""))


class TestbenchTimescaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = _make_benchmark(self.tmp / "bench")
        self.designs = {d.name: d for d in discover_designs(self.root)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _with_testbench(self, text):
        design = self.designs["tiny_adder"]
        design.testbench.write_text(text, encoding="utf-8")
        return design

    def test_a_declared_timescale_is_copied(self):
        design = self._with_testbench("`timescale 1ns / 1ps\nmodule testbench;\nendmodule\n")
        self.assertEqual(read_testbench_timescale(design), "`timescale 1ns/1ps")

    def test_a_testbench_without_one_yields_none(self):
        design = self._with_testbench("module testbench;\nendmodule\n")
        self.assertIsNone(read_testbench_timescale(design))

    def test_only_the_two_time_literals_can_escape(self):
        # The justification for reading the testbench at all: nothing else comes out.
        design = self._with_testbench(
            "`timescale 10ps/1ps\n"
            "module testbench;\n"
            "  // EXPECTED_ANSWER_MARKER sum should be 8'h06\n"
            "endmodule\n"
        )
        directive = read_testbench_timescale(design)
        self.assertEqual(directive, "`timescale 10ps/1ps")
        self.assertNotIn("EXPECTED_ANSWER_MARKER", directive)
        self.assertNotIn("8'h06", directive)

    def test_stuck_is_constant_over_the_second_half_not_the_whole_run(self):
        # The interesting hang: `done` leaves X at the first clock edge, settles to 0, and
        # never rises. Requiring zero changes overall would call that signal healthy.
        samples = [
            TraceSample(0, {"clk": "0", "done": "x"}),
            TraceSample(1, {"clk": "1", "done": "0"}),
            TraceSample(2, {"clk": "0", "done": "0"}),
            TraceSample(3, {"clk": "1", "done": "0"}),
            TraceSample(4, {"clk": "0", "done": "0"}),
            TraceSample(5, {"clk": "1", "done": "0"}),
            TraceSample(6, {"clk": "0", "done": "0"}),
            TraceSample(7, {"clk": "1", "done": "0"}),
        ]
        stuck, oscillating = _stuck_and_oscillating(samples)
        self.assertEqual(stuck, ("done",))
        self.assertEqual(oscillating, ("clk",))

    def test_a_single_sample_classifies_nothing(self):
        self.assertEqual(_stuck_and_oscillating([TraceSample(0, {"a": "1"})]), ((), ()))


class BoundedCaptureHeadTests(unittest.TestCase):
    def test_without_a_head_budget_only_the_tail_survives(self):
        sink = _BoundedCapture(tail=10, limit=10_000)
        sink.feed(b"A" * 50 + b"B" * 10)
        text = sink.text()
        self.assertTrue(text.endswith("B" * 10))
        self.assertNotIn("A" * 20, text)

    def test_a_head_budget_keeps_both_ends(self):
        sink = _BoundedCapture(tail=10, limit=10_000, head=8)
        sink.feed(b"HEADHEAD" + b"x" * 200 + b"TAILTAIL12")
        text = sink.text()
        self.assertTrue(text.startswith("HEADHEAD"))
        self.assertTrue(text.endswith("TAILTAIL12"))
        self.assertIn("truncated", text)

    def test_a_stream_that_fits_is_not_duplicated(self):
        sink = _BoundedCapture(tail=100, limit=10_000, head=8)
        sink.feed(b"short stream")
        self.assertEqual(sink.text(), "short stream")


# --------------------------------------------------------------------------- #
# self-instrumentation and the oracle diff, against the real simulator
# --------------------------------------------------------------------------- #

SEQ_TB = """`timescale 1ns/1ps
module testbench;
    reg clk = 0;
    reg rst = 1;
    wire done;
    always #5 clk = ~clk;
    seq_done dut(.clk(clk), .rst(rst), .done(done));
    initial begin
        #20 rst = 0;
        wait (done === 1'b1);
        $display("__PASS_BANNER__");
        $finish;
    end
endmodule
""".replace("__PASS_BANNER__", PASS_BANNER)

SEQ_RTL = """module seq_done(input clk, input rst, output reg done);
    reg [3:0] count;
    always @(posedge clk) begin
        if (rst) begin
            count <= 4'd0;
            done <= 1'b0;
        end else if (count == 4'd5) begin
            done <= 1'b1;
        end else begin
            count <= count + 4'd1;
        end
    end
endmodule
"""

#: The serial2parallel failure shape: it compiles, it runs, and `done` never rises, so the
#: watchdog kills it and the simulation log comes back EMPTY.
SEQ_HANG_RTL = SEQ_RTL.replace(
    "end else if (count == 4'd5) begin\n            done <= 1'b1;\n        end else begin\n            count <= count + 4'd1;\n        end",
    "end else begin\n            count <= count;\n            done <= 1'b0;\n        end",
)


@unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
class SelfTraceRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "bench"
        _write_design(
            self.root, "Misc/Seq", "seq_done", SEQ_TB, SEQ_RTL.replace("seq_done", "verified_seq_done")
        )
        self.design = {d.name: d for d in discover_designs(self.root)}["seq_done"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_trace_holds_the_candidates_own_signals_and_nothing_the_testbench_printed(self):
        result = run_self_trace(self.design, SEQ_RTL, self.tmp / "trace")
        self.assertTrue(result.ran, result.note)
        self.assertTrue(result.compiled)
        self.assertEqual(result.signals, ("clk", "rst", "done", "count"))
        self.assertIn("count=", result.trace)
        # The testbench's own stdout -- which is where the expected answers live -- is gone.
        self.assertNotIn(PASS_BANNER, result.trace)
        self.assertNotIn("Passed", result.trace)
        samples = parse_trace(result.trace)
        self.assertGreater(len(samples), 3)
        self.assertGreater(max(s.time for s in samples), 0)  # time really advances

    def test_a_trace_run_produces_no_verdict_at_all(self):
        # Structural, not incidental: a TraceResult has no pass/fail field, so no code path
        # can promote an instrumented run into a score.
        result = run_self_trace(self.design, SEQ_RTL, self.tmp / "trace2")
        for forbidden in ("func_pass", "func_pass_strict", "syntax_pass", "failure_family"):
            self.assertFalse(hasattr(result, forbidden), forbidden)

    def test_tracing_leaves_the_scored_verdict_untouched(self):
        before = evaluate_rtl(self.design, SEQ_RTL, self.tmp / "score_before")
        run_self_trace(self.design, SEQ_RTL, self.tmp / "trace3")
        after = evaluate_rtl(self.design, SEQ_RTL, self.tmp / "score_after")
        self.assertTrue(before.func_pass_strict, before.sim_log)
        self.assertEqual(before.func_pass, after.func_pass)
        self.assertEqual(before.failure_family, after.failure_family)

    def test_a_candidate_the_instrumenter_cannot_locate_is_reported_not_raised(self):
        result = run_self_trace(self.design, "module other(input a);\nendmodule\n", self.tmp / "t4")
        # `other` is the only module, so it is instrumented as the top -- but it will not
        # link against a testbench looking for `seq_done`.
        self.assertFalse(result.ran)
        self.assertFalse(result.compiled)
        self.assertIn("scored candidate is unaffected", result.note)

    def test_a_candidate_that_is_not_verilog_yields_no_trace(self):
        result = run_self_trace(self.design, "I refuse to write this design.", self.tmp / "t5")
        self.assertFalse(result.ran)
        self.assertIn("could not locate", result.note)


@unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
class TimeoutDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "bench"
        _write_design(
            self.root, "Misc/Seq", "seq_done", SEQ_TB, SEQ_RTL.replace("seq_done", "verified_seq_done")
        )
        self.design = {d.name: d for d in discover_designs(self.root)}["seq_done"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_hang_leaves_an_empty_sim_log_which_is_why_this_exists(self):
        result = evaluate_rtl(self.design, SEQ_HANG_RTL, self.tmp / "scored", sim_timeout=3)
        self.assertTrue(result.syntax_pass, result.compile_log)
        self.assertEqual(result.failure_family, "timeout")
        self.assertEqual(result.sim_log.strip(), "")  # nothing for a repair agent to read

    def test_the_diagnosis_names_the_signal_that_never_rises(self):
        diagnosis = diagnose_timeout(self.design, SEQ_HANG_RTL, self.tmp / "diag")
        self.assertTrue(diagnosis.ran, diagnosis.note)
        self.assertGreater(diagnosis.transitions, 10)
        self.assertIn("done", diagnosis.stuck)
        self.assertIn("clk", diagnosis.oscillating)
        self.assertEqual(diagnosis.final_values.get("done"), "0")

    def test_simulation_time_is_reported_in_units_that_actually_advance(self):
        # Regression 1: with the candidate on iverilog's 1s default and this testbench on
        # 1ns/1ps, every snapshot read time=0 and the diagnosis said "time never advanced --
        # zero-delay loop" about a design whose real fault was a counter that never reached
        # its terminal value.
        diagnosis = diagnose_timeout(self.design, SEQ_HANG_RTL, self.tmp / "diag2")
        self.assertTrue(diagnosis.time_advanced)
        self.assertTrue(diagnosis.signals_moved)
        self.assertIsNotNone(diagnosis.last_time)
        self.assertGreater(diagnosis.last_time, 0)

    def test_matching_the_testbenchs_timescale_is_what_makes_time_readable(self):
        # `$time` is rounded to the *enclosing module's* time unit before it is formatted.
        # A copy compiled on iverilog's 1s default against a 1ns/1ps testbench therefore
        # rounds every instant to zero -- which is precisely the trace that made the
        # diagnosis announce a zero-delay loop. Matching the testbench is the whole fix.
        matched = run_self_trace(self.design, SEQ_RTL, self.tmp / "units_matched")
        unmatched = run_self_trace(self.design, SEQ_RTL, self.tmp / "units_unmatched", timescale=None)
        matched_times = {sample.time for sample in parse_trace(matched.trace)}
        unmatched_times = {sample.time for sample in parse_trace(unmatched.trace)}
        self.assertEqual(unmatched_times, {0})  # the bug
        self.assertGreater(len(matched_times), 5)  # the fix
        self.assertEqual(sorted(matched_times)[0], 0)

    def test_a_testbench_with_no_timescale_still_gets_a_usable_bound(self):
        # Regression 2: forcing 1ps on the copy while this testbench runs on the 1s default
        # made the injected `#n $finish` fire 250000x before the first clock edge, so the
        # diagnosis saw a single t=0 snapshot and called a stuck design a zero-delay loop.
        # 10 of the 50 RTLLM testbenches declare no timescale, including serial2parallel.
        self.design.testbench.write_text(
            SEQ_TB.replace("`timescale 1ns/1ps\n", ""), encoding="utf-8"
        )
        self.assertIsNone(read_testbench_timescale(self.design))
        diagnosis = diagnose_timeout(self.design, SEQ_HANG_RTL, self.tmp / "nots")
        self.assertTrue(diagnosis.ran, diagnosis.note)
        self.assertGreater(diagnosis.transitions, 10)
        self.assertTrue(diagnosis.time_advanced)
        self.assertTrue(diagnosis.signals_moved)
        self.assertIn("done", diagnosis.stuck)

    def test_the_bounded_stop_witnesses_time_even_when_nothing_in_the_design_moves(self):
        diagnosis = diagnose_timeout(self.design, SEQ_HANG_RTL, self.tmp / "witness")
        self.assertIsNotNone(diagnosis.last_time)
        # The stop line is parsed even though it is not a signal snapshot.
        self.assertGreaterEqual(diagnosis.last_time, max(s.time for s in parse_trace(diagnosis.digest)))

    def test_the_report_is_self_derived_and_actionable(self):
        report = diagnose_timeout(self.design, SEQ_HANG_RTL, self.tmp / "diag3").report()
        self.assertIn("last simulation time reached", report)
        self.assertIn("did simulation time advance at all: yes", report)
        self.assertIn("NEVER changed again", report)
        self.assertIn("final observed values", report)
        # Nothing from the oracle: not the reference, not the testbench's stdout.
        self.assertNotIn(PASS_BANNER, report)
        self.assertNotIn("verified_", report)

    def test_the_diagnosis_is_bounded_and_cheap(self):
        started = time.time()
        diagnose_timeout(self.design, SEQ_HANG_RTL, self.tmp / "diag4")
        self.assertLess(time.time() - started, 30)

    def test_a_healthy_design_diagnoses_as_live(self):
        diagnosis = diagnose_timeout(self.design, SEQ_RTL, self.tmp / "diag5")
        self.assertTrue(diagnosis.ran)
        self.assertTrue(diagnosis.time_advanced)
        self.assertNotIn("done", diagnosis.stuck)


@unittest.skipUnless(HAS_IVERILOG, "iverilog/vvp not installed")
class TimeoutReadingTests(unittest.TestCase):
    """The three verdicts a diagnosis can reach. They need opposite repairs, so they must
    not collapse into one another."""

    def _diagnosis(self, **overrides):
        base = dict(
            design="dut",
            ran=True,
            last_time=20000,
            time_advanced=True,
            transitions=800,
            stuck=("done",),
            oscillating=("clk",),
            digest="",
            timed_out=False,
            note="",
            signals_moved=True,
            final_values={"done": "0"},
        )
        base.update(overrides)
        return TimeoutDiagnosis(**base)

    def test_time_never_advanced_reads_as_a_zero_delay_loop(self):
        report = self._diagnosis(time_advanced=False, last_time=0, signals_moved=False).report()
        self.assertIn("zero-delay loop", report)
        self.assertNotIn("NOT ONE of your module's signals", report)

    def test_time_advanced_but_nothing_moved_reads_as_an_undriven_design(self):
        report = self._diagnosis(signals_moved=False, stuck=(), oscillating=()).report()
        self.assertIn("NOT ONE of your module's signals ever changed", report)
        self.assertNotIn("zero-delay loop", report)

    def test_time_advanced_and_a_signal_settled_reads_as_an_unreachable_condition(self):
        report = self._diagnosis(oscillating=()).report()
        self.assertIn("terminating condition", report)
        self.assertNotIn("zero-delay loop", report)

    def test_a_run_that_produced_nothing_says_so_instead_of_guessing(self):
        report = self._diagnosis(ran=False, note="the instrumented copy did not compile").report()
        self.assertIn("did not compile", report)
        self.assertNotIn("zero-delay loop", report)
        self.assertNotIn("terminating condition", report)


class OracleBehaviourDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = _make_benchmark(self.tmp / "bench")
        self.designs = {d.name: d for d in discover_designs(self.root)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _candidate(self, rtl, name="w"):
        return evaluate_rtl(self.designs["tiny_adder"], rtl, self.tmp / name)

    def test_the_first_divergence_is_reported_with_expected_and_got(self):
        wrong = "module tiny_adder(input [3:0] a, input [3:0] b, output [4:0] sum);\n    assign sum = a - b;\nendmodule\n"
        sim = self._candidate(wrong)
        diff = oracle_behaviour_diff(self.designs["tiny_adder"], sim.sim_log, self.tmp / "o1")
        self.assertTrue(diff.ran)
        self.assertTrue(diff.diverged)
        self.assertEqual(diff.line, 1)
        self.assertEqual(diff.expected, PASS_BANNER)
        self.assertIn("Failed at", diff.got)
        report = diff.report()
        self.assertIn("ORACLE-DERIVED EVIDENCE", report)
        self.assertIn("first divergence at output line 1", report)

    def test_the_reference_rtl_source_never_appears_in_the_report(self):
        # The whole justification for calling this "behaviour only": the answer key is
        # simulated, never quoted.
        wrong = "module tiny_adder(input [3:0] a, input [3:0] b, output [4:0] sum);\n    assign sum = 5'd0;\nendmodule\n"
        sim = self._candidate(wrong, "w2")
        diff = oracle_behaviour_diff(self.designs["tiny_adder"], sim.sim_log, self.tmp / "o2")
        report = diff.report()
        for leak in ("verified_tiny_adder", "assign sum = a + b", "endmodule", "module "):
            self.assertNotIn(leak, report)
        self.assertNotIn(reference_rtl_text(self.designs["tiny_adder"]).strip(), report)

    def test_matching_behaviour_reports_no_divergence(self):
        sim = self._candidate(TINY_ADDER_RTL, "w3")
        diff = oracle_behaviour_diff(self.designs["tiny_adder"], sim.sim_log, self.tmp / "o3")
        self.assertTrue(diff.ran)
        self.assertFalse(diff.diverged)
        self.assertIsNone(diff.line)
        self.assertIn("identical", diff.report())

    def test_a_design_without_reference_rtl_is_reported_not_raised(self):
        design = self.designs["tiny_adder"]
        stripped = RtllmDesign(
            name=design.name,
            category=design.category,
            directory=design.directory,
            description=design.description,
            testbench=design.testbench,
            reference_files=(),
        )
        diff = oracle_behaviour_diff(stripped, "whatever", self.tmp / "o4")
        self.assertFalse(diff.ran)
        self.assertIn("no reference RTL", diff.report())

    def test_a_candidate_that_printed_nothing_still_diffs(self):
        diff = oracle_behaviour_diff(self.designs["tiny_adder"], "", self.tmp / "o5")
        self.assertTrue(diff.ran)
        self.assertTrue(diff.diverged)
        self.assertEqual(diff.candidate_lines, 0)
        self.assertIn("your run stopped here", diff.report())


if __name__ == "__main__":
    unittest.main()
