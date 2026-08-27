"""Integrity and behaviour tests for the agent component scaffold."""

from __future__ import annotations

import importlib
import shutil
import tempfile
import unittest
from pathlib import Path

from c2hlsc_agent.agent_loop import multi_agent_procedures
from c2hlsc_agent.components import (
    ADVANCING_STATUSES,
    DEFAULT_PIPELINE,
    STAGE_ORDER,
    STAGE_PURPOSE,
    ComponentContext,
    ComponentError,
    component_specs,
    describe_components,
    get_component,
    render_components_markdown,
    run_stages,
    workflow_stages,
)
from c2hlsc_agent.config import load_config
from c2hlsc_agent.equivalence import PhaseResult, VerificationState


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "vector_add"
HOST_TOOLS = shutil.which("g++") is not None and shutil.which("make") is not None


def _resolves(dotted: str) -> bool:
    """True when ``pkg.module.attr`` (or ``pkg.module.Class.method``) exists."""

    parts = dotted.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for attribute in parts[split:]:
            obj = getattr(obj, attribute, None)
            if obj is None:
                return False
        return True
    return False


def _context(project_dir: Path) -> ComponentContext:
    return ComponentContext(project_dir=project_dir, config=load_config(EXAMPLE / "config.yaml"))


class ComponentSpecTests(unittest.TestCase):
    def test_every_declared_procedure_has_exactly_one_component(self) -> None:
        declared = sorted(procedure.name for procedure in multi_agent_procedures())
        bound = sorted(spec.name for spec in component_specs())
        self.assertEqual(declared, bound)

    def test_specs_are_well_formed(self) -> None:
        for spec in component_specs():
            with self.subTest(component=spec.name):
                self.assertIn(spec.stage, STAGE_ORDER)
                self.assertIn(spec.status, {"deterministic", "llm_optional"})
                self.assertTrue(spec.implementation, "a component must name the code that implements it")
                self.assertTrue(spec.gate.strip())
                self.assertTrue(spec.llm_seam.strip())
                self.assertTrue(spec.invariants, "a component must state what it may never do")
                self.assertEqual(spec.name, spec.procedure.name)

    def test_implementation_targets_resolve(self) -> None:
        """Every dotted path in a spec must exist, so the scaffold cannot drift from the code."""

        for spec in component_specs():
            for target in spec.implementation:
                with self.subTest(component=spec.name, target=target):
                    self.assertTrue(
                        _resolves(target),
                        f"{target} is named by {spec.name} but does not exist",
                    )

    def test_registry_is_in_stage_order(self) -> None:
        stages = [spec.stage for spec in component_specs()]
        self.assertEqual(stages, sorted(stages, key=STAGE_ORDER.index))

    def test_stage_graph_covers_every_component(self) -> None:
        listed = [name for stage in workflow_stages() for name in stage["components"]]
        self.assertEqual(sorted(listed), sorted(spec.name for spec in component_specs()))
        for stage in workflow_stages():
            self.assertEqual(stage["purpose"], STAGE_PURPOSE[stage["stage"]])

    def test_default_pipeline_is_a_prefix_walk_of_the_registry(self) -> None:
        known = [spec.name for spec in component_specs()]
        self.assertTrue(set(DEFAULT_PIPELINE).issubset(known))
        order = [known.index(name) for name in DEFAULT_PIPELINE]
        self.assertEqual(order, sorted(order))
        # Repair and optimization are loop bodies / a separate command, not linear steps.
        self.assertNotIn("hlsc_repair_agent", DEFAULT_PIPELINE)
        self.assertNotIn("rtl_optimizer_agent", DEFAULT_PIPELINE)

    def test_describe_components_is_json_ready(self) -> None:
        import json

        json.dumps(describe_components())

    def test_generated_reference_doc_is_in_sync(self) -> None:
        doc = ROOT / "docs" / "agent_components.md"
        self.assertTrue(doc.exists(), "docs/agent_components.md is generated from the registry")
        self.assertEqual(
            doc.read_text(encoding="utf-8"),
            render_components_markdown(),
            "regenerate with: python -m c2hlsc_agent components --markdown > docs/agent_components.md",
        )

    def test_unknown_component_raises(self) -> None:
        with self.assertRaises(KeyError):
            get_component("no_such_agent")


