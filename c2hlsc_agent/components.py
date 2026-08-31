"""Executable scaffolding for the eight agents declared in :mod:`agent_loop`.

``agent_loop.multi_agent_procedures()`` describes *what* each agent owns. This module
binds each of those declarations to the code that actually implements it today, so the
pipeline stops being an implicit sequence inside ``cli.run_convert`` and becomes a
inspectable, individually runnable set of components:

* :class:`ComponentSpec` — the static contract: stage, real entry points, artifacts read
  and written, the gate that stops the flow, which run-control budgets it spends, and the
  seam where a live model-driven agent would replace the deterministic implementation.
* :class:`Component` — the runtime contract: ``run(context) -> ComponentOutcome``. Every
  adapter here is deliberately thin; the logic stays in ``analyze``/``convert``/
  ``hls_project``/``hls_runner``/``agent_loop``/``hlsc_repair_agent``/``report``/
  ``qor_optimizer``. Swapping a live agent in means replacing one ``run`` body, not
  re-implementing the ladder.
* :func:`run_stages` — a linear driver over those components, used by the tests and by
  anyone who wants a traced start-to-finish walk of the flow.

``cli.run_convert`` remains the production driver: it owns the persistent budgets
(:mod:`run_control`), the bounded repair loop, and the oscillation guards. The component
layer never duplicates that control flow, and it never relaxes an invariant: the verifier
is still the only equivalence gate, and the original C is still never handed to a model as
a reference to copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .agent_loop import AgentProcedure, FailureAnalysis, classify_failure, multi_agent_procedures
from .analyze import AnalysisResult, analyze_source
from .candidates import CANDIDATE_DIRNAME, select_best_candidate
from .config import AgentConfig
from .convert import GeneratedSource, generate_hls_sources
from .equivalence import VerificationState
from .hls_project import ProjectFiles, write_project
from .hls_runner import PHASE_ORDER, earliest_failing_phase, verify_project
from .hlsc_repair_agent import REPAIR_AUDIT_FILENAME, RepairOutcome, load_repair_audit, repair_project
from .llm import LLMClient
from .remote import RemoteVitis
from .report import final_status, write_reports
from .run_control import RUN_LEDGER_FILENAME


STAGE_ORDER = ("plan", "generate", "emit", "verify", "triage", "repair", "record", "optimize")

STAGE_PURPOSE = {
    "plan": "Fix the must-preserve contract before any code is generated.",
    "generate": "Propose a synthesizable HLS-C translation unit (deterministic baseline, optional model candidate).",
    "emit": "Materialize the project: sources, every testbench tier, TCLs, Makefile; refine the stimulus against coverage.",
    "verify": "Run the short-circuiting equivalence ladder; this is the only acceptance oracle.",
    "triage": "Turn the earliest failure into a routed, owner-tagged repair intent.",
    "repair": "Apply a minimal auditable patch, then rerun the ladder from the beginning.",
    "record": "Persist reports, the repair audit, and the bounded-run ledger snapshot.",
    "optimize": "Post-equivalence PPA work; gated on a full ladder pass and re-verified before acceptance.",
}

#: Components ``run_stages`` executes by default: one linear pass from an input C file
#: (or NL-generated reference) to a written report. Repair and optimization are invoked
#: explicitly because they are loop bodies, not single steps.
DEFAULT_PIPELINE = (
    "contract_planner",
    "hlsc_generator_agent",
    "shift_left_testbench_agent",
    "cosim_operator",
    "failure_analyst",
    "audit_memory_agent",
)

#: Outcome statuses that mean "the flow may continue to the next component".
ADVANCING_STATUSES = frozenset({"pass", "applied", "applied_llm", "skipped", "needs_action"})


class ComponentError(RuntimeError):
    """A component was run without the inputs its contract requires."""


@dataclass(frozen=True)
class ComponentSpec:
    """Static contract for one agent component.

    ``implementation`` names the functions that do the work today; ``llm_seam`` names what
    a live model-driven agent would take over. ``status`` is ``deterministic`` when no
    model is involved at all, and ``llm_optional`` when a model may propose and the
    deterministic path is the floor/fallback.
    """

    name: str
    stage: str
    procedure: AgentProcedure
    implementation: tuple[str, ...]
    status: str
    cli: tuple[str, ...]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    gate: str
    budgets: tuple[str, ...]
    invariants: tuple[str, ...]
    llm_seam: str

    @property
    def role(self) -> str:
        return self.procedure.role

    @property
    def owns(self) -> str:
        return self.procedure.owns

    @property
    def stop_condition(self) -> str:
        return self.procedure.stop_condition

    def to_dict(self) -> dict[str, object]:
        payload = self.procedure.to_dict()
        payload.update(
            {
                "stage": self.stage,
                "implementation": list(self.implementation),
                "status": self.status,
                "cli": list(self.cli),
                "reads": list(self.reads),
                "writes": list(self.writes),
                "gate": self.gate,
                "budgets": list(self.budgets),
                "invariants": list(self.invariants),
                "llm_seam": self.llm_seam,
            }
        )
        return payload


@dataclass
class ComponentContext:
    """Everything the components hand to each other, in the order they produce it.

    ``project_dir`` is the generated project root (``--out`` for ``convert``, ``--project``
    for ``repair``/``optimize``). Later components read what earlier ones set; each adapter
    raises :class:`ComponentError` rather than failing with an ``AttributeError`` when a
    predecessor has not run.
    """

    project_dir: Path
    config: AgentConfig
    llm: LLMClient | None = None
    remote: RemoteVitis | None = None
    analysis: AnalysisResult | None = None
    generated: GeneratedSource | None = None
    project: ProjectFiles | None = None
    state: VerificationState | None = None
    decision: FailureAnalysis | None = None
    repairs: list[RepairOutcome] = field(default_factory=list)
    iteration: int = 1
    iterations_used: int = 1
    verbose: bool = False
    run_control: dict[str, Any] | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def require_analysis(self, component: str) -> AnalysisResult:
        if self.analysis is None:
            raise ComponentError(f"{component} needs contract_planner to run first (no analysis)")
        return self.analysis

    def require_generated(self, component: str) -> GeneratedSource:
        if self.generated is None:
            raise ComponentError(f"{component} needs hlsc_generator_agent to run first (no generated source)")
        return self.generated

    def require_state(self, component: str) -> VerificationState:
        if self.state is None:
            raise ComponentError(f"{component} needs cosim_operator to run first (no verification state)")
        return self.state


@dataclass
class ComponentOutcome:
    """What one component did, in the repository's own status vocabulary."""

    name: str
    stage: str
    status: str
    summary: str
    artifacts: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def advances(self) -> bool:
        return self.status in ADVANCING_STATUSES

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "stage": self.stage,
            "status": self.status,
            "summary": self.summary,
            "artifacts": list(self.artifacts),
            "detail": self.detail,
        }


