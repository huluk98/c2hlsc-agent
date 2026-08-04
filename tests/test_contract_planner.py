import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.cli import build_parser, run_convert
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.contract_planner import plan_contracts
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.llm import extract_json_block


SOURCE = """
void scale(const int *data, int *out, int n) {
  for (int i = 0; i < n; ++i) {
    out[i] = data[i] * 2;
  }
}
"""

PLANNER_RESPONSE = """Contract review complete.

```json
{
  "arguments": {
    "data": {"direction": "input", "length": 8},
    "out": {"direction": "output", "length": 8},
    "n": {"range": [1, 8]}
  },
  "notes": "n bounds the active length of both 8-element arrays."
}
```
"""


class FakeLLM:
    def __init__(self, response: str, model: str = "fake-model"):
        self.response = response
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> str:
        self.calls.append((system, user))
        return self.response


class ExplodingLLM:
    model = "exploding-model"

    def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> str:
        raise RuntimeError("backend down")


def _analysis(tmp: Path, config: AgentConfig):
    input_path = tmp / "input.c"
    input_path.write_text(SOURCE, encoding="utf-8")
    config.input_files = [input_path]
    config.top = "scale"
    return analyze_source(input_path, "scale", config)


class ExtractJsonBlockTests(unittest.TestCase):
    def test_prefers_json_fenced_block(self):
        text = 'prose\n```json\n{"a": 1}\n```\nmore'
        self.assertEqual(extract_json_block(text), {"a": 1})

    def test_untagged_fence_fallback(self):
        text = '```\n{"b": [1, 2]}\n```'
        self.assertEqual(extract_json_block(text), {"b": [1, 2]})

    def test_raw_balanced_object_fallback(self):
        text = 'the plan is {"c": {"nested": "yes"}} thanks'
        self.assertEqual(extract_json_block(text), {"c": {"nested": "yes"}})

    def test_braces_inside_strings_do_not_confuse_the_scan(self):
        text = 'x {"key": "value with } brace"} y'
        self.assertEqual(extract_json_block(text), {"key": "value with } brace"})

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_json_block("no json here { broken"))

    def test_cpp_block_is_not_json(self):
        self.assertIsNone(extract_json_block("```cpp\nint main() { return 0; }\n```"))


class PlanContractsTests(unittest.TestCase):
    def test_valid_proposals_are_merged_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True, plan_contracts=True)
            analysis = _analysis(Path(tmp), config)
            fake = FakeLLM(PLANNER_RESPONSE)

            result = plan_contracts(analysis, config, fake, SOURCE)

            self.assertTrue(result.raw_ok)
            self.assertEqual(result.applied["data"], ["direction", "length"])
            self.assertEqual(result.applied["n"], ["range"])
            self.assertEqual(config.arguments["data"].length, 8)
            self.assertEqual(config.arguments["out"].direction, "output")
            self.assertEqual(config.arguments["n"].range, (1, 8))
            self.assertEqual(result.model, "fake-model")
            # Prompt carries the contract evidence but is a planning prompt, not codegen.
            system, user = fake.calls[0]
            self.assertIn("contract_planner", system)
            self.assertIn("`scale`", user)
            self.assertIn("data", user)
            self.assertIn("void scale(const int *data, int *out, int n)", user)

    def test_user_config_wins_per_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True, plan_contracts=True)
            config.arguments["data"] = ArgumentConfig(length=32)
            analysis = _analysis(Path(tmp), config)

            result = plan_contracts(analysis, config, FakeLLM(PLANNER_RESPONSE), SOURCE)

            # length stays user-set; direction (unset by the user) is filled in.
            self.assertEqual(config.arguments["data"].length, 32)
            self.assertEqual(config.arguments["data"].direction, "input")
            self.assertEqual(result.applied["data"], ["direction"])

    def test_garbage_response_degrades_to_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True, plan_contracts=True)
            analysis = _analysis(Path(tmp), config)

            result = plan_contracts(analysis, config, FakeLLM("I could not analyze this."), SOURCE)

            self.assertFalse(result.raw_ok)
            self.assertEqual(result.proposals, {})
            self.assertEqual(config.arguments, {})

    def test_llm_exception_degrades_to_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True, plan_contracts=True)
            analysis = _analysis(Path(tmp), config)

            result = plan_contracts(analysis, config, ExplodingLLM(), SOURCE)

            self.assertFalse(result.raw_ok)
            self.assertEqual(result.proposals, {})
            self.assertFalse(result.changed)
            self.assertEqual(config.arguments, {})

    def test_invalid_proposals_are_skipped_with_reasons(self):
        response = """```json
{
  "arguments": {
    "ghost": {"direction": "input"},
    "data": {"direction": "sideways"},
    "out": {"length": -4},
    "n": {"range": [9, 1]}
  }
}
```"""
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True, plan_contracts=True)
            analysis = _analysis(Path(tmp), config)

            result = plan_contracts(analysis, config, FakeLLM(response), SOURCE)

            self.assertTrue(result.raw_ok)
            self.assertEqual(result.proposals, {})
            self.assertEqual(config.arguments, {})
            self.assertIn("unknown argument", result.skipped["ghost"])
            self.assertIn("invalid direction", result.skipped["data"])
            self.assertIn("invalid length", result.skipped["out"])
            self.assertIn("invalid range", result.skipped["n"])

    def test_boolean_length_is_rejected(self):
        response = '```json\n{"arguments": {"data": {"length": true}}}\n```'
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True, plan_contracts=True)
            analysis = _analysis(Path(tmp), config)
            result = plan_contracts(analysis, config, FakeLLM(response), SOURCE)
            self.assertEqual(result.proposals, {})
            self.assertIn("invalid length", result.skipped["data"])


