from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.equivalence import Mismatch, PhaseResult, VerificationState
from c2hlsc_agent.evidence_context import (
    EVIDENCE_BUDGET,
    build_repair_evidence,
    distill_evidence,
)


# Realistic Vitis HLS banner: note the indented '** Copyright' lines, which a
# naive '^\*\*' pattern misses.
VITIS_BANNER = """\

****** Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2023.2 (64-bit)
  **** SW Build 4029153 on Fri Oct 13 20:13:54 MDT 2023
  **** IP Build 4028589 on Sat Oct 14 00:45:43 MDT 2023
    ** Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
    ** Copyright 2022-2023 Advanced Micro Devices, Inc. All Rights Reserved.

source /tools/Xilinx/Vitis_HLS/2023.2/scripts/vitis_hls/hls.tcl -notrace
INFO: [HLS 200-10] Running '/tools/Xilinx/Vitis_HLS/2023.2/bin/unwrapped/lnx64.o/vitis_hls'
INFO: [HLS 200-10] Opening project '/scratch/user/proj/hls_project' ...
"""

CSIM_COMPILE_FAILURE_LOG = VITIS_BANNER + """\
INFO: [SIM 211-2] *************** CSIM start ***************
INFO: [SIM 211-4] CSIM will launch GCC as the compiler.
   Compiling /scratch/user/proj/tb/testbench.cpp in debug mode
   Compiling ../../../../src/hls_top.cpp in debug mode
../../../../src/hls_top.cpp:12:5: error: 'foo' was not declared in this scope
   12 |     foo(x);
      |     ^~~
make: *** [obj/hls_top.o] Error 1
ERROR: [SIM 211-100] 'csim_design' failed: compilation error(s).
INFO: [SIM 211-3] *************** CSIM finish ***************
INFO: [HLS 200-111] Finished Command csim_design CPU user time: 1.2 seconds.
command 'ap_source' returned error code
"""

COSIM_FAILURE_LOG = VITIS_BANNER + """\
INFO: [COSIM 212-47] Using XSIM for RTL simulation.
INFO: [COSIM 212-316] Starting C post checking ...
ERROR: [COSIM 212-359] Aborting co-simulation: RTL simulation failed.
ERROR: [COSIM 212-4] *** C/RTL co-simulation finished: FAIL ***
command 'ap_source' returned error code
"""