class Component(Protocol):
    """Runtime contract every agent component implements."""

    spec: ComponentSpec

    def run(self, context: ComponentContext) -> ComponentOutcome:  # pragma: no cover - protocol
        ...


def _procedure(name: str) -> AgentProcedure:
    for procedure in multi_agent_procedures():
        if procedure.name == name:
            return procedure
    raise KeyError(f"no declared agent procedure named {name!r}")


def _existing(project_dir: Path, *relatives: str) -> tuple[str, ...]:
    return tuple(rel for rel in relatives if (project_dir / rel).exists())


class ContractPlanner:
    """`contract_planner` — regex static analysis of the top function.

    Produces the must-preserve contract (signature, pointer directions, bounds, ranges,
    interface metadata) plus the unsupported-construct diagnostics that can reject the
    input before a single line of HLS-C is generated.
    """

    spec = ComponentSpec(
        name="contract_planner",
        stage="plan",
        procedure=_procedure("contract_planner"),
        implementation=(
            "c2hlsc_agent.analyze.analyze_source",
            "c2hlsc_agent.analyze._infer_pointer_directions",
            "c2hlsc_agent.analyze._unsupported",
            "c2hlsc_agent.config.load_config",
            "c2hlsc_agent.config.merge_cli_config",
        ),
        status="deterministic",
        cli=("convert", "repair", "optimize"),
        reads=("input.c",),
        writes=(),
        gate=(
            "error-severity diagnostics stop the run before verification unless --keep-going; "
            "missing pointer bounds default to length 16 with a warning"
        ),
        budgets=(),
        invariants=(
            "The analysed C file is the golden oracle; it is never rewritten by any component.",
            "An unbounded pointer parameter must surface a diagnostic, never a silent guess.",
        ),
        llm_seam=(
            "Replace or augment the regex direction/bound inference with a model pass that emits the "
            "same ArgumentConfig shape; the verifier still gates whatever it proposes."
        ),
    )

    def run(self, context: ComponentContext) -> ComponentOutcome:
        config = context.config
        if not config.input_files:
            raise ComponentError("contract_planner needs config.input_files (an input C file or an NL-generated reference)")
        if not config.top:
            raise ComponentError("contract_planner needs config.top (the top function name)")
        analysis = analyze_source(config.input_files[0], config.top, config)
        context.analysis = analysis
        errors = analysis.diagnostics.by_severity("error")
        blocked = bool(errors) and not config.keep_going
        arguments = [
            {
                "name": arg.name,
                "type": arg.c_type,
                "direction": arg.direction,
                "length": arg.length,
                "range": list(arg.scalar_range) if arg.scalar_range else None,
                "interface": arg.interface,
            }
            for arg in analysis.function.args
        ]
        return ComponentOutcome(
            name=self.spec.name,
            stage=self.spec.stage,
            status="fail" if blocked else "pass",
            summary=(
                f"Analysed {config.top}({len(arguments)} arg(s)) from {config.input_files[0].name}; "
                f"{len(errors)} error diagnostic(s), {len(analysis.unsupported_constructs)} unsupported construct(s)."
            ),
            artifacts=(),
            detail={
                "top": config.top,
                "return_type": analysis.function.return_type,
                "arguments": arguments,
                "diagnostics": analysis.diagnostics.to_list(),
                "unsupported_constructs": [item.to_dict() for item in analysis.unsupported_constructs],
                "keep_going": config.keep_going,
            },
        )