class ComponentContractTests(unittest.TestCase):
    def test_components_require_their_predecessors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(Path(tmp) / "project")
            for name in ("hlsc_generator_agent", "shift_left_testbench_agent", "failure_analyst"):
                with self.subTest(component=name):
                    with self.assertRaises(ComponentError):
                        get_component(name).run(context)

    def test_contract_planner_needs_a_top(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(Path(tmp) / "project")
            context.config.top = None
            with self.assertRaises(ComponentError):
                get_component("contract_planner").run(context)

    def test_failure_analyst_routes_a_synthetic_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(Path(tmp) / "project")
            get_component("contract_planner").run(context)
            context.config.run_vitis = True
            state = VerificationState()
            state.add_phase(PhaseResult("software_equivalence", "pass"))
            state.add_phase(PhaseResult("trace_consistency", "pass"))
            state.add_phase(PhaseResult("csim", "pass"))
            state.add_phase(
                PhaseResult("csynth", "fail", stdout="ERROR: unsupported pointer aliasing and memory bound")
            )
            state.add_phase(PhaseResult("cosim", "blocked", summary="csynth failed"))
            context.state = state
            outcome = get_component("failure_analyst").run(context)
            self.assertEqual(outcome.status, "needs_action")
            self.assertEqual(outcome.detail["decision"]["owner_agent"], "hlsc_repair_agent")
            self.assertEqual(outcome.detail["earliest_failing_phase"], "csynth")

    def test_optimizer_is_blocked_until_the_full_ladder_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(Path(tmp) / "project")
            get_component("contract_planner").run(context)
            state = VerificationState()
            state.add_phase(PhaseResult("software_equivalence", "pass"))
            context.state = state
            outcome = get_component("rtl_optimizer_agent").run(context)
            self.assertEqual(outcome.status, "blocked")
            self.assertIn("full ladder", outcome.summary)

    def test_advancing_statuses_do_not_include_failures(self) -> None:
        self.assertNotIn("fail", ADVANCING_STATUSES)
        self.assertNotIn("blocked", ADVANCING_STATUSES)


@unittest.skipUnless(HOST_TOOLS, "host equivalence needs g++ and make")
class ComponentPipelineTests(unittest.TestCase):
    def test_run_stages_converts_the_vector_add_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            context = _context(project)
            outcomes = run_stages(context)
            by_name = {outcome.name: outcome for outcome in outcomes}

            self.assertEqual(by_name["contract_planner"].status, "pass")
            self.assertEqual(by_name["hlsc_generator_agent"].status, "pass")
            self.assertEqual(by_name["shift_left_testbench_agent"].status, "pass")
            self.assertEqual(by_name["cosim_operator"].status, "pass")
            self.assertEqual(by_name["audit_memory_agent"].status, "pass")

            # The contract survived analysis.
            directions = {
                arg["name"]: arg["direction"] for arg in by_name["contract_planner"].detail["arguments"]
            }
            self.assertEqual(directions["a"], "input")
            self.assertEqual(directions["out"], "output")

            # Host equivalence really ran; Vitis was not requested and must not read as pass.
            phases = by_name["cosim_operator"].detail["phases"]
            self.assertEqual(phases["software_equivalence"]["status"], "pass")
            self.assertEqual(phases["csim"]["status"], "skipped")

            for artifact in (
                "input.c",
                "src/hls_top.cpp",
                "tb/testbench.cpp",
                "tb/leveri_compare.py",
                "tb/rtl_tb_manifest.json",
                "run_cosim.tcl",
                "Makefile",
                "conversion_report.md",
                "conversion_report.json",
            ):
                self.assertTrue((project / artifact).exists(), f"missing {artifact}")

    def test_a_failing_ladder_still_writes_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            context = _context(project)
            run_stages(context, ("contract_planner", "hlsc_generator_agent", "shift_left_testbench_agent"))

            # Break the generated design only — input.c stays the golden oracle.
            source = project / "src" / "hls_top.cpp"
            source.write_text(
                source.read_text(encoding="utf-8").replace("a[i] + b[i]", "a[i] - b[i]"),
                encoding="utf-8",
            )

            outcomes = run_stages(context, ("cosim_operator", "failure_analyst", "audit_memory_agent"))
            by_name = {outcome.name: outcome for outcome in outcomes}
            self.assertEqual(by_name["cosim_operator"].status, "fail")
            self.assertTrue(by_name["cosim_operator"].detail["mismatches"], "the oracle must catch the wrong result")
            self.assertEqual(by_name["audit_memory_agent"].status, "pass")
            self.assertIn("fail", (project / "conversion_report.md").read_text(encoding="utf-8"))

    def test_repair_component_writes_an_audit_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            context = _context(project)
            run_stages(context, ("contract_planner", "hlsc_generator_agent", "shift_left_testbench_agent"))
            context.config.run_vitis = True
            state = VerificationState()
            state.add_phase(PhaseResult("software_equivalence", "pass"))
            state.add_phase(PhaseResult("trace_consistency", "pass"))
            state.add_phase(PhaseResult("csim", "pass"))
            state.add_phase(
                PhaseResult(
                    "csynth",
                    "fail",
                    stdout="error: use of undeclared identifier 'memcpy'",
                )
            )
            state.add_phase(PhaseResult("cosim", "blocked", summary="csynth failed"))
            context.state = state
            outcome = get_component("hlsc_repair_agent").run(context)
            self.assertEqual(outcome.status, "applied")
            self.assertIn("repair_audit.json", outcome.artifacts)
            self.assertTrue((project / "repair_audit.json").exists())
            self.assertEqual(context.repairs[-1].owner_agent, "hlsc_repair_agent")


if __name__ == "__main__":
    unittest.main()
