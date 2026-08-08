"""Offline tests for the natural-language -> RTL multi-agent loop.

Everything here is hermetic: a scripted :class:`FakeLLM` stands in for the model and a
scripted verifier stands in for ``rtllm_bench.evaluate_rtl``, so no ``claude`` CLI, no
``iverilog``, and no benchmark checkout is required.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from c2hlsc_agent import rtllm_agent
from c2hlsc_agent.rtllm_agent import (
    EVIDENCE_LIMIT,
    EVIDENCE_POLICIES,
    FAMILY_REPAIR_INSTRUCTIONS,
    UNREPAIRABLE_FAMILIES,
    ORACLE_DERIVED_POLICIES,
    RtllmAgentConfig,
    build_evidence,
    extract_verilog,
    failure_analyst,
    interface_restatement,
    repair_instructions,
    run_design,
)
from c2hlsc_agent.rtllm_bench import (
    FAILURE_FAMILIES,
    BehaviourDiff,
    RtllmDesign,
    SimResult,
    TimeoutDiagnosis,
    TraceResult,
)


DESCRIPTION = """Please act as a professional verilog designer.

Implement a module of an 8-bit adder with multiple bit-level adders in combinational logic.

Module name:
    adder_8bit
Input ports:
    a[7:0]: 8-bit input operand A.
    b[7:0]: 8-bit input operand B.
    cin: Carry-in input.
Output ports:
    sum[7:0]: 8-bit output representing the sum of A and B.
    cout: Carry-out output.

Give me the complete code.
"""

GOLDEN_TB_MARKER = "GOLDEN_TESTBENCH_MARKER_DO_NOT_LEAK"
GOLDEN_REF_MARKER = "GOLDEN_REFERENCE_MARKER_DO_NOT_LEAK"

DESIGN_RTL = """module adder_8bit(
  input  [7:0] a,
  input  [7:0] b,
  input        cin,
  output [7:0] sum,
  output       cout
);
  assign {cout, sum} = a + b + cin;