class HlscGeneratorAgent:
    """`hlsc_generator_agent` — propose the HLS-C translation unit.

    The conservative deterministic copy of the original body is always built first and is
    the fallback for every failure mode: no LLM configured, backend error, unparsable
    answer, or (with ``--candidates N``) no candidate surviving the structural gate.
    """

    spec = ComponentSpec(
        name="hlsc_generator_agent",
        stage="generate",
        procedure=_procedure("hlsc_generator_agent"),
        implementation=(
            "c2hlsc_agent.convert.generate_hls_sources",
            "c2hlsc_agent.convert._generate_conservative_sources",
            "c2hlsc_agent.convert.generate_hls_source_candidates",
            "c2hlsc_agent.candidates.select_best_candidate",
            "c2hlsc_agent.llm.build_generator_user_prompt",
            "c2hlsc_agent.llm.extract_hls_source",
            "c2hlsc_agent.hlsc_generator.HLSC_GENERATOR_SYSTEM_PROMPT",
        ),
        status="llm_optional",
        cli=("convert",),
        reads=("input.c",),
        writes=(f"{CANDIDATE_DIRNAME}/cand_*/", "candidate_scores.json"),
        gate=(
            "extract_hls_source + is_plausible_translation_unit must accept the model's block "
            "(balanced braces, defines the top); otherwise the conservative copy is used"
        ),
        budgets=("llm_calls",),
        invariants=(
            "The original C is never given to the model as a reference implementation to copy.",
            "Model output is a proposal only; acceptance comes from the verifier, never from the model.",
            "Every fallback reason is recorded in the transformation ledger, never swallowed.",
        ),
        llm_seam=(
            "Already live: swap the prompt/policy in hlsc_generator, or the client in llm.build_llm_client. "
            "Best-of-N scoring lives in candidates.select_best_candidate and uses local host equivalence only."
        ),
    )

    def run(self, context: ComponentContext) -> ComponentOutcome:
        analysis = context.require_analysis(self.spec.name)
        config = context.config
        scores: list[Any] = []
        mode = "deterministic"
        generated: GeneratedSource | None = None
        if context.llm is not None and config.use_llm and config.llm_candidates > 1:
            mode = f"best-of-{config.llm_candidates}"
            generated, scores = select_best_candidate(context.project_dir, analysis, config, context.llm)
            if generated is None:
                generated = generate_hls_sources(analysis, config, llm=None)
                generated.transformations.append(
                    f"Best-of-{config.llm_candidates} produced no usable candidate; "
                    "fell back to the conservative deterministic copy."
                )
                mode = f"best-of-{config.llm_candidates} (all rejected, deterministic fallback)"
        if generated is None:
            if context.llm is not None and config.use_llm:
                mode = "llm"
            generated = generate_hls_sources(analysis, config, llm=context.llm if config.use_llm else None)
        context.generated = generated
        return ComponentOutcome(
            name=self.spec.name,
            stage=self.spec.stage,
            status="pass",
            summary=(
                f"Generated HLS-C for {analysis.function.name} in {mode} mode "
                f"({len(generated.transformations)} ledger entr(ies))."
            ),
            artifacts=_existing(context.project_dir, "candidate_scores.json"),
            detail={
                "mode": mode,
                "generator_prompt_id": generated.generator_prompt_id,
                "transformations": list(generated.transformations),
                "interface_pragmas": list(generated.interface_pragmas),
                "candidate_scores": [score.to_dict() for score in scores],
            },
        )