class EvidenceContextTests(unittest.TestCase):
    def _state_with_log(
        self,
        phase: str,
        log_text: str,
        summary: str = "",
        stdout: str = "",
        stderr: str = "",
        mismatches: tuple[Mismatch, ...] = (),
    ) -> VerificationState:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log_path = Path(tmp.name) / f"{phase}.log"
        log_path.write_text(log_text, encoding="utf-8")
        state = VerificationState()
        state.add_phase(PhaseResult(phase, "fail", 1, stdout, stderr, log_path, summary))
        state.mismatches.extend(mismatches)
        return state

    def test_missing_phase_yields_empty_bundle(self):
        state = VerificationState()
        for phase in (None, "csim"):
            bundle = build_repair_evidence(state, phase)
            self.assertEqual(bundle.text, "")
            self.assertEqual(bundle.sections, ())
            self.assertIsNone(bundle.anchor_kind)
            self.assertEqual(bundle.mismatch_count, 0)
            self.assertFalse(bundle.truncated)

    def test_summary_only_phase_and_provenance_shape(self):
        state = VerificationState()
        state.add_phase(
            PhaseResult(
                "csim",
                "fail",
                summary="vitis_hls not found on PATH (use --vitis-ssh to run Vitis on a remote Linux host)",
            )
        )
        bundle = build_repair_evidence(state, "csim")
        self.assertTrue(bundle.text.startswith("[summary csim]"))
        self.assertIn("vitis_hls not found on PATH", bundle.text)
        self.assertEqual(bundle.sections, ("summary",))
        self.assertEqual(
            set(bundle.to_dict()),
            {"sections", "anchor_kind", "mismatch_count", "truncated"},
        )
        self.assertEqual(bundle.to_dict()["sections"], ["summary"])

    def test_mismatches_lead_bundle_with_failing_output_rollup(self):
        mismatches = (
            Mismatch(0, "out", "3", "4", element_index=1, seed=42),
            Mismatch(0, "out", "5", "6", element_index=2, seed=42),
            Mismatch(1, "return", "7", "9", seed=42),
        )
        log = (
            "Mismatch test=0 arg=out index=1 expected=3 actual=4 seed=42\n"
            "Mismatch test=0 arg=out index=2 expected=5 actual=6 seed=42\n"
            "Mismatch test=1 return expected=7 actual=9 seed=42\n"
            + "scheduling chatter line\n" * 3
        )
        state = self._state_with_log("software_equivalence", log, mismatches=mismatches)
        bundle = build_repair_evidence(state, "software_equivalence")
        self.assertEqual(bundle.mismatch_count, 3)
        self.assertIn("[mismatches] 3 recorded; failing outputs: out (2), return (1)", bundle.text)
        self.assertIn("test=0 out[1]: expected=3 actual=4 seed=42", bundle.text)
        self.assertEqual(bundle.anchor_kind, "mismatch")
        self.assertLess(bundle.text.index("[mismatches]"), bundle.text.index("[log window"))

    def test_mismatch_list_is_capped_and_marked_truncated(self):
        log = "".join(
            f"Mismatch test={i} arg=out index={i} expected=1 actual=2 seed=7\n" for i in range(50)
        )
        state = self._state_with_log("software_equivalence", log)
        bundle = build_repair_evidence(state, "software_equivalence")
        self.assertEqual(bundle.mismatch_count, 50)
        self.assertIn("... (38 more mismatch(es) omitted)", bundle.text)
        self.assertTrue(bundle.truncated)
        self.assertLessEqual(len(bundle.text), EVIDENCE_BUDGET)

    def test_field_form_mismatches_from_hls_nl_testbench_are_distilled(self):
        # The HLS_NL driver testbench prints 'field=' mismatches without arg/index/seed.
        log = VITIS_BANNER + (
            "Mismatch test=3 field=sum expected=7 actual=9\n"
            "ERROR: [COSIM 212-4] *** C/RTL co-simulation finished: FAIL ***\n"
        )
        bundle = distill_evidence(log, phase="cosim")
        self.assertEqual(bundle.mismatch_count, 1)
        self.assertIn("test=3 sum: expected=7 actual=9", bundle.text)
        self.assertEqual(bundle.anchor_kind, "mismatch")

    def test_error_anchor_beats_tail_slice(self):
        error_line = "ERROR: [XFORM 203-504] Stop unrolling loop 'main_loop' because of code size."
        chatter = "".join(f"|  op_{i}  |  fadd  |  4  |  0.25  |  none  |\n" for i in range(220))
        log = VITIS_BANNER + error_line + "\n" + chatter
        # Premise guard: the old blind tail slice would have lost the error line.
        self.assertNotIn("XFORM 203-504", log[-EVIDENCE_BUDGET:])
        state = self._state_with_log("csynth", log, summary="csynth failed")
        bundle = build_repair_evidence(state, "csynth")
        self.assertEqual(bundle.anchor_kind, "vitis_error")
        self.assertIn("XFORM 203-504", bundle.text)
        self.assertIn("anchored_window", bundle.sections)
        self.assertTrue(bundle.truncated)
        self.assertLessEqual(len(bundle.text), EVIDENCE_BUDGET)

    def test_compile_error_anchor_on_realistic_csim_log(self):
        state = self._state_with_log("csim", CSIM_COMPILE_FAILURE_LOG, summary="csim failed")
        bundle = build_repair_evidence(state, "csim")
        self.assertEqual(bundle.anchor_kind, "compile_error")
        # The gcc diagnostic survives with its directory replaced but basename kept.
        self.assertIn("<path>/src/hls_top.cpp:12:5: error:", bundle.text)
        self.assertIn("<path>/testbench.cpp", bundle.text)
        self.assertNotIn("/scratch/user", bundle.text)
        # The Vitis-level verdict after the anchor is still inside the window.
        self.assertIn("ERROR: [SIM 211-100] 'csim_design' failed", bundle.text)
        self.assertNotIn("** Copyright", bundle.text)

    def test_cosim_failure_marker_anchors_without_mismatches(self):
        state = self._state_with_log("cosim", COSIM_FAILURE_LOG, summary="cosim failed")
        bundle = build_repair_evidence(state, "cosim")
        self.assertEqual(bundle.anchor_kind, "cosim_fail")
        self.assertEqual(bundle.mismatch_count, 0)
        self.assertIn("Aborting co-simulation: RTL simulation failed", bundle.text)
        self.assertIn("C/RTL co-simulation finished: FAIL", bundle.text)

    def test_tail_fallback_when_nothing_anchors(self):
        log = "routine output line one\nnothing notable here\nall quiet on close\n"
        bundle = distill_evidence(log)
        self.assertIsNone(bundle.anchor_kind)
        self.assertEqual(bundle.sections, ("tail",))
        for line in ("routine output line one", "nothing notable here", "all quiet on close"):
            self.assertIn(line, bundle.text)
        self.assertFalse(bundle.truncated)

    def test_banner_dropped_and_repeated_lines_collapsed(self):
        warning = "WARNING: [SIM 212-303] Aggregating hls::stream compound port 'data_in'."
        log = VITIS_BANNER + (warning + "\n") * 20
        bundle = distill_evidence(log)
        self.assertEqual(bundle.text.count("Aggregating hls::stream"), 1)
        self.assertIn("(previous line repeated 19 more time(s))", bundle.text)
        self.assertNotIn("** Copyright", bundle.text)
        self.assertNotIn("Vitis HLS - High-Level Synthesis", bundle.text)
        self.assertNotIn("hls.tcl", bundle.text)

    def test_log_supersedes_stdout_stderr_and_mismatches_dedupe(self):
        # run_command copies stdout+stderr into the phase log, so the raw path
        # counted this text twice; the bundle must count it once, and a state
        # mismatch also present in the log must not double the mismatch count.
        stdout = (
            "Mismatch test=0 arg=out index=1 expected=3 actual=4 seed=9\n"
            "UNIQ_TOKEN error: kaboom\n"
        )
        log = stdout + "\n--- stderr ---\n"
        state = self._state_with_log(
            "software_equivalence",
            log,
            stdout=stdout,
            mismatches=(Mismatch(0, "out", "3", "4", element_index=1, seed=9),),
        )
        bundle = build_repair_evidence(state, "software_equivalence")
        self.assertEqual(bundle.mismatch_count, 1)
        self.assertEqual(bundle.text.count("UNIQ_TOKEN"), 1)

    def test_budget_enforced_on_huge_messy_log(self):
        filler_head = "".join(f"early detail row {i}: latency estimate {i % 7} cycles\n" for i in range(500))
        filler_tail = "".join(f"late detail row {i}: latency estimate {i % 7} cycles\n" for i in range(1500))
        log = (
            VITIS_BANNER
            + filler_head
            + "ERROR: [SYNCHK 200-61] unsupported pointer reinterpretation on variable 'buf'.\n"
            + filler_tail
        )
        state = self._state_with_log("csynth", log, summary="csynth failed")
        bundle = build_repair_evidence(state, "csynth")
        self.assertLessEqual(len(bundle.text), EVIDENCE_BUDGET)
        self.assertTrue(bundle.truncated)
        self.assertEqual(bundle.anchor_kind, "vitis_error")
        self.assertIn("SYNCHK 200-61", bundle.text)


if __name__ == "__main__":
    unittest.main()
