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


if __name__ == "__main__":
    unittest.main()