class ShiftLeftTestbenchAgent:
    """`shift_left_testbench_agent` — emit every testbench tier and the project scaffold.

    ``write_project`` is where this agent's outputs land: the golden-C oracle harness, the
    HLS-LeVeri paired-trace pair with its comparator, the gcov/KLEE coverage hooks, the
    standalone RTL vector/testbench flow, the four TCLs, and the Makefile that drives them.
    The generator's in-memory sources are written in the same call.
    """

    spec = ComponentSpec(
        name="shift_left_testbench_agent",
        stage="emit",
        procedure=_procedure("shift_left_testbench_agent"),
        implementation=(
            "c2hlsc_agent.hls_project.write_project",
            "c2hlsc_agent.testgen.generate_testbench",
            "c2hlsc_agent.leveri_testgen.generate_leveri_testbenches",
            "c2hlsc_agent.verilog_testgen.generate_verilog_testbenches",
            "c2hlsc_agent.stimulus.render_helpers",
            "c2hlsc_agent.coverage_refine.refine_project",
            "c2hlsc_agent.hls_project.render_makefile",
            "c2hlsc_agent.hls_project.render_host_build",
            "c2hlsc_agent.hls_project.render_run_csim",
            "c2hlsc_agent.hls_project.render_run_csynth",
            "c2hlsc_agent.hls_project.render_run_cosim",
        ),
        status="deterministic",
        cli=("convert", "refine"),
        reads=("input.c", "coverage/gcov_report.json", "coverage/klee-out/*.ktest"),
        writes=(
            "src/hls_top.hpp",
            "src/hls_top.cpp",
            "tb/testbench.cpp",
            "tb/leveri_golden_tb.cpp",
            "tb/leveri_hls_tb.cpp",
            "tb/leveri_compare.py",
            "tb/run_gcov.py",
            "tb/klee_driver.cpp",
            "tb/run_klee.py",
            "tb/leveri_manifest.json",
            "tb/stimulus_contract.json",
            "tb/rtl_vectors_tb.cpp",
            "tb/gen_rtl_tb.py",
            "tb/run_rtl_sim.py",
            "tb/rtl_tb_manifest.json",
            "tb/host_build.py",
            "coverage_refinement.json",
            "run_hls.tcl",
            "run_csim.tcl",
            "run_csynth.tcl",
            "run_cosim.tcl",
            "Makefile",
            "run_all.sh",
            "run_all.py",
        ),
        gate=(
            "the oracle testbench must compile and drive golden C and HLS-C with identical stimuli; "
            "refinement stops when the coverage target is met, two rounds fail to improve it, or the "
            "round/vector budget is spent"
        ),
        budgets=(),
        invariants=(
            "The golden side calls the ORIGINAL C, macro-renamed to <top>_ref; it is never the generated code.",
            "Stimulus is seeded (mt19937_64) so a mismatch is reproducible from the report.",
            "Both paired harnesses run ONE schedule; the static tier proves that rather than assuming it.",
            "Refinement only ADDS test cases: it never rewrites src/hls_top.cpp, so a repaired or "
            "optimized design survives a refinement round untouched.",
            "No repair component may ever rewrite a testbench file from model output.",
        ),
        llm_seam=(
            "Coverage-driven refinement is live: KLEE counterexamples become permanent directed cases. "
            "The next increment is model-proposed directed stimuli on top of that; keep the deterministic "
            "harness as the floor so a model can never weaken the oracle."
        ),
    )

    def run(self, context: ComponentContext) -> ComponentOutcome:
        analysis = context.require_analysis(self.spec.name)
        generated = context.require_generated(self.spec.name)
        project = write_project(context.project_dir, analysis, generated, context.config)
        context.project = project
        written = tuple(str(path.relative_to(project.root)) for path in project.generated_files)
        return ComponentOutcome(
            name=self.spec.name,
            stage=self.spec.stage,
            status="pass",
            summary=f"Wrote {len(written)} project file(s) under {project.root}.",
            artifacts=written,
            detail={
                "root": str(project.root),
                "num_tests": context.config.num_tests,
                "directed_tests": list(context.config.directed_tests),
                "seed": context.config.seed,
            },
        )


class CosimOperator:
    """`cosim_operator` — run the short-circuiting equivalence ladder.

    ``software_equivalence`` (``make test``) always runs. The Vitis phases run only with
    ``--run-vitis`` (or an ``--vitis-ssh`` host, which implies it), locally or over SSH,
    and each failure blocks everything after it instead of being skipped silently.
    """

    spec = ComponentSpec(
        name="cosim_operator",
        stage="verify",
        procedure=_procedure("cosim_operator"),
        implementation=(
            "c2hlsc_agent.hls_runner.verify_project",
            "c2hlsc_agent.hls_runner.run_software_equivalence",
            "c2hlsc_agent.hls_runner.run_trace_consistency",
            "c2hlsc_agent.hls_runner.run_vitis",
            "c2hlsc_agent.hls_runner._gate_cosim_on_log",
            "c2hlsc_agent.cosim_verdict.evaluate_cosim_verdict",
            "c2hlsc_agent.equivalence.run_command",
            "c2hlsc_agent.remote.RemoteVitis.run_phase",
        ),
        status="deterministic",
        cli=("convert", "optimize"),
        reads=(
            "Makefile",
            "run_csim.tcl",
            "run_csynth.tcl",
            "run_cosim.tcl",
            "src/hls_top.cpp",
            "tb/testbench.cpp",
            "tb/leveri_golden_tb.cpp",
            "tb/leveri_hls_tb.cpp",
        ),
        writes=(
            "software_equivalence.log",
            "trace_consistency.log",
            "csim.log",
            "csynth.log",
            "cosim.log",
            "c2hlsc_project/",
        ),
        gate=(
            "software_equivalence -> trace_consistency -> csim -> csynth -> cosim, short-circuited: the "
            "first non-pass phase blocks the rest; a CoSim log failure marker downgrades a zero exit code to fail"
        ),
        budgets=("vitis_runs", "wall_seconds"),
        invariants=(
            "A skipped or unrequested phase is never reported as pass.",
            "The shift-left trace tier runs on every verification, so a paired-trace divergence "
            "fails the run instead of sitting in an advisory report nobody reads.",
            "Vitis exiting 0 is not sufficient for CoSim: the log verdict is checked too.",
            "A remote sync failure is reported as toolchain_unavailable (blocked), never as a code defect.",
        ),
        llm_seam=(
            "None by design. The operator is the acceptance oracle; keeping it deterministic is what makes "
            "every model-proposed change checkable."
        ),
    )

    def run(self, context: ComponentContext) -> ComponentOutcome:
        analysis = context.require_analysis(self.spec.name)
        config = context.config
        state = verify_project(
            context.project_dir,
            config.run_vitis,
            verbose=context.verbose,
            remote=context.remote,
        )
        context.state = state
        status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
        phases = {name: result.status for name, result in state.phases.items()}
        return ComponentOutcome(
            name=self.spec.name,
            stage=self.spec.stage,
            status=status,
            summary=(
                "Ladder: "
                + ", ".join(f"{phase}={phases.get(phase, 'skipped')}" for phase in PHASE_ORDER)
                + f"; {len(state.mismatches)} parsed mismatch(es)."
            ),
            artifacts=_existing(context.project_dir, *(f"{phase}.log" for phase in PHASE_ORDER)),
            detail={
                "phases": {name: result.to_dict() for name, result in state.phases.items()},
                "mismatches": [mismatch.to_dict() for mismatch in state.mismatches],
                "earliest_failing_phase": earliest_failing_phase(state, config.run_vitis),
                "run_vitis": config.run_vitis,
                "remote": context.remote.host if context.remote is not None else None,
            },
        )