endmodule
"""


def _make_design(tmp: Path, name: str = "adder_8bit") -> RtllmDesign:
    directory = tmp / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "design_description.txt").write_text(DESCRIPTION, encoding="utf-8")
    testbench = directory / "testbench.v"
    testbench.write_text(
        f"`timescale 1ns / 1ps\nmodule testbench;\n  // {GOLDEN_TB_MARKER}\n  initial $display(\"Pass\");\nendmodule\n",
        encoding="utf-8",
    )
    reference = directory / "verified_adder_8bit.v"
    reference.write_text(f"// {GOLDEN_REF_MARKER}\n{DESIGN_RTL}", encoding="utf-8")
    return RtllmDesign(
        name=name,
        category="Arithmetic/Adder",
        directory=directory,
        description=DESCRIPTION,
        testbench=testbench,
        reference_files=(reference,),
    )


def _sim(
    design: str = "adder_8bit",
    *,
    syntax: bool = True,
    func: bool = False,
    family: str | None = "functional_mismatch",
    compile_log: str = "",
    sim_log: str = "",
    timed_out: bool = False,
) -> SimResult:
    return SimResult(
        design=design,
        syntax_pass=syntax,
        func_pass=func,
        func_pass_strict=func,
        timed_out=timed_out,
        compile_log=compile_log,
        sim_log=sim_log,
        duration_s=0.25,
        failure_family=None if func else family,
    )


class FakeLLM:
    """Deterministic scripted stand-in for an :class:`~c2hlsc_agent.llm.LLMClient`.

    Each scripted item is either a response string or an exception instance to raise, so a
    flaky/dead backend can be simulated per call. ``repeat`` keeps returning the last item
    once the script runs out.
    """

    def __init__(self, responses, *, repeat: bool = False, model: str = "fake-model") -> None:
        self.responses = list(responses)
        self.repeat = repeat
        self.model = model
        self.calls: list[tuple[str, str]] = []

    @property
    def prompts(self) -> str:
        """Every system+user prompt handed to the client, concatenated."""

        return "\n".join(system + "\n" + user for system, user in self.calls)

    def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> str:
        self.calls.append((system, user))
        if not self.responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        item = self.responses[0] if (self.repeat and len(self.responses) == 1) else self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeVerifier:
    """Scripted stand-in for ``rtllm_bench.evaluate_rtl`` that records what it was asked to verify."""

    def __init__(self, results, *, repeat: bool = False) -> None:
        # `repeat` is accepted for symmetry with FakeLLM; the last scripted result is
        # returned indefinitely either way, so it documents intent at the call site.
        self.results = list(results)
        self.repeat = repeat
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, design, rtl_text, workdir, **kwargs):
        self.calls.append((rtl_text, Path(workdir)))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return result


def _fenced(body: str, lang: str = "verilog") -> str:
    return f"```{lang}\n{body}\n```"


# Markers planted in the fake evidence channels. Each one is unique to ONE channel, so a
# prompt assertion says exactly which channel fed it.
SELF_TRACE_MARKER = "SELF_TRACE_MARKER_9F2A"
TIMEOUT_DIAG_MARKER = "TIMEOUT_DIAG_MARKER_7C41"
ORACLE_DIFF_MARKER = "ORACLE_DIFF_MARKER_1B8E"


def _fake_trace(*, ran: bool = True) -> TraceResult:
    return TraceResult(
        design="adder_8bit",
        ran=ran,
        signals=("a", "b", "cin", "sum", "cout"),
        trace=f"RTLLM_TRACE time=0 a=00000001 {SELF_TRACE_MARKER}=1",
        timed_out=False,
        compiled=True,
        note="",
    )


def _fake_diagnosis(*, ran: bool = True) -> TimeoutDiagnosis:
    return TimeoutDiagnosis(
        design="adder_8bit",
        ran=ran,
        last_time=4200,
        time_advanced=True,
        transitions=97,
        stuck=(TIMEOUT_DIAG_MARKER,),
        oscillating=("clk",),
        digest="RTLLM_TRACE time=4200 done=0",
        timed_out=False,
        note="",
        final_values={"done": "0"},
    )


def _fake_diff() -> BehaviourDiff:
    return BehaviourDiff(
        design="adder_8bit",
        ran=True,
        diverged=True,
        line=3,
        expected=f"{ORACLE_DIFF_MARKER} sum=8'h06",
        got="sum=8'h05",
        reference_lines=4,
        candidate_lines=9,
        note="",
    )


class EvidenceSpies:
    """Stand-ins for the three failure_analyst sub-runs, recording what they were handed.

    Patched in as module attributes of ``rtllm_agent`` so the loop's real call sites are
    exercised; only the simulator work is faked.
    """

    def __init__(self) -> None:
        self.trace_calls: list[str] = []
        self.diagnosis_calls: list[str] = []
        self.diff_calls: list[str] = []

    def run_self_trace(self, design, rtl_text, workdir, **kwargs):
        self.trace_calls.append(rtl_text)
        return _fake_trace()

    def diagnose_timeout(self, design, rtl_text, workdir, **kwargs):
        self.diagnosis_calls.append(rtl_text)
        return _fake_diagnosis()

    def oracle_behaviour_diff(self, design, candidate_sim_log, workdir, **kwargs):
        self.diff_calls.append(candidate_sim_log)
        return _fake_diff()

    def patch(self):
        return mock.patch.multiple(
            rtllm_agent,
            run_self_trace=self.run_self_trace,
            diagnose_timeout=self.diagnose_timeout,
            oracle_behaviour_diff=self.oracle_behaviour_diff,
        )


# --------------------------------------------------------------------------- #
# extract_verilog
# --------------------------------------------------------------------------- #


class ExtractVerilogTests(unittest.TestCase):
    def test_fenced_block(self):
        text = f"Here is the design.\n\n{_fenced(DESIGN_RTL)}\n\nWant a testbench?"
        code = extract_verilog(text, "adder_8bit")
        self.assertIn("module adder_8bit", code)
        self.assertIn("assign {cout, sum}", code)
        self.assertNotIn("Want a testbench", code)
        self.assertTrue(code.endswith("endmodule\n"))

    def test_unfenced_module(self):
        code = extract_verilog(DESIGN_RTL, "adder_8bit")
        self.assertIn("module adder_8bit", code)
        self.assertIn("endmodule", code)

    def test_prose_around_unfenced_code(self):
        text = (
            "Sure! I will build a ripple-carry adder.\n"
            "The carry chain propagates from bit 0 upward.\n\n"
            "`timescale 1ns / 1ps\n"
            f"{DESIGN_RTL}\n"
            "Note that this is purely combinational, so no clock is needed.\n"
        )
        code = extract_verilog(text, "adder_8bit")
        self.assertIn("`timescale 1ns / 1ps", code)  # directives survive the preamble filter
        self.assertIn("module adder_8bit", code)
        self.assertNotIn("Sure!", code)
        self.assertNotIn("purely combinational", code)

    def test_volunteered_testbench_is_stripped(self):
        tb = (
            "module testbench;\n"
            "  reg [7:0] a;\n"
            "  initial begin $display(\"Pass\"); $finish; end\n"
            "endmodule\n"
        )
        alt_tb = "module adder_8bit_tb;\n  initial $finish;\nendmodule\n"
        text = _fenced(DESIGN_RTL + "\n" + tb + "\n" + alt_tb)
        code = extract_verilog(text, "adder_8bit")
        self.assertIn("module adder_8bit", code)
        self.assertNotIn("module testbench", code)
        self.assertNotIn("adder_8bit_tb", code)
        self.assertNotIn("$finish", code)
        self.assertEqual(code.count("endmodule"), 1)

    def test_helper_submodule_is_kept(self):
        text = _fenced(
            "module adder_8bit(input [7:0] a, input [7:0] b, input cin, output [7:0] sum, output cout);\n"
            "  wire [8:0] c;\n"
            "  assign c[0] = cin;\n"
            "  full_adder fa0(a[0], b[0], c[0], sum[0], c[1]);\n"
            "  assign cout = c[8];\n"
            "endmodule\n"
            "\n"
            "module full_adder(input x, input y, input cin, output s, output cout);\n"
            "  assign {cout, s} = x + y + cin;\n"
            "endmodule\n"
        )
        code = extract_verilog(text, "adder_8bit")
        self.assertIn("module adder_8bit", code)
        self.assertIn("module full_adder", code)
        self.assertEqual(code.count("endmodule"), 2)

    def test_refusal_and_empty_yield_empty_string(self):
        self.assertEqual(extract_verilog("", "adder_8bit"), "")
        self.assertEqual(extract_verilog("I'm sorry, I can't help with that request.", "adder_8bit"), "")
        # A response with only a testbench leaves nothing to verify.
        self.assertEqual(
            extract_verilog(_fenced("module testbench;\ninitial $finish;\nendmodule"), "adder_8bit"),
            "",
        )

    def test_wrong_module_name_is_not_silently_renamed(self):
        wrong = DESIGN_RTL.replace("adder_8bit", "adder8")
        code = extract_verilog(_fenced(wrong), "adder_8bit")
        self.assertIn("module adder8", code)
        self.assertNotIn("adder_8bit", code)  # the verifier must report missing_module

    def test_mislabeled_fence_still_parsed(self):
        code = extract_verilog(_fenced(DESIGN_RTL, lang="text"), "adder_8bit")
        self.assertIn("module adder_8bit", code)

    def test_duplicate_definition_keeps_the_last(self):
        draft = DESIGN_RTL.replace("a + b + cin", "a + b /* DRAFT */")
        final = DESIGN_RTL.replace("a + b + cin", "a + b + cin /* FINAL */")
        code = extract_verilog(f"Draft:\n{_fenced(draft)}\nFixed:\n{_fenced(final)}", "adder_8bit")
        self.assertIn("FINAL", code)
        self.assertNotIn("DRAFT", code)
        self.assertEqual(code.count("endmodule"), 1)

    def test_endmodule_not_at_line_start_falls_back_to_slice(self):
        text = _fenced("module adder_8bit(input a, output b); assign b = a; endmodule")
        code = extract_verilog(text, "adder_8bit")
        self.assertIn("module adder_8bit", code)
        self.assertIn("endmodule", code)


# --------------------------------------------------------------------------- #
# build_evidence (failure_analyst)
# --------------------------------------------------------------------------- #


class BuildEvidenceTests(unittest.TestCase):
    def test_uses_compile_log_when_it_did_not_compile(self):
        sim = _sim(
            syntax=False,
            family="compile_error",
            compile_log="adder_8bit.v:3: syntax error near 'assgn'",
            sim_log="SIM LOG SHOULD NOT APPEAR",
        )
        evidence = build_evidence(sim, RtllmAgentConfig())
        self.assertIn("compile (iverilog)", evidence)
        self.assertIn("syntax error", evidence)
        self.assertIn("compile_error", evidence)
        self.assertIn(rtllm_agent.REPAIR_INTENTS["compile_error"], evidence)
        self.assertNotIn("SIM LOG SHOULD NOT APPEAR", evidence)

    def test_uses_sim_log_once_it_compiles(self):
        sim = _sim(compile_log="COMPILE NOISE", sim_log="Failed: sum=8'h05 expected 8'h06")
        evidence = build_evidence(sim, RtllmAgentConfig())
        self.assertIn("simulate (vvp)", evidence)
        self.assertIn("expected 8'h06", evidence)
        self.assertNotIn("COMPILE NOISE", evidence)

    def test_tail_slices_a_long_log(self):
        log = ("head marker\n" + "x" * (EVIDENCE_LIMIT * 2)) + "\ntail marker"
        evidence = build_evidence(_sim(sim_log=log), RtllmAgentConfig())
        self.assertIn("tail marker", evidence)
        self.assertNotIn("head marker", evidence)
        self.assertLess(len(evidence), EVIDENCE_LIMIT + 1000)

    def test_timeout_is_flagged(self):
        evidence = build_evidence(
            _sim(family="timeout", timed_out=True, sim_log="still running"), RtllmAgentConfig()
        )
        self.assertIn("WATCHDOG", evidence)
        self.assertIn("timeout", evidence)

    def test_evidence_policy_none_withholds_everything(self):
        sim = _sim(family="functional_mismatch", sim_log="Failed: sum=8'h05 expected 8'h06")
        evidence = build_evidence(sim, RtllmAgentConfig(evidence_policy="none"))
        self.assertNotIn("expected 8'h06", evidence)
        self.assertNotIn("functional_mismatch", evidence)
        self.assertIn("blind retry", evidence)


class ParserPerformanceTests(unittest.TestCase):
    """A model response is untrusted input, so parsing it must be bounded."""

    def test_repeated_headers_with_no_endmodule_parse_in_linear_time(self):
        # The old `module ... .*? endmodule` pattern rescanned to end-of-input from every
        # unterminated header: 2000 repeats took 8.9 s and 20 000 did not finish in 110 s,
        # stalling a worker thread with no timeout and no cancellation. Assert the SHAPE
        # (linear, not quadratic) rather than a wall-clock threshold, so the test is not
        # flaky on a loaded machine.
        line = "module adder_8bit(input a, output b);\n"

        def elapsed(count: int) -> float:
            started = time.perf_counter()
            extract_verilog(line * count, "adder_8bit")
            return time.perf_counter() - started

        elapsed(500)  # warm up the regex cache
        small, large = elapsed(2000), elapsed(8000)
        # 4x the input: linear predicts ~4x, quadratic ~16x. Allow a wide margin.
        self.assertLess(large, max(small, 1e-4) * 40 + 0.5)
        self.assertLess(large, 5.0)

    def test_an_oversized_response_is_capped_before_parsing(self):
        huge = "x" * (rtllm_agent.MAX_RESPONSE_CHARS * 2)
        started = time.perf_counter()
        self.assertEqual(extract_verilog(huge, "adder_8bit"), "")
        self.assertLess(time.perf_counter() - started, 5.0)

    def test_capping_does_not_disturb_a_normal_response(self):
        self.assertEqual(extract_verilog(_fenced(DESIGN_RTL), "adder_8bit").strip(), DESIGN_RTL.strip())

    def test_module_pairing_matches_the_previous_semantics(self):
        text = (
            "module adder_8bit(input a);\nendmodule\n"
            "module helper(input b);\nendmodule\n"
            "module adder_8bit_tb;\nendmodule\n"
        )
        kept = extract_verilog(text, "adder_8bit")
        self.assertIn("module adder_8bit(", kept)
        self.assertIn("module helper", kept)
        self.assertNotIn("_tb", kept)  # a volunteered testbench is dropped

    def test_a_truncated_final_block_still_yields_the_complete_modules(self):
        text = "module adder_8bit(input a);\nendmodule\nmodule cut_off(input b);\n"
        kept = extract_verilog(text, "adder_8bit")
        self.assertIn("module adder_8bit", kept)
        self.assertNotIn("cut_off", kept)


# --------------------------------------------------------------------------- #
# run_design (the loop)
# --------------------------------------------------------------------------- #


class RunDesignTests(unittest.TestCase):
    def _run(self, client, verifier, config, *, design_name: str = "adder_8bit"):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            design = _make_design(tmp / "bench", design_name)
            with mock.patch.object(rtllm_agent, "evaluate_rtl", verifier):
                return run_design(design, client, config, tmp / "work"), design

    def test_repair_loop_stops_on_first_pass(self):
        client = FakeLLM([_fenced(DESIGN_RTL), _fenced(DESIGN_RTL.replace("cin;", "cin; // fixed"))])
        verifier = FakeVerifier([_sim(family="functional_mismatch", sim_log="Failed"), _sim(func=True)])
        config = RtllmAgentConfig(plan=False, max_repair_rounds=3)
        result, _design = self._run(client, verifier, config)

        sample = result.samples[0]
        self.assertEqual([r.role for r in sample.rounds], ["rtl_generator", "rtl_repair_agent"])
        self.assertTrue(sample.func_pass)
        self.assertTrue(sample.syntax_pass)
        self.assertEqual(sample.to_dict()["func_pass_round"], 1)
        self.assertEqual(len(client.calls), 2)  # no third round once it passes
        self.assertEqual(len(verifier.calls), 2)
        self.assertIn("// fixed", sample.final_rtl)
        self.assertEqual(result.func_success, 1)
        self.assertEqual(result.syntax_success, 1)

    def test_loop_exhausts_max_repair_rounds(self):
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        verifier = FakeVerifier([_sim(family="functional_mismatch", sim_log="Failed")])
        config = RtllmAgentConfig(plan=False, max_repair_rounds=2)
        result, _design = self._run(client, verifier, config)

        sample = result.samples[0]
        self.assertEqual([r.round for r in sample.rounds], [0, 1, 2])
        self.assertEqual(len(verifier.calls), 3)
        self.assertFalse(sample.func_pass)
        self.assertTrue(sample.syntax_pass)  # it compiled every round, it just computed wrong
        self.assertEqual(result.func_success, 0)
        self.assertEqual(result.syntax_success, 1)
        self.assertIsNone(sample.to_dict()["func_pass_round"])

    def test_each_round_gets_its_own_workdir(self):
        # A stale simv/sim binary from a previous round must never be able to fake a
        # syntax pass, so every attempt verifies in a fresh directory that already exists.
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        seen: list[bool] = []

        class _DirCheckingVerifier(FakeVerifier):
            def __call__(self, design, rtl_text, workdir, **kwargs):
                seen.append(Path(workdir).is_dir())
                return super().__call__(design, rtl_text, workdir, **kwargs)

        verifier = _DirCheckingVerifier([_sim(syntax=False, family="compile_error", compile_log="err")])
        config = RtllmAgentConfig(plan=False, max_repair_rounds=1)
        self._run(client, verifier, config)
        dirs = [path for _rtl, path in verifier.calls]
        self.assertEqual(len(set(dirs)), 2)
        self.assertEqual([d.name for d in dirs], ["round0", "round1"])
        self.assertEqual(seen, [True, True])

    def test_llm_error_when_the_client_always_fails(self):
        client = FakeLLM([RuntimeError("claude CLI failed (rc=1)")], repeat=True)
        verifier = FakeVerifier([_sim(func=True)])
        config = RtllmAgentConfig(plan=False, max_repair_rounds=2, llm_retries=2)
        with mock.patch.object(rtllm_agent.time, "sleep") as sleep:
            result, _design = self._run(client, verifier, config)

        sample = result.samples[0]
        self.assertEqual(len(sample.rounds), 1)
        self.assertEqual(sample.rounds[0].role, "rtl_generator")
        self.assertIn("claude CLI failed", sample.rounds[0].llm_error or "")
        self.assertFalse(sample.syntax_pass)
        self.assertFalse(sample.func_pass)
        self.assertEqual(sample.final_rtl, "")
        self.assertEqual(len(client.calls), 3)  # 1 + llm_retries
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 4.0])
        self.assertEqual(verifier.calls, [])  # nothing was ever verified
        self.assertEqual(result.func_success, 0)

    def test_repair_llm_error_keeps_the_generated_candidate(self):
        client = FakeLLM([_fenced(DESIGN_RTL), OSError("socket hung up")], repeat=True)
        verifier = FakeVerifier([_sim(family="functional_mismatch", sim_log="Failed")])
        config = RtllmAgentConfig(plan=False, max_repair_rounds=2, llm_retries=0)
        result, _design = self._run(client, verifier, config)

        sample = result.samples[0]
        self.assertEqual([r.role for r in sample.rounds], ["rtl_generator", "rtl_repair_agent"])
        self.assertIn("socket hung up", sample.rounds[1].llm_error or "")
        self.assertIn("module adder_8bit", sample.final_rtl)
        self.assertTrue(sample.syntax_pass)
        self.assertFalse(sample.func_pass)

    def test_planner_failure_is_not_fatal(self):
        client = FakeLLM([RuntimeError("planner down"), _fenced(DESIGN_RTL)])
        verifier = FakeVerifier([_sim(func=True)])
        config = RtllmAgentConfig(plan=True, max_repair_rounds=0, llm_retries=0)
        result, _design = self._run(client, verifier, config)

        sample = result.samples[0]
        self.assertIsNone(sample.contract)
        self.assertIn("planner down", sample.plan_error or "")
        self.assertTrue(sample.func_pass)

    def test_contract_from_planner_reaches_the_generator(self):
        contract = "Module: adder_8bit\nPorts: a | input | [7:0] | operand A"
        client = FakeLLM([contract, _fenced(DESIGN_RTL)])
        verifier = FakeVerifier([_sim(func=True)])
        config = RtllmAgentConfig(plan=True, max_repair_rounds=0)
        result, _design = self._run(client, verifier, config)

        self.assertEqual(result.samples[0].contract, contract)
        generator_prompt = client.calls[1][1]
        self.assertIn("Ports: a | input | [7:0] | operand A", generator_prompt)
        self.assertIn("rtl_planner", generator_prompt)

    def test_evidence_policy_none_withholds_tool_output_from_the_repair_prompt(self):
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        verifier = FakeVerifier(
            [_sim(family="functional_mismatch", sim_log="Failed: sum=8'h05 expected 8'h06")]
        )
        config = RtllmAgentConfig(plan=False, max_repair_rounds=1, evidence_policy="none")
        result, _design = self._run(client, verifier, config)

        repair_prompt = client.calls[1][1]
        self.assertNotIn("expected 8'h06", repair_prompt)
        self.assertNotIn("functional_mismatch", repair_prompt)
        self.assertIn("blind retry", repair_prompt)
        # The candidate itself is still shown -- only the tool output is withheld.
        self.assertIn("module adder_8bit", repair_prompt)
        # And the setting is recorded, so a report cannot claim the logs were used.
        self.assertEqual(result.samples[0].to_dict()["evidence_policy"], "none")

    def test_evidence_policy_logs_passes_tool_output(self):
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        verifier = FakeVerifier(
            [_sim(family="functional_mismatch", sim_log="Failed: sum=8'h05 expected 8'h06")]
        )
        config = RtllmAgentConfig(plan=False, max_repair_rounds=1)
        result, _design = self._run(client, verifier, config)

        repair_prompt = client.calls[1][1]
        self.assertIn("expected 8'h06", repair_prompt)
        self.assertIn("functional_mismatch", repair_prompt)
        self.assertEqual(result.samples[0].to_dict()["evidence_policy"], "logs")

    def test_golden_rtl_and_testbench_never_reach_the_model(self):
        client = FakeLLM(
            ["Module: adder_8bit", _fenced(DESIGN_RTL), _fenced(DESIGN_RTL), _fenced(DESIGN_RTL)]
        )
        verifier = FakeVerifier(
            [_sim(syntax=False, family="compile_error", compile_log="adder_8bit.v:3: syntax error")]
        )
        config = RtllmAgentConfig(plan=True, max_repair_rounds=2)
        _result, design = self._run(client, verifier, config)

        prompts = client.prompts
        self.assertEqual(len(client.calls), 4)  # plan + generate + 2 repairs
        self.assertNotIn(GOLDEN_TB_MARKER, prompts)
        self.assertNotIn(GOLDEN_REF_MARKER, prompts)
        self.assertNotIn("verified_adder_8bit", prompts)
        self.assertNotIn(str(design.testbench), prompts)
        self.assertNotIn("testbench.v", prompts)
        # ... while the natural-language description is present in every prompt.
        for _system, user in client.calls:
            self.assertIn("8-bit input operand A", user)

    def test_a_stop_signal_ends_the_repair_loop_between_rounds(self):
        # Without this hook an interrupt cannot shorten an in-flight design: at the default
        # timeouts one design is up to 900s x 3 attempts x 4 roles of model calls.
        stop = threading.Event()
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        verifier = FakeVerifier([_sim(family="functional_mismatch", sim_log="Failed")], repeat=True)
        original = verifier.__call__

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            design = _make_design(tmp / "bench", "adder_8bit")

            def verify_then_stop(*args, **kwargs):
                stop.set()  # as if SIGINT arrived while round 0 was being verified
                return original(*args, **kwargs)

            with mock.patch.object(rtllm_agent, "evaluate_rtl", verify_then_stop):
                result = run_design(
                    design, client, RtllmAgentConfig(plan=False, max_repair_rounds=5),
                    tmp / "work", stop=stop,
                )

        sample = result.samples[0]
        self.assertEqual([r.role for r in sample.rounds], ["rtl_generator"])  # no repair ran
        self.assertEqual(len(client.calls), 1)

    def test_a_stop_signal_ends_the_sample_loop_between_samples(self):
        stop = threading.Event()
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        verifier = FakeVerifier([_sim(func=True)], repeat=True)
        original = verifier.__call__

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            design = _make_design(tmp / "bench", "adder_8bit")

            def verify_then_stop(*args, **kwargs):
                stop.set()
                return original(*args, **kwargs)

            with mock.patch.object(rtllm_agent, "evaluate_rtl", verify_then_stop):
                result = run_design(
                    design, client, RtllmAgentConfig(plan=False, samples=5, max_repair_rounds=0),
                    tmp / "work", stop=stop,
                )

        # The partial result reports the samples it actually ran, so pass@k is not computed
        # over a sample budget that never executed.
        self.assertEqual(len(result.samples), 1)
        self.assertEqual(result.to_dict()["n_samples"], 1)

    def test_without_a_stop_signal_nothing_changes(self):
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        verifier = FakeVerifier([_sim(family="functional_mismatch", sim_log="Failed")], repeat=True)
        result, _design = self._run(client, verifier, RtllmAgentConfig(plan=False, max_repair_rounds=2))
        self.assertEqual(len(result.samples[0].rounds), 3)

    def test_a_candidate_that_prints_the_pass_banner_is_scored_by_the_real_verifier(self):
        # End to end through the loop with the REAL verifier gate (no iverilog needed: the
        # refusal happens before compilation). A response like this used to score a pass.
        cheating = (
            "module adder_8bit(input [7:0] a, input [7:0] b, input cin,\n"
            "                  output [7:0] sum, output cout);\n"
            "  assign sum = 8'b0;\n"
            "  assign cout = 1'b0;\n"
            "  initial begin\n"
            '    $display("===========Your Design Passed===========");\n'
            "    $finish;\n"
            "  end\n"
            "endmodule\n"
        )
        client = FakeLLM([_fenced(cheating)], repeat=True)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            design = _make_design(tmp / "bench", "adder_8bit")
            result = run_design(
                design, client, RtllmAgentConfig(plan=False, max_repair_rounds=1), tmp / "work"
            )

        sample = result.samples[0]
        self.assertFalse(sample.func_pass)
        self.assertFalse(sample.func_pass_strict)
        self.assertEqual(result.func_success, 0)
        self.assertEqual(sample.rounds[0].sim.failure_family, "illegal_system_task")
        # extract_verilog left the module intact -- the verifier is the gate, not the parser.
        self.assertIn("$display", sample.rounds[0].rtl)
        # ... and the repair agent is told exactly what to remove.
        repair_prompt = client.calls[-1][1]
        self.assertIn("illegal_system_task", repair_prompt)
        self.assertIn("$display", repair_prompt)

    def test_harness_defect_family_is_not_repaired(self):
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        verifier = FakeVerifier(
            [_sim(family="missing_golden_data", sim_log="ERROR: cannot open reference.dat")]
        )
        config = RtllmAgentConfig(plan=False, max_repair_rounds=3)
        result, _design = self._run(client, verifier, config)

        self.assertEqual(len(result.samples[0].rounds), 1)
        self.assertEqual(len(client.calls), 1)

    def test_samples_are_counted_the_way_pass_at_k_expects(self):
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        results = [_sim(func=True), _sim(syntax=False, family="compile_error", compile_log="err")]
        verifier = FakeVerifier(results + [results[-1]])
        config = RtllmAgentConfig(plan=False, max_repair_rounds=0, samples=2)
        result, _design = self._run(client, verifier, config)

        self.assertEqual(len(result.samples), 2)
        self.assertEqual([s.sample for s in result.samples], [0, 1])
        self.assertEqual(result.func_success, 1)
        self.assertEqual(result.syntax_success, 1)
        payload = result.to_dict()
        self.assertEqual(payload["n_samples"], 2)
        self.assertEqual(payload["design"], "adder_8bit")
        self.assertEqual(payload["category"], "Arithmetic/Adder")

    def test_log_callback_is_the_only_output_channel(self):
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        verifier = FakeVerifier([_sim(func=True)])
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            design = _make_design(tmp / "bench")
            with mock.patch.object(rtllm_agent, "evaluate_rtl", verifier):
                run_design(
                    design,
                    client,
                    RtllmAgentConfig(plan=False, max_repair_rounds=0),
                    tmp / "work",
                    log=messages.append,
                )
        self.assertTrue(any("round 0" in m for m in messages))

    def test_missing_client_is_reported_as_an_llm_error(self):
        verifier = FakeVerifier([_sim(func=True)])
        config = RtllmAgentConfig(plan=False, max_repair_rounds=1)
        result, _design = self._run(None, verifier, config)
        self.assertIn("no LLM client", result.samples[0].rounds[0].llm_error or "")
        self.assertEqual(verifier.calls, [])


# --------------------------------------------------------------------------- #
# evidence policy: configuration
# --------------------------------------------------------------------------- #


class EvidencePolicyConfigTests(unittest.TestCase):
    def test_the_ladder_is_the_documented_one(self):
        self.assertEqual(EVIDENCE_POLICIES, ("none", "logs", "self", "oracle"))

    def test_only_oracle_is_marked_oracle_derived(self):
        self.assertEqual(ORACLE_DERIVED_POLICIES, frozenset({"oracle"}))
        for policy in ("none", "logs", "self"):
            self.assertFalse(RtllmAgentConfig(evidence_policy=policy).oracle_derived_evidence)
        self.assertTrue(RtllmAgentConfig(evidence_policy="oracle").oracle_derived_evidence)

    def test_a_typo_is_rejected_rather_than_degraded_to_logs(self):
        # Silently falling back would report a run as "logs" that the user asked to be
        # "self", which is a mislabeled measurement, not a harmless default.
        with self.assertRaises(ValueError) as caught:
            RtllmAgentConfig(evidence_policy="sef")
        self.assertIn("evidence_policy", str(caught.exception))

    def test_case_is_normalised(self):
        self.assertEqual(RtllmAgentConfig(evidence_policy="ORACLE").evidence_policy, "oracle")

    def test_the_policy_and_its_track_are_both_in_to_dict(self):
        payload = RtllmAgentConfig(evidence_policy="oracle").to_dict()
        self.assertEqual(payload["evidence_policy"], "oracle")
        self.assertIs(payload["oracle_derived_evidence"], True)
        self.assertIs(RtllmAgentConfig().to_dict()["oracle_derived_evidence"], False)


# --------------------------------------------------------------------------- #
# family-specific repair instructions
# --------------------------------------------------------------------------- #


class RepairInstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.design = _make_design(self.tmp / "bench")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_repairable_family_has_its_own_instructions(self):
        # A harness defect the loop refuses to repair needs no procedure; everything else the
        # verifier can emit must have one. Driven off UNREPAIRABLE_FAMILIES rather than a
        # hardcoded name, so adding a new unrepairable family does not silently require
        # inventing repair instructions for a failure the repair loop will never see.
        for family in FAILURE_FAMILIES:
            if family in UNREPAIRABLE_FAMILIES:
                continue
            with self.subTest(family=family):
                self.assertIn(family, FAMILY_REPAIR_INSTRUCTIONS)
                self.assertTrue(repair_instructions(self.design, family).strip())

    def test_the_instructions_actually_differ_by_family(self):
        texts = {
            family: repair_instructions(self.design, family)
            for family in ("compile_error", "functional_mismatch", "timeout", "port_mismatch")
        }
        self.assertEqual(len(set(texts.values())), len(texts))

    def test_the_timeout_procedure_is_about_liveness(self):
        text = repair_instructions(self.design, "timeout").lower()
        for topic in ("combinational loop", "clock edge", "terminating condition"):
            self.assertIn(topic, text)

    def test_the_functional_procedure_forbids_touching_the_interface(self):
        text = repair_instructions(self.design, "functional_mismatch")
        self.assertIn("keep the port list exactly as it is", text.lower())

    def test_interface_families_restate_the_module_name_and_port_list(self):
        for family in ("missing_module", "port_mismatch"):
            with self.subTest(family=family):
                text = repair_instructions(self.design, family)
                self.assertIn("INTERFACE CONTRACT VIOLATED", text)
                self.assertIn("Module name: adder_8bit", text)
                self.assertIn("a[7:0]: 8-bit input operand A.", text)
                self.assertIn("cout: Carry-out output.", text)

    def test_the_restatement_comes_from_the_description_not_the_reference(self):
        restated = interface_restatement(self.design)
        self.assertIn("Input ports:", restated)
        self.assertIn("Output ports:", restated)
        # It stops at the blank line before the trailing prose, and never reads the golden RTL.
        self.assertNotIn("Give me the complete code", restated)
        self.assertNotIn(GOLDEN_REF_MARKER, restated)
        self.assertNotIn(GOLDEN_TB_MARKER, restated)

    def test_a_description_without_an_interface_section_still_names_the_module(self):
        bare = RtllmDesign(
            name="widget",
            category="Misc",
            directory=self.design.directory,
            description="Build something nice.",
            testbench=self.design.testbench,
            reference_files=(),
        )
        self.assertIn("Module name: widget", interface_restatement(bare))

    def test_an_unknown_family_yields_no_instructions(self):
        self.assertEqual(repair_instructions(self.design, None), "")
        self.assertEqual(repair_instructions(self.design, "not_a_family"), "")

    def test_instructions_reach_the_evidence_text(self):
        sim = _sim(syntax=False, family="port_mismatch", compile_log="is not a port of adder_8bit")
        text = build_evidence(sim, RtllmAgentConfig(), design=self.design)
        self.assertIn("INTERFACE CONTRACT VIOLATED", text)
        self.assertIn("is not a port of adder_8bit", text)
        # ... and nothing at all under the blind-retry policy.
        blind = build_evidence(sim, RtllmAgentConfig(evidence_policy="none"), design=self.design)
        self.assertNotIn("INTERFACE CONTRACT VIOLATED", blind)
        self.assertNotIn("port_mismatch", blind)


# --------------------------------------------------------------------------- #
# failure_analyst: which channel fires under which policy
# --------------------------------------------------------------------------- #


class FailureAnalystTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.design = _make_design(self.tmp / "bench")
        self.spies = EvidenceSpies()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _analyse(self, policy, sim, rtl=DESIGN_RTL):
        config = RtllmAgentConfig(plan=False, evidence_policy=policy)
        with self.spies.patch():
            return failure_analyst(self.design, rtl, sim, config, self.tmp / "ev")

    def test_none_returns_the_blind_notice_and_runs_no_sub_run(self):
        text, sources = self._analyse("none", _sim(sim_log="Failed: sum wrong"))
        self.assertEqual(sources, ("none",))
        self.assertIn("blind retry", text)
        self.assertNotIn("functional_mismatch", text)
        self.assertNotIn("Failed: sum wrong", text)
        self.assertEqual(self.spies.trace_calls, [])
        self.assertEqual(self.spies.diff_calls, [])
        self.assertEqual(self.spies.diagnosis_calls, [])

    def test_logs_passes_the_tool_output_and_nothing_else(self):
        text, sources = self._analyse("logs", _sim(sim_log="Failed: sum wrong"))
        self.assertEqual(sources, ("logs",))
        self.assertIn("Failed: sum wrong", text)
        self.assertNotIn(SELF_TRACE_MARKER, text)
        self.assertNotIn(ORACLE_DIFF_MARKER, text)
        self.assertEqual(self.spies.trace_calls, [])
        self.assertEqual(self.spies.diff_calls, [])

    def test_self_adds_a_trace_of_the_candidates_own_signals(self):
        text, sources = self._analyse("self", _sim(sim_log="Failed: sum wrong"))
        self.assertEqual(sources, ("logs", "self_trace"))
        self.assertIn("Failed: sum wrong", text)  # logs are still there
        self.assertIn(SELF_TRACE_MARKER, text)
        self.assertIn("your scored file is untouched", text)
        self.assertNotIn(ORACLE_DIFF_MARKER, text)
        self.assertEqual(self.spies.trace_calls, [DESIGN_RTL])
        self.assertEqual(self.spies.diff_calls, [])

    def test_self_skips_the_trace_when_the_candidate_did_not_compile(self):
        # Nothing to trace: the instrumented copy would fail to compile too.
        text, sources = self._analyse(
            "self", _sim(syntax=False, family="compile_error", compile_log="syntax error")
        )
        self.assertEqual(sources, ("logs",))
        self.assertNotIn(SELF_TRACE_MARKER, text)
        self.assertEqual(self.spies.trace_calls, [])

    def test_oracle_adds_the_behavioural_diff(self):
        text, sources = self._analyse("oracle", _sim(sim_log="Failed: sum wrong"))
        self.assertEqual(sources, ("logs", "oracle_diff"))
        self.assertIn(ORACLE_DIFF_MARKER, text)
        self.assertIn("ORACLE-DERIVED EVIDENCE", text)
        self.assertNotIn(SELF_TRACE_MARKER, text)  # one extra channel, not both
        self.assertEqual(self.spies.diff_calls, ["Failed: sum wrong"])
        self.assertEqual(self.spies.trace_calls, [])

    def test_oracle_skips_the_diff_when_the_candidate_did_not_compile(self):
        _text, sources = self._analyse(
            "oracle", _sim(syntax=False, family="compile_error", compile_log="syntax error")
        )
        self.assertEqual(sources, ("logs",))
        self.assertEqual(self.spies.diff_calls, [])

    def test_a_timeout_is_diagnosed_even_under_the_logs_policy(self):
        # The bug this closes: a watchdog kill leaves an EMPTY sim log, so `logs` alone hands
        # the repair agent "(no captured tool output)" and it repairs blind for every round.
        timeout = _sim(family="timeout", timed_out=True, sim_log="")
        text, sources = self._analyse("logs", timeout)
        self.assertEqual(sources, ("logs", "timeout_diagnosis"))
        self.assertIn(TIMEOUT_DIAG_MARKER, text)
        self.assertIn("Timeout diagnosis", text)
        self.assertEqual(self.spies.diagnosis_calls, [DESIGN_RTL])

    def test_a_timeout_is_not_diagnosed_under_none(self):
        _text, sources = self._analyse("none", _sim(family="timeout", timed_out=True, sim_log=""))
        self.assertEqual(sources, ("none",))
        self.assertEqual(self.spies.diagnosis_calls, [])

    def test_a_timeout_under_self_uses_the_bounded_diagnosis_not_the_unbounded_trace(self):
        _text, sources = self._analyse("self", _sim(family="timeout", timed_out=True, sim_log=""))
        self.assertEqual(sources, ("logs", "timeout_diagnosis"))
        self.assertEqual(self.spies.trace_calls, [])
        self.assertEqual(len(self.spies.diagnosis_calls), 1)

    def test_a_timeout_under_oracle_gets_both_channels(self):
        _text, sources = self._analyse("oracle", _sim(family="timeout", timed_out=True, sim_log=""))
        self.assertEqual(sources, ("logs", "timeout_diagnosis", "oracle_diff"))

    def test_a_failing_sub_run_costs_the_evidence_not_the_sweep(self):
        def explode(*args, **kwargs):
            raise RuntimeError("iverilog vanished")

        config = RtllmAgentConfig(plan=False, evidence_policy="self")
        messages: list[str] = []
        with mock.patch.object(rtllm_agent, "run_self_trace", explode):
            text, sources = failure_analyst(
                self.design,
                DESIGN_RTL,
                _sim(sim_log="Failed: sum wrong"),
                config,
                self.tmp / "ev2",
                log=messages.append,
            )
        self.assertEqual(sources, ("logs",))
        self.assertIn("Failed: sum wrong", text)
        self.assertTrue(any("self_trace evidence unavailable" in m for m in messages))

    def test_sub_runs_never_share_a_directory_with_a_scored_attempt(self):
        seen: list[Path] = []

        def record(design, rtl_text, workdir, **kwargs):
            seen.append(Path(workdir))
            return _fake_trace()

        config = RtllmAgentConfig(plan=False, evidence_policy="self")
        with mock.patch.object(rtllm_agent, "run_self_trace", record):
            failure_analyst(self.design, DESIGN_RTL, _sim(), config, self.tmp / "round1_evidence")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("round1", seen[0].name)
        self.assertIn("self_trace", seen[0].name)


# --------------------------------------------------------------------------- #
# the loop under each policy: what actually reaches the model
# --------------------------------------------------------------------------- #


class EvidencePolicyLoopTests(unittest.TestCase):
    """End-to-end through ``run_design``, asserting on the prompts the model received."""

    def _run(self, policy, verifier, *, rounds: int = 1, spies=None):
        spies = spies or EvidenceSpies()
        client = FakeLLM([_fenced(DESIGN_RTL)], repeat=True)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            design = _make_design(tmp / "bench")
            config = RtllmAgentConfig(plan=False, max_repair_rounds=rounds, evidence_policy=policy)
            with mock.patch.object(rtllm_agent, "evaluate_rtl", verifier), spies.patch():
                result = run_design(design, client, config, tmp / "work")
        return result, client, spies

    def test_the_golden_rtl_and_testbench_source_reach_no_prompt_under_any_strict_policy(self):
        for policy in ("none", "logs", "self"):
            with self.subTest(policy=policy):
                verifier = FakeVerifier([_sim(sim_log="Failed at vector 3")], repeat=True)
                _result, client, _spies = self._run(policy, verifier, rounds=2)
                prompts = client.prompts
                self.assertNotIn(GOLDEN_TB_MARKER, prompts)
                self.assertNotIn(GOLDEN_REF_MARKER, prompts)
                self.assertNotIn("verified_adder_8bit", prompts)
                self.assertNotIn("testbench.v", prompts)
                # ... and no oracle-derived channel fired.
                self.assertNotIn(ORACLE_DIFF_MARKER, prompts)
                self.assertNotIn("ORACLE-DERIVED", prompts)

    def test_oracle_derived_evidence_appears_only_under_the_oracle_policy(self):
        for policy in EVIDENCE_POLICIES:
            with self.subTest(policy=policy):
                verifier = FakeVerifier([_sim(sim_log="Failed at vector 3")], repeat=True)
                _result, client, _spies = self._run(policy, verifier)
                present = ORACLE_DIFF_MARKER in client.prompts
                self.assertEqual(present, policy == "oracle")

    def test_the_self_trace_appears_only_under_the_self_policy(self):
        for policy in EVIDENCE_POLICIES:
            with self.subTest(policy=policy):
                verifier = FakeVerifier([_sim(sim_log="Failed at vector 3")], repeat=True)
                _result, client, _spies = self._run(policy, verifier)
                self.assertEqual(SELF_TRACE_MARKER in client.prompts, policy == "self")

    def test_even_under_oracle_the_reference_source_is_never_quoted(self):
        # The oracle track hands over BEHAVIOUR. The answer key stays on disk.
        verifier = FakeVerifier([_sim(sim_log="Failed at vector 3")], repeat=True)
        _result, client, _spies = self._run("oracle", verifier, rounds=2)
        prompts = client.prompts
        self.assertIn(ORACLE_DIFF_MARKER, prompts)
        self.assertNotIn(GOLDEN_REF_MARKER, prompts)
        self.assertNotIn(GOLDEN_TB_MARKER, prompts)
        self.assertNotIn("verified_adder_8bit", prompts)

    def test_the_instrumented_copy_is_never_handed_to_the_verifier(self):
        verifier = FakeVerifier([_sim(sim_log="Failed")], repeat=True)
        result, client, spies = self._run("self", verifier, rounds=2)
        self.assertEqual(len(spies.trace_calls), 2)  # the trace really ran, twice
        for scored_rtl, _workdir in verifier.calls:
            self.assertNotIn("$strobe", scored_rtl)
            self.assertNotIn("RTLLM_TRACE", scored_rtl)
            self.assertNotIn("`timescale", scored_rtl)
        for record in result.samples[0].rounds:
            self.assertNotIn("$strobe", record.rtl)
        self.assertNotIn("$strobe", result.samples[0].final_rtl)

    def test_the_policy_and_the_channels_used_are_recorded_on_every_row(self):
        verifier = FakeVerifier([_sim(sim_log="Failed")], repeat=True)
        result, _client, _spies = self._run("oracle", verifier, rounds=1)
        payload = result.to_dict()
        self.assertIs(payload["oracle_derived_evidence"], True)
        sample = payload["samples"][0]
        self.assertEqual(sample["evidence_policy"], "oracle")
        self.assertIs(sample["oracle_derived_evidence"], True)
        self.assertEqual(sample["evidence_sources"], ["logs", "oracle_diff"])
        # The generation round consumed no evidence; the repair round did.
        self.assertEqual(sample["rounds"][0]["evidence_sources"], [])
        self.assertEqual(sample["rounds"][1]["evidence_sources"], ["logs", "oracle_diff"])

    def test_a_strict_run_is_stamped_as_not_oracle_derived(self):
        verifier = FakeVerifier([_sim(sim_log="Failed")], repeat=True)
        result, _client, _spies = self._run("self", verifier, rounds=1)
        payload = result.to_dict()
        self.assertIs(payload["oracle_derived_evidence"], False)
        self.assertIs(payload["samples"][0]["oracle_derived_evidence"], False)
        self.assertEqual(payload["samples"][0]["evidence_sources"], ["logs", "self_trace"])

    def test_a_design_that_passes_first_time_still_carries_its_track(self):
        verifier = FakeVerifier([_sim(func=True)], repeat=True)
        result, _client, spies = self._run("oracle", verifier, rounds=2)
        self.assertIs(result.to_dict()["oracle_derived_evidence"], True)
        self.assertEqual(spies.diff_calls, [])  # nothing failed, so nothing was diffed

    def test_a_hang_reaches_the_repair_agent_with_a_diagnosis_instead_of_an_empty_log(self):
        verifier = FakeVerifier([_sim(family="timeout", timed_out=True, sim_log="")], repeat=True)
        _result, client, spies = self._run("logs", verifier, rounds=1)
        repair_prompt = client.calls[-1][1]
        self.assertIn(TIMEOUT_DIAG_MARKER, repair_prompt)
        self.assertIn("last simulation time reached", repair_prompt)
        self.assertIn("combinational loop", repair_prompt)  # the timeout repair procedure
        self.assertEqual(len(spies.diagnosis_calls), 1)

    def test_the_gate_still_refuses_a_self_printing_candidate_under_the_self_policy(self):
        # Instrumentation must not be an excuse to relax the gate: a candidate shipping its
        # own $display can print the oracle's pass marker.
        cheating = (
            "module adder_8bit(input [7:0] a, input [7:0] b, input cin,\n"
            "                  output [7:0] sum, output cout);\n"
            "  assign sum = 8'b0;\n"
            "  assign cout = 1'b0;\n"
            "  initial begin\n"
            '    $display("===========Your Design Passed===========");\n'
            "    $finish;\n"
            "  end\n"
            "endmodule\n"
        )
        client = FakeLLM([_fenced(cheating)], repeat=True)
        spies = EvidenceSpies()
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            design = _make_design(tmp / "bench")
            config = RtllmAgentConfig(plan=False, max_repair_rounds=1, evidence_policy="self")
            with spies.patch():  # real verifier, faked sub-runs
                result = run_design(design, client, config, tmp / "work")

        sample = result.samples[0]
        self.assertFalse(sample.func_pass)
        self.assertFalse(sample.func_pass_strict)
        self.assertEqual(sample.rounds[0].sim.failure_family, "illegal_system_task")
        # It never compiled, so no trace was attempted ...
        self.assertEqual(spies.trace_calls, [])
        # ... and the repair agent is told exactly what to delete.
        repair_prompt = client.calls[-1][1]
        self.assertIn("illegal_system_task", repair_prompt)
        self.assertIn("There is no acceptable use of them in a design", repair_prompt)


if __name__ == "__main__":
    unittest.main()
