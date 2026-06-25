import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.agent_loop import hlsc_generator_policy, multi_agent_procedures
from c2hlsc_agent.hlsc_generator import (
    HLSC_GENERATOR_OUTPUT_SECTIONS,
    HLSC_GENERATOR_PROMPT_ID,
    HLSC_GENERATOR_SYSTEM_PROMPT,
    get_hlsc_generator_contract,
    render_hlsc_generator_task,
)


class HlscGeneratorTests(unittest.TestCase):
    def test_prompt_contains_required_hlsc_generation_contract(self):
        self.assertIn("FPGA HLS code-generation assistant", HLSC_GENERATOR_SYSTEM_PROMPT)
        self.assertIn("default to AMD/Xilinx Vitis HLS syntax", HLSC_GENERATOR_SYSTEM_PROMPT)
        self.assertIn("Identify hotspot loops", HLSC_GENERATOR_SYSTEM_PROMPT)
        self.assertIn("Identify candidate loop-carried dependencies", HLSC_GENERATOR_SYSTEM_PROMPT)
        self.assertIn("Identify array or memory-port bottlenecks", HLSC_GENERATOR_SYSTEM_PROMPT)
        self.assertIn("The original code copied exactly", HLSC_GENERATOR_SYSTEM_PROMPT)
        self.assertIn("Inline comments explaining every pragma placement", HLSC_GENERATOR_SYSTEM_PROMPT)
        self.assertIn("Intel HLS notes", HLSC_GENERATOR_SYSTEM_PROMPT)

    def test_prompt_declares_separate_testbench_ownership(self):
        contract = get_hlsc_generator_contract()
        self.assertEqual(contract.prompt_id, HLSC_GENERATOR_PROMPT_ID)
        self.assertEqual(contract.owner_agent, "hlsc_generator_agent")
        self.assertFalse(contract.owns_testbench)
        self.assertIn("Do not generate the sidecar testbench", contract.system_prompt)

    def test_output_sections_are_exactly_the_requested_order(self):
        self.assertEqual(
            HLSC_GENERATOR_OUTPUT_SECTIONS,
            (
                "1. Assumptions",
                "2. Hotspot analysis",
                "3. Original code",
                "4. Vitis HLS annotated code",
                "5. Expected hardware impact",
                "6. Trade-offs / risks",
                "7. Intel HLS notes",
                "8. Report checklist",
            ),
        )

    def test_agent_loop_exposes_hlsc_generator_policy(self):
        policy = hlsc_generator_policy()
        self.assertEqual(policy["prompt_id"], HLSC_GENERATOR_PROMPT_ID)
        self.assertEqual(policy["owner_agent"], "hlsc_generator_agent")
        self.assertFalse(policy["owns_testbench"])

        generator = [p for p in multi_agent_procedures() if p.name == "hlsc_generator_agent"][0]
        self.assertIn(HLSC_GENERATOR_PROMPT_ID, generator.owns)
        self.assertIn("beginner-facing HLS analysis", generator.outputs)

    def test_render_task_wraps_input_code_without_mutating_it(self):
        code = "int add_one(int x) {\n  return x + 1;\n}\n"
        task = render_hlsc_generator_task(code)
        self.assertIn("```c\nint add_one(int x) {\n  return x + 1;\n}\n```", task)
        self.assertIn("copy the original code exactly in section 3", task)


if __name__ == "__main__":
    unittest.main()