class FailureAnalyst:
    """`failure_analyst` — route the earliest failure to an owner and a next action.

    The routing table is live: regex triage of the failing phase's text produces a failure
    family, the owning agent, the evidence the repair needs, and the scope it may touch.
    A pass routes to ``rtl_optimizer_agent``; a toolchain outage routes to
    ``cosim_operator`` as ``blocked`` so no component mutates correct source.
    """

    spec = ComponentSpec(
        name="failure_analyst",
        stage="triage",
        procedure=_procedure("failure_analyst"),
        implementation=(
            "c2hlsc_agent.agent_loop.classify_failure",
            "c2hlsc_agent.agent_loop.classify_log_family",
            "c2hlsc_agent.hls_runner.earliest_failing_phase",
            "c2hlsc_agent.equivalence.parse_mismatches",
        ),
        status="deterministic",
        cli=("convert", "repair"),
        reads=("software_equivalence.log", "csim.log", "csynth.log", "cosim.log"),
        writes=(),
        gate="Routing only; it never edits the project. Its verdict decides which component runs next.",
        budgets=(),
        invariants=(
            "Full logs stay audit-only; only compact excerpts reach a repair prompt.",
            "A blocked family (missing toolchain) must not be escalated into a source repair.",
        ),
        llm_seam=(
            "The cleanest first live agent: replace the regex triage with a model that returns the same "
            "FailureAnalysis dataclass, and add PMLC slicing for CoSim mismatches. Zero risk — the output "
            "shape is already validated and the verifier still decides."
        ),
    )

    def run(self, context: ComponentContext) -> ComponentOutcome:
        analysis = context.require_analysis(self.spec.name)
        state = context.require_state(self.spec.name)
        decision = classify_failure(state, context.config.run_vitis, analysis.diagnostics.has_errors)
        context.decision = decision
        phase = earliest_failing_phase(state, context.config.run_vitis)
        return ComponentOutcome(
            name=self.spec.name,
            stage=self.spec.stage,
            status=decision.status,
            summary=f"{decision.family} -> {decision.owner_agent} (earliest failing phase: {phase or 'none'}).",
            artifacts=(),
            detail={
                "decision": decision.to_dict(),
                "earliest_failing_phase": phase,
                "mismatches": [mismatch.to_dict() for mismatch in state.mismatches],
            },
        )


