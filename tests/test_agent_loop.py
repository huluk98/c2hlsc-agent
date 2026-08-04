import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.agent_loop import classify_failure, classify_log_family, multi_agent_procedures
from c2hlsc_agent.equivalence import PhaseResult, VerificationState


class AgentLoopTests(unittest.TestCase):
    def test_declares_multi_agent_pipeline(self):
        names = [procedure.name for procedure in multi_agent_procedures()]
        self.assertIn("shift_left_testbench_agent", names)
        self.assertIn("hlsc_generator_agent", names)
        self.assertIn("cosim_operator", names)
        self.assertIn("rtl_optimizer_agent", names)

    def test_routes_host_mismatch_to_failure_analyst(self):
        state = VerificationState()
        state.add_phase(
            PhaseResult(
                "software_equivalence",
                "fail",
                stdout="Mismatch test=5 arg=out index=7 expected=12 actual=13 seed=123",
            )
        )
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.family, "host_behavior_mismatch")
        self.assertEqual(decision.owner_agent, "failure_analyst")

    def test_routes_missing_vitis_to_operator(self):
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        state.add_phase(PhaseResult("csim", "fail", summary="vitis_hls not found on PATH"))
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.family, "toolchain_unavailable")
        self.assertEqual(decision.owner_agent, "cosim_operator")
        self.assertEqual(decision.status, "blocked")

    def test_routes_missing_unified_vitis_launcher_to_operator(self):
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        state.add_phase(
            PhaseResult("csim", "fail", summary="Vitis HLS launcher 'vitis-run' not found")
        )
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.family, "toolchain_unavailable")
        self.assertEqual(decision.owner_agent, "cosim_operator")
        self.assertEqual(decision.status, "blocked")

    def test_all_pass_hands_to_optimizer(self):
        state = VerificationState()
        for phase in ("software_equivalence", "csim", "csynth", "cosim"):
            state.add_phase(PhaseResult(phase, "pass"))
        decision = classify_failure(state, run_vitis_requested=True)
        self.assertEqual(decision.family, "functional_equivalence_signed_off")
        self.assertEqual(decision.owner_agent, "rtl_optimizer_agent")
        self.assertEqual(decision.status, "pass")

    def test_classifies_synthesis_memory_failure(self):
        family = classify_log_family("csynth", "ERROR: unsupported pointer aliasing and memory bound")
        self.assertEqual(family, "memory_pointer")

    def test_routes_named_relational_klee_counterexample_to_failure_analyst(self):
        state = VerificationState()
        for phase in ("software_equivalence", "shift_left_trace", "coverage_gcov"):
            state.add_phase(PhaseResult(phase, "pass"))
        state.add_phase(
            PhaseResult(
                "symbolic_klee",
                "fail",
                metadata={
                    "schema": "c2hlsc-klee-report-v1",
                    "scope": "golden_hlsc_relational",
                    "outcome": "counterexample",
                    "failure_kind": "relational_counterexample",
                    "invocations": 1,
                    "observable_count": 1,
                    "top": "kernel",
                    "assumptions": {
                        "pointer_alias_model": "distinct_pointer_arguments",
                        "hidden_state_model": "no_mutable_hidden_state",
                        "comparison": "return_and_complete_pointer_post_state",
                    },
                    "artifact_sha256": {
                        relative: "0" * 64
                        for relative in (
                            "input.c",
                            "src/hls_top.hpp",
                            "src/hls_top.cpp",
                            "tb/klee_driver.cpp",
                            "tb/leveri_manifest.json",
                        )
                    },
                    "counterexample_names": ["C2HLSC_RELATIONAL_MISMATCH:return"],
                },
            )
        )

        decision = classify_failure(state, run_vitis_requested=True)

        self.assertEqual(decision.family, "klee_relational_counterexample")
        self.assertEqual(decision.owner_agent, "failure_analyst")
        self.assertEqual(decision.status, "needs_action")

    def test_unscoped_klee_failure_cannot_authorize_hlsc_repair(self):
        state = VerificationState()
        for phase in ("software_equivalence", "shift_left_trace", "coverage_gcov"):
            state.add_phase(PhaseResult(phase, "pass"))
        state.add_phase(
            PhaseResult(
                "symbolic_klee",
                "fail",
                metadata={"counterexample_names": ["C2HLSC_RELATIONAL_MISMATCH:return"]},
            )
        )

        decision = classify_failure(state, run_vitis_requested=True)

        self.assertEqual(decision.family, "symbolic_execution_failure")
        self.assertEqual(decision.status, "blocked")
        self.assertIn("no automatic HLS-C mutation", decision.repair_scope)

    def test_generic_named_klee_error_cannot_authorize_hlsc_repair(self):
        state = VerificationState()
        for phase in ("software_equivalence", "shift_left_trace", "coverage_gcov"):
            state.add_phase(PhaseResult(phase, "pass"))
        state.add_phase(
            PhaseResult(
                "symbolic_klee",
                "fail",
                metadata={
                    "schema": "c2hlsc-klee-report-v1",
                    "scope": "golden_hlsc_relational",
                    "outcome": "counterexample",
                    "failure_kind": "relational_counterexample",
                    "counterexample_names": ["division_by_zero"],
                },
            )
        )

        decision = classify_failure(state, run_vitis_requested=True)

        self.assertEqual(decision.family, "symbolic_execution_failure")
        self.assertEqual(decision.status, "blocked")


if __name__ == "__main__":
    unittest.main()