class PlannerCliIntegrationTests(unittest.TestCase):
    def test_plan_contracts_flag_applies_bounds_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.c"
            input_path.write_text(SOURCE, encoding="utf-8")
            out_dir = root / "out"
            args = build_parser().parse_args(
                [
                    "convert",
                    "--input",
                    str(input_path),
                    "--top",
                    "scale",
                    "--out",
                    str(out_dir),
                    "--no-run-vitis",
                    "--plan-contracts",
                ]
            )
            state = VerificationState()
            state.add_phase(PhaseResult("software_equivalence", "pass"))
            fake = FakeLLM(PLANNER_RESPONSE)

            with patch("c2hlsc_agent.cli.build_llm_client", return_value=fake), patch(
                "c2hlsc_agent.cli.verify_project", return_value=state
            ):
                rc = run_convert(args)

            self.assertEqual(rc, 0)
            plan = json.loads((out_dir / "contract_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["applied"]["data"], ["direction", "length"])
            # Re-analysis took effect: the testbench sizes the arrays with the planner's
            # bound (8), not the analyzer's default guess (16).
            testbench = (out_dir / "tb" / "testbench.cpp").read_text(encoding="utf-8")
            self.assertIn("[8]", testbench)
            self.assertNotIn("[16]", testbench)
            # Transformations are rendered in the markdown report (the JSON report has
            # no transformations key).
            report_md = (out_dir / "conversion_report.md").read_text(encoding="utf-8")
            self.assertIn("contract_planner:", report_md)

    def test_no_plan_contracts_overrides_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.c"
            input_path.write_text(SOURCE, encoding="utf-8")
            config_path = root / "config.yaml"
            config_path.write_text("plan_contracts: true\nuse_llm: true\n", encoding="utf-8")
            out_dir = root / "out"
            args = build_parser().parse_args(
                [
                    "convert",
                    "--input",
                    str(input_path),
                    "--top",
                    "scale",
                    "--out",
                    str(out_dir),
                    "--no-run-vitis",
                    "--config",
                    str(config_path),
                    "--no-plan-contracts",
                ]
            )
            state = VerificationState()
            state.add_phase(PhaseResult("software_equivalence", "pass"))
            fake = FakeLLM(PLANNER_RESPONSE)

            with patch("c2hlsc_agent.cli.build_llm_client", return_value=fake), patch(
                "c2hlsc_agent.cli.verify_project", return_value=state
            ):
                rc = run_convert(args)

            self.assertEqual(rc, 0)
            self.assertFalse((out_dir / "contract_plan.json").exists())


if __name__ == "__main__":
    unittest.main()