class HlscRepairAgentComponent:
    """`hlsc_repair_agent` — one minimal, audited patch per iteration.

    Four deterministic mechanical repairs are tried first (missing standard includes, C99
    ``restrict`` for C++, pulling in helper functions the original C defines, stripping
    interface pragmas Vitis rejected). Only if none applies — and only with the LLM path
    enabled — does a model rewrite ``src/hls_top.cpp``, structurally validated and checked
    against every previously visited source hash before it is written.
    """

    spec = ComponentSpec(
        name="hlsc_repair_agent",
        stage="repair",
        procedure=_procedure("hlsc_repair_agent"),
        implementation=(
            "c2hlsc_agent.hlsc_repair_agent.repair_project",
            "c2hlsc_agent.hlsc_repair_agent._repair_missing_standard_includes",
            "c2hlsc_agent.hlsc_repair_agent._repair_restrict_for_cpp",
            "c2hlsc_agent.hlsc_repair_agent._repair_missing_original_support",
            "c2hlsc_agent.hlsc_repair_agent._repair_invalid_interface_pragmas",
            "c2hlsc_agent.hlsc_repair_agent._llm_repair",
            "c2hlsc_agent.llm.build_repair_prompt",
        ),
        status="llm_optional",
        cli=("convert", "repair"),
        reads=("src/hls_top.cpp", "src/hls_top.hpp", REPAIR_AUDIT_FILENAME),
        writes=("src/hls_top.cpp", "src/hls_top.hpp", REPAIR_AUDIT_FILENAME),
        gate=(
            "requires --auto-repair on convert; a repair that reproduces a previously seen project "
            "signature stops the loop (oscillation guard), and a no-change repair ends it"
        ),
        budgets=("attempts", "llm_calls", "wall_seconds"),
        invariants=(
            "Only src/hls_top.cpp is model-writable. input.c and every tb/ file are off limits.",
            "Each change records a before/after sha256 and a unified diff in repair_audit.json.",
            "After any patch the verifier reruns from software_equivalence, never from the failing phase.",
        ),
        llm_seam=(
            "Already live. The next increment is evidence localization: feed PMLC slices from failure_analyst "
            "instead of a raw log tail, and retrieve audited repair cards from audit_memory_agent."
        ),
    )

    def run(self, context: ComponentContext) -> ComponentOutcome:
        analysis = context.require_analysis(self.spec.name)
        state = context.require_state(self.spec.name)
        outcome = repair_project(
            context.project_dir,
            analysis,
            context.config,
            state,
            context.iteration,
            llm=context.llm if context.config.use_llm else None,
        )
        context.repairs.append(outcome)
        return ComponentOutcome(
            name=self.spec.name,
            stage=self.spec.stage,
            status=outcome.status,
            summary=outcome.summary,
            artifacts=_existing(context.project_dir, REPAIR_AUDIT_FILENAME) + tuple(outcome.target_files),
            detail=outcome.to_dict(),
        )


class AuditMemoryAgent:
    """`audit_memory_agent` — persist the evidence chain.

    Writes ``conversion_report.md``/``.json`` (contract, ledgers, phase results, the
    ``failure_analyst`` verdict, the repair audit table, mismatches, and the bounded-run
    snapshot) and leaves ``repair_audit.json`` and ``run_ledger.jsonl`` in place as the
    reproducible record of what happened.
    """

    spec = ComponentSpec(
        name="audit_memory_agent",
        stage="record",
        procedure=_procedure("audit_memory_agent"),
        implementation=(
            "c2hlsc_agent.report.write_reports",
            "c2hlsc_agent.report.final_status",
            "c2hlsc_agent.hlsc_repair_agent.load_repair_audit",
            "c2hlsc_agent.run_control.RunLedger",
            "c2hlsc_agent.run_control.RunController.snapshot",
        ),
        status="deterministic",
        cli=("convert", "status"),
        reads=(REPAIR_AUDIT_FILENAME, RUN_LEDGER_FILENAME),
        writes=("conversion_report.md", "conversion_report.json", RUN_LEDGER_FILENAME),
        gate="Always runs, including on failure: a report that hides a failed phase is a bug.",
        budgets=(),
        invariants=(
            "Prompts, model responses, API keys, and endpoints are never written to the ledger.",
            "run_control status (running/passed/failed/blocked/exhausted/cancelled) is reported separately "
            "from the verification status and neither may be described as the other.",
        ),
        llm_seam=(
            "Promote only audited failure-to-pass chains from repair_audit.json into retrieval memory, "
            "keyed by failing stage + failure family + named symbols. Reference HLS and hidden labels "
            "must never enter prompt-facing memory."
        ),
    )

    def run(self, context: ComponentContext) -> ComponentOutcome:
        analysis = context.require_analysis(self.spec.name)
        generated = context.require_generated(self.spec.name)
        if context.project is None:
            raise ComponentError("audit_memory_agent needs shift_left_testbench_agent to run first (no project)")
        state = context.state if context.state is not None else VerificationState()
        write_reports(
            context.project,
            analysis,
            generated,
            context.config,
            state,
            context.iterations_used,
            context.repairs,
            run_control=context.run_control,
        )
        status = final_status(state, context.config.run_vitis, analysis.diagnostics.has_errors)
        audit = load_repair_audit(context.project_dir)
        return ComponentOutcome(
            name=self.spec.name,
            stage=self.spec.stage,
            status="pass",
            summary=(
                f"Wrote conversion_report.md/.json (verification status: {status}, "
                f"{len(audit)} audited repair(s), {context.iterations_used} attempt(s))."
            ),
            artifacts=_existing(
                context.project_dir,
                "conversion_report.md",
                "conversion_report.json",
                REPAIR_AUDIT_FILENAME,
                RUN_LEDGER_FILENAME,
            ),
            detail={
                "verification_status": status,
                "iterations": context.iterations_used,
                "repairs": [item.to_dict() for item in audit],
                "run_control": context.run_control,
            },
        )


