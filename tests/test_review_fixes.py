from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.cli import _external_failure_state
from c2hlsc_agent.config import AgentConfig, load_config, merge_cli_config
from c2hlsc_agent.equivalence import PhaseResult
from c2hlsc_agent.hls_runner import _gate_cosim_on_log


class CosimLogGateTests(unittest.TestCase):
    def test_pass_with_failure_marker_is_downgraded(self):
        # Vitis can exit 0 while the CoSim log reports a mismatch.
        result = PhaseResult(
            "cosim", "pass", returncode=0, stdout="C/RTL co-simulation finished: FAIL\n"
        )
        self.assertEqual(_gate_cosim_on_log(result).status, "fail")

    def test_clean_pass_stays_pass(self):
        result = PhaseResult(
            "cosim", "pass", returncode=0, stdout="C/RTL co-simulation finished: PASS\n"
        )
        self.assertEqual(_gate_cosim_on_log(result).status, "pass")

    def test_non_pass_is_untouched(self):
        result = PhaseResult("cosim", "fail", returncode=1, stdout="boom")
        self.assertEqual(_gate_cosim_on_log(result).status, "fail")


class ExternalFailureStateTests(unittest.TestCase):
    def test_stage_not_in_active_phases_is_still_recorded(self):
        # Defensive: a stage outside the active phase list must not be dropped.
        state = _external_failure_state("csim", "log evidence", run_vitis=False)
        self.assertEqual(state.status_for("software_equivalence"), "pass")
        self.assertEqual(state.status_for("csim"), "fail")


class ConfigMergeTests(unittest.TestCase):
    def _args(self, **overrides):
        base = dict(
            keep_going=False,
            auto_repair=False,
            run_vitis=False,
            no_run_vitis=False,
            use_llm=False,
            no_llm=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_config_keep_going_not_clobbered_by_absent_flag(self):
        merged = merge_cli_config(AgentConfig(keep_going=True), self._args())
        self.assertTrue(merged.keep_going)

    def test_cli_keep_going_sets_true(self):
        merged = merge_cli_config(AgentConfig(keep_going=False), self._args(keep_going=True))
        self.assertTrue(merged.keep_going)

    def test_load_config_reads_loop_knobs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text(
            '{"input_files": ["input.c"], "top": "k", '
            '"max_iterations": 5, "auto_repair": true, "keep_going": true}',
            encoding="utf-8",
        )
        config = load_config(path)
        self.assertEqual(config.max_iterations, 5)
        self.assertTrue(config.auto_repair)
        self.assertTrue(config.keep_going)


class MinimalYamlFlowTests(unittest.TestCase):
    """The no-PyYAML fallback must read the config syntax the docs actually recommend.

    PyYAML is an optional extra, so `pip install -e .` alone leaves the built-in parser in
    charge. It handled block style but not flow collections: `[input.c]` raised a bare
    ValueError out of ast.literal_eval (Python reads `input.c` as attribute access), and
    `{direction: input}` came back as a *string*, which surfaced later as the misleading
    "argument metadata must be a mapping".
    """

    def test_flow_list_of_unquoted_strings(self) -> None:
        from c2hlsc_agent.config import _parse_scalar

        self.assertEqual(_parse_scalar("[input.c]"), ["input.c"])
        self.assertEqual(_parse_scalar("[input.c, helpers.c]"), ["input.c", "helpers.c"])

    def test_flow_mapping_becomes_a_dict(self) -> None:
        from c2hlsc_agent.config import _parse_scalar

        self.assertEqual(
            _parse_scalar("{direction: input, length: 16}"),
            {"direction": "input", "length": 16},
        )
        self.assertEqual(_parse_scalar("{range: [0, 16]}"), {"range": [0, 16]})

    def test_flow_splitting_respects_quotes_and_nesting(self) -> None:
        from c2hlsc_agent.config import _parse_scalar

        self.assertEqual(_parse_scalar('["a,b", c]'), ["a,b", "c"])
        self.assertEqual(_parse_scalar("[[1, 2], [3, 4]]"), [[1, 2], [3, 4]])
        self.assertEqual(_parse_scalar("[]"), [])
        self.assertEqual(_parse_scalar("{}"), {})

    def test_a_flow_style_config_loads_without_pyyaml(self) -> None:
        """The documented example in docs/input_contract.md, parsed by the fallback."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yaml"
        path.write_text(
            "input_files: [input.c]\n"
            "top: guarded_scale\n"
            "num_tests: 64\n"
            "arguments:\n"
            "  a:   {direction: input,  length: 16}\n"
            "  out: {direction: output, length: 16}\n"
            "  n:   {range: [0, 16]}\n",
            encoding="utf-8",
        )
        # Force the fallback even where PyYAML happens to be installed.
        with mock.patch.dict(sys.modules, {"yaml": None}):
            config = load_config(path)

        self.assertEqual(config.top, "guarded_scale")
        self.assertEqual(config.num_tests, 64)
        self.assertEqual([p.name for p in config.input_files], ["input.c"])
        self.assertEqual(config.arguments["a"].direction, "input")
        self.assertEqual(config.arguments["a"].length, 16)
        self.assertEqual(config.arguments["out"].direction, "output")
        self.assertEqual(config.arguments["n"].range, (0, 16))

    def test_block_and_flow_styles_agree(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        flow = Path(tmp.name) / "flow.yaml"
        block = Path(tmp.name) / "block.yaml"
        flow.write_text(
            "top: k\narguments:\n  a: {direction: input, length: 8}\n", encoding="utf-8"
        )
        block.write_text(
            "top: k\narguments:\n  a:\n    direction: input\n    length: 8\n", encoding="utf-8"
        )
        with mock.patch.dict(sys.modules, {"yaml": None}):
            a = load_config(flow)
            b = load_config(block)
        self.assertEqual(a.arguments["a"].direction, b.arguments["a"].direction)
        self.assertEqual(a.arguments["a"].length, b.arguments["a"].length)

    def test_a_malformed_inline_mapping_names_the_offender(self) -> None:
        from c2hlsc_agent.config import _parse_scalar

        with self.assertRaises(ValueError) as caught:
            _parse_scalar("{direction input}")
        self.assertIn("direction input", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