class RtlOptimizerAgent:
    """`rtl_optimizer_agent` — post-equivalence PPA, gated and re-verified.

    Disabled until the full ladder passes. Candidates (one deterministic pipeline-pragma
    proposal plus N model proposals) are staged in scratch copies, filtered by local host
    equivalence, scored on a fresh CSynth report, and only the winner is promoted into the
    project — after which the FULL ladder reruns and a failure rolls the source back.
    """

    spec = ComponentSpec(
        name="rtl_optimizer_agent",
        stage="optimize",
        procedure=_procedure("rtl_optimizer_agent"),
        implementation=(
            "c2hlsc_agent.qor_optimizer.optimize_project",
            "c2hlsc_agent.qor_optimizer._pipeline_innermost_loops",
            "c2hlsc_agent.qor_optimizer._llm_candidate_source",
            "c2hlsc_agent.qor.parse_csynth_xml",
            "c2hlsc_agent.qor.evaluate_targets",
            "c2hlsc_agent.qor.objective_score",
            "c2hlsc_agent.local_ppa.run_local_ppa",
        ),
        status="llm_optional",
        cli=("optimize",),
        reads=("src/hls_top.cpp", "c2hlsc_project/solution1/syn/report/csynth.xml"),
        writes=(
            "qor_report.json",
            "qor_report.md",
            "qor_table.tex",
            "src/hls_top.cpp.pre_qor",
            ".qor/cand_*/",
        ),
        gate=(
            "runs only on a project that passed the ladder; a promoted winner must pass host equivalence, "
            "CSim, CSynth and CoSim again or the pre-QoR source is restored and the stale report deleted"
        ),
        budgets=("llm_calls", "vitis_runs", "wall_seconds"),
        invariants=(
            "Optimization never becomes its own oracle: acceptance is a full re-verification.",
            "No latency/area/timing/power claim without a fresh report from a named tool, part and clock.",
            "A toolchain outage is reported as an infrastructure problem, not as a QoR verdict.",
        ),
        llm_seam=(
            "Already live for candidate proposal. Extensions: one optimization family per round "
            "(pipeline, unroll, array partition, dataflow, interface, bitwidth) and a candidate queue "
            "with explicit rollback records."
        ),
    )

    def run(self, context: ComponentContext) -> ComponentOutcome:
        from .qor_optimizer import optimize_project

        analysis = context.require_analysis(self.spec.name)
        state = context.state
        if state is not None:
            # The gate is the FULL ladder, not final_status: a host-only run reports "pass"
            # without ever having synthesized or co-simulated, and optimizing that would let
            # PPA edits hide semantic bugs the RTL check has not looked for yet.
            phases = {phase: state.status_for(phase) for phase in PHASE_ORDER}
            if any(status != "pass" for status in phases.values()):
                return ComponentOutcome(
                    name=self.spec.name,
                    stage=self.spec.stage,
                    status="blocked",
                    summary=(
                        "Functional equivalence is not signed off across the full ladder "
                        f"({', '.join(f'{name}={status}' for name, status in phases.items())}); "
                        "the optimizer stays disabled."
                    ),
                    artifacts=(),
                    detail={"phases": phases},
                )
        options = dict(context.options.get("optimize", {}))
        try:
            outcome = optimize_project(
                context.project_dir,
                analysis,
                context.config,
                context.llm if context.config.use_llm else None,
                context.remote,
                verbose=context.verbose,
                **options,
            )
        except RuntimeError as exc:
            # No baseline synthesis report and no way to make one (usually no Vitis on PATH).
            # That is an infrastructure outage, not a QoR verdict — say so instead of failing.
            return ComponentOutcome(
                name=self.spec.name,
                stage=self.spec.stage,
                status="blocked",
                summary=f"QoR optimization could not run: {exc}",
                artifacts=(),
                detail={"error": str(exc)},
            )
        return ComponentOutcome(
            name=self.spec.name,
            stage=self.spec.stage,
            status="pass" if outcome.accepted else ("fail" if outcome.rolled_back else "no_change"),
            summary=outcome.summary,
            artifacts=_existing(context.project_dir, "qor_report.json", "qor_report.md", "qor_table.tex"),
            detail=outcome.to_dict(),
        )


_COMPONENT_CLASSES = (
    ContractPlanner,
    HlscGeneratorAgent,
    ShiftLeftTestbenchAgent,
    CosimOperator,
    FailureAnalyst,
    HlscRepairAgentComponent,
    AuditMemoryAgent,
    RtlOptimizerAgent,
)


def component_registry() -> tuple[Component, ...]:
    """Every agent component, in stage order."""

    components = [cls() for cls in _COMPONENT_CLASSES]
    return tuple(sorted(components, key=lambda item: STAGE_ORDER.index(item.spec.stage)))


def component_specs() -> tuple[ComponentSpec, ...]:
    return tuple(component.spec for component in component_registry())


def get_component(name: str) -> Component:
    for component in component_registry():
        if component.spec.name == name:
            return component
    raise KeyError(f"no component named {name!r}")


def describe_components() -> list[dict[str, object]]:
    return [spec.to_dict() for spec in component_specs()]


def workflow_stages() -> list[dict[str, object]]:
    """The stage graph: what each stage is for and which components sit in it."""

    stages: list[dict[str, object]] = []
    for stage in STAGE_ORDER:
        members = [spec.name for spec in component_specs() if spec.stage == stage]
        stages.append({"stage": stage, "purpose": STAGE_PURPOSE[stage], "components": members})
    return stages


def run_stages(
    context: ComponentContext,
    stages: tuple[str, ...] = DEFAULT_PIPELINE,
    stop_on_non_advancing: bool = True,
) -> list[ComponentOutcome]:
    """Run named components in order and return their outcomes.

    This is the traced, linear walk of the flow — useful for inspection, tests, and as the
    base for swapping a live agent into one stage. It deliberately does NOT reimplement
    ``cli.run_convert``: there is no budget reservation, no repair loop, and no oscillation
    guard here. Use ``convert`` for a real bounded run.
    """

    outcomes: list[ComponentOutcome] = []
    for name in stages:
        component = get_component(name)
        outcome = component.run(context)
        outcomes.append(outcome)
        if stop_on_non_advancing and not outcome.advances:
            # Everything downstream would report on a state that never happened. The record
            # stage is the one exception: a failed run must still write its report — but only
            # once there is a project to report on (a rejected contract never emits one).
            tail = [item for item in stages[stages.index(name) + 1 :] if item == "audit_memory_agent"]
            if tail and context.project is not None and context.generated is not None:
                outcomes.append(get_component("audit_memory_agent").run(context))
            elif tail:
                outcomes.append(
                    ComponentOutcome(
                        name="audit_memory_agent",
                        stage="record",
                        status="skipped",
                        summary=f"{name} stopped the flow before a project was emitted; nothing to report on.",
                    )
                )
            break
    return outcomes


def render_components_markdown() -> str:
    """Full component reference, rendered from the registry itself."""

    lines: list[str] = [
        "# Agent components",
        "",
        "Generated from `c2hlsc_agent.components`. Regenerate with:",
        "",
        "```text",
        "python -m c2hlsc_agent components --markdown > docs/agent_components.md",
        "```",
        "",
        "Each component binds one declared agent from `agent_loop.multi_agent_procedures()`",
        "to the code that implements it today. `status` is `deterministic` when no model is",
        "involved, and `llm_optional` when a model may propose and the deterministic path is",
        "the floor and the fallback.",
        "",
        "## Stages",
        "",
        "| # | Stage | Purpose | Components |",
        "| --- | --- | --- | --- |",
    ]
    for index, stage in enumerate(workflow_stages(), start=1):
        members = ", ".join(f"`{name}`" for name in stage["components"]) or "_none_"
        lines.append(f"| {index} | `{stage['stage']}` | {stage['purpose']} | {members} |")
    lines.extend(
        [
            "",
            "## Components at a glance",
            "",
            "| Component | Stage | Status | Driven by | Gate |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for spec in component_specs():
        cli = ", ".join(f"`{item}`" for item in spec.cli) or "_library only_"
        lines.append(f"| `{spec.name}` | `{spec.stage}` | `{spec.status}` | {cli} | {spec.gate} |")
    lines.append("")
    for spec in component_specs():
        lines.extend(_render_component_section(spec))
    return "\n".join(lines).rstrip() + "\n"


def _render_component_section(spec: ComponentSpec) -> list[str]:
    def bullets(title: str, values: tuple[str, ...], code: bool = True) -> list[str]:
        if not values:
            return [f"- **{title}:** _none_"]
        rendered = ", ".join(f"`{value}`" if code else value for value in values)
        return [f"- **{title}:** {rendered}"]

    lines = [
        f"## `{spec.name}`",
        "",
        f"- **Role:** {spec.role}",
        f"- **Stage:** `{spec.stage}` — {STAGE_PURPOSE[spec.stage]}",
        f"- **Status:** `{spec.status}`",
        f"- **Owns:** {spec.owns}",
    ]
    lines.extend(bullets("Inputs", spec.procedure.inputs, code=False))
    lines.extend(bullets("Outputs", spec.procedure.outputs, code=False))
    lines.extend(bullets("Implemented by", spec.implementation))
    lines.extend(bullets("Driven by CLI", spec.cli))
    lines.extend(bullets("Reads", spec.reads))
    lines.extend(bullets("Writes", spec.writes))
    lines.extend(bullets("Budgets", spec.budgets))
    lines.extend(
        [
            f"- **Gate:** {spec.gate}",
            f"- **Stop condition:** {spec.stop_condition}",
            f"- **LLM seam:** {spec.llm_seam}",
            "- **Invariants:**",
        ]
    )
    lines.extend(f"  - {item}" for item in spec.invariants)
    lines.append("")
    return lines
