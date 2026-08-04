from __future__ import annotations

import re
from dataclasses import dataclass

from .equivalence import VerificationState
from .hlsc_generator import HLSC_GENERATOR_PROMPT_ID, get_hlsc_generator_contract
from .leveri_testgen import LEVERI_TESTBENCH_POLICY_ID, get_leveri_testbench_contract


@dataclass(frozen=True)
class AgentProcedure:
    name: str
    role: str
    owns: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    stop_condition: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "role": self.role,
            "owns": self.owns,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "stop_condition": self.stop_condition,
        }


@dataclass(frozen=True)
class FailureAnalysis:
    family: str
    owner_agent: str
    next_action: str
    evidence_needed: tuple[str, ...]
    repair_scope: str
    status: str = "needs_action"

    def to_dict(self) -> dict[str, object]:
        return {
            "family": self.family,
            "owner_agent": self.owner_agent,
            "next_action": self.next_action,
            "evidence_needed": list(self.evidence_needed),
            "repair_scope": self.repair_scope,
            "status": self.status,
        }


def _is_valid_relational_klee_metadata(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    names = metadata.get("counterexample_names")
    assumptions = metadata.get("assumptions")
    hashes = metadata.get("artifact_sha256")
    return (
        metadata.get("schema") == "c2hlsc-klee-report-v1"
        and metadata.get("scope") == "golden_hlsc_relational"
        and metadata.get("outcome") == "counterexample"
        and metadata.get("failure_kind") == "relational_counterexample"
        and metadata.get("invocations") == 1
        and type(metadata.get("observable_count")) is int
        and metadata["observable_count"] > 0
        and isinstance(metadata.get("top"), str)
        and bool(metadata["top"])
        and isinstance(names, list)
        and bool(names)
        and all(
            isinstance(name, str)
            and re.fullmatch(
                r"C2HLSC_RELATIONAL_MISMATCH:(?:return|[A-Za-z_][A-Za-z0-9_]*)",
                name,
            )
            for name in names
        )
        and isinstance(assumptions, dict)
        and assumptions.get("pointer_alias_model") == "distinct_pointer_arguments"
        and assumptions.get("hidden_state_model") == "no_mutable_hidden_state"
        and assumptions.get("comparison") == "return_and_complete_pointer_post_state"
        and isinstance(hashes, dict)
        and set(hashes)
        == {
            "input.c",
            "src/hls_top.hpp",
            "src/hls_top.cpp",
            "tb/klee_driver.cpp",
            "tb/leveri_manifest.json",
        }
        and all(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
            for digest in hashes.values()
        )
    )


def multi_agent_procedures() -> tuple[AgentProcedure, ...]:
    return (
        AgentProcedure(
            name="contract_planner",
            role="Planner",
            owns="Extract the top function, interface contract, legal input domain, Vitis part/clock, and unsupported C constructs.",
            inputs=("original C/C++", "user config", "top-function name"),
            outputs=("must-preserve contract", "argument metadata", "static diagnostics"),
            stop_condition="All pointer bounds, scalar ranges, directions, and top-level contracts are explicit or conservatively defaulted.",
        ),
        AgentProcedure(
            name="shift_left_testbench_agent",
            role="Testbench and coverage agent",
            owns=(
                "Build a golden-C oracle harness and high-coverage stimuli before synthesis; "
                f"follow {LEVERI_TESTBENCH_POLICY_ID} for paired trace generation and dual-tier consistency checks."
            ),
            inputs=("original C/C++", "must-preserve contract", "argument metadata"),
            outputs=(
                "host testbench",
                "paired golden/HLS trace testbenches",
                "standalone RTL self-checking testbench",
                "directed/random stimuli",
                "gcov/KLEE coverage artifacts",
                "coverage plan",
                "input/output trace schema",
            ),
            stop_condition="Host testbench compiles, feeds identical inputs to golden C and HLS-C, and reaches the configured coverage target.",
        ),
        AgentProcedure(
            name="hlsc_generator_agent",
            role="C-to-HLS-C generator",
            owns=(
                "Emit synthesizable HLS-C while preserving functional behavior and the external contract; "
                f"follow {HLSC_GENERATOR_PROMPT_ID} for beginner-facing generation and keep testbench generation separate."
            ),
            inputs=("original C/C++", "static diagnostics", "must-preserve contract", "testbench expectations"),
            outputs=(
                "hls_top.hpp",
                "hls_top.cpp",
                "beginner-facing HLS analysis",
                "transformation ledger",
                "interface pragma ledger",
            ),
            stop_condition="Candidate HLS-C is host-compilable and contains only justified, equivalence-preserving transformations.",
        ),
        AgentProcedure(
            name="cosim_operator",
            role="Vitis operator",
            owns="Run the verifier as the loop controller, short-circuiting on the first failing stage.",
            inputs=("HLS project", "run_hls.tcl", "testbench", "toolchain settings"),
            outputs=("software equivalence log", "CSim log", "CSynth log", "CoSim log", "phase status"),
            stop_condition="Compile, CSim, synthesis, and C/RTL CoSim pass, or the earliest failure is classified with compact evidence.",
        ),
        AgentProcedure(
            name="failure_analyst",
            role="Evidence and localization agent",
            owns="Classify failures and compress logs into repair evidence without leaking audit-only artifacts.",
            inputs=("earliest failing stage", "truncated logs", "local code window", "mismatch traces when available"),
            outputs=("failure family", "named symbols", "repair intent", "PMLC evidence for mismatches"),
            stop_condition="The repair agent receives only the current candidate, minimal evidence, and the must-preserve contract.",
        ),
        AgentProcedure(
            name="hlsc_repair_agent",
            role="Minimal patch agent",
            owns="Repair the current HLS-C/testbench candidate using stage-specific evidence.",
            inputs=("current candidate", "failure analysis", "must-preserve contract", "retrieved repair cards"),
            outputs=("patched candidate", "patch rationale", "updated transformation ledger"),
            stop_condition="A minimal patch is produced and the full verifier is rerun from the beginning.",
        ),
        AgentProcedure(
            name="rtl_optimizer_agent",
            role="Post-equivalence optimizer",
            owns="Improve PPA only after functional equivalence is locked.",
            inputs=("four-stage passing HLS-C", "Vitis reports", "optimization policy"),
            outputs=("pragma candidates", "optimized HLS-C", "QoR delta report"),
            stop_condition="Every optimization candidate reruns host equivalence, CSim, synthesis, and CoSim before acceptance.",
        ),
        AgentProcedure(
            name="audit_memory_agent",
            role="Evidence memory agent",
            owns="Persist reproducible artifacts and promote only audited repair successes into retrieval memory.",
            inputs=("logs", "reports", "patches", "failure analyses", "human audit decision"),
            outputs=("audit ledger", "repair-success cards", "retrieval blind-spot notes"),
            stop_condition="No reference HLS, hidden labels, or manual fixes enter prompt-facing memory.",
        ),
        AgentProcedure(
            name="cross_reference_operator",
            role="Dual-generation differential oracle",
            owns="Generate two independent implementations from the same NL spec (no shared "
            "context, different framings), compare them under shared stimulus in isolated "
            "namespaces, and classify each record.",
            inputs=("HLS_NL record (NL spec + top name)", "stimulus seed", "vector count"),
            outputs=("cross_referenced_corpus.jsonl", "needs_review.jsonl", "results.jsonl"),
            stop_condition="Only records whose two arms parse, compile, and agree on every "
            "driven vector enter the cross-verified corpus; the dataset reference is never "
            "shown to either arm.",
        ),
    )


def _phase_text(state: VerificationState, phase: str) -> str:
    result = state.phases.get(phase)
    if result is None:
        return ""
    return "\n".join(part for part in (result.summary, result.stdout, result.stderr) if part)


def classify_log_family(phase: str, text: str) -> str:
    lowered = text.lower()
    if "local-hls backend" in lowered:
        # A local-hls (Bambu) csynth/cosim failure: the backend synthesizes the
        # golden C, so this is a backend/toolchain limitation, not a repairable
        # HLS-C defect. Route to a blocked family so no HLS-C mutation happens.
        return "local_hls_backend"
    if (
        "vitis_hls not found" in lowered
        or "remote vitis unavailable" in lowered
        or ("vitis" in lowered and "not found" in lowered)
    ):
        return "toolchain_unavailable"
    if re.search(r"\b(timeout|timed out|deadlock|stdout-silence)\b", lowered):
        return "timeout_or_deadlock"
    if "mismatch" in lowered or ("expected=" in lowered and "actual=" in lowered):
        return "behavioral_mismatch"
    if re.search(r"\b(interface|axi|axis|ap_ctrl|s_axilite|m_axi|port)\b", lowered):
        return "interface_contract"
    if re.search(r"\b(pointer|alias|array|memory|malloc|calloc|free|bound)\b", lowered):
        return "memory_pointer"
    if re.search(r"\b(bitwidth|bit-width|overflow|truncate|ap_int|ap_uint|float|double)\b", lowered):
        return "numeric_bitwidth"
    if re.search(r"\b(loop|pipeline|unroll|dataflow|ii violation)\b", lowered):
        return "loop_scheduling"
    if re.search(r"\b(not synthesizable|unsupported|cannot synthesize|synthesis failed)\b", lowered):
        return "non_synthesizable_construct"
    if phase in {"software_equivalence", "csim"}:
        return "testbench_or_c_semantics"
    if phase == "csynth":
        return "synthesis_failure"
    if phase == "cosim":
        return "cosim_failure"
    return "unknown"


def classify_failure(
    state: VerificationState,
    run_vitis_requested: bool,
    diagnostics_has_errors: bool = False,
) -> FailureAnalysis:
    if diagnostics_has_errors:
        return FailureAnalysis(
            family="static_source_rejected",
            owner_agent="contract_planner",
            next_action="Refactor or reject unsupported source constructs before HLS-C generation.",
            evidence_needed=("static diagnostics", "unsupported construct list", "top-function contract"),
            repair_scope="original C contract or explicit config metadata",
        )

    software_status = state.status_for("software_equivalence")
    if software_status == "fail":
        text = _phase_text(state, "software_equivalence")
        family = classify_log_family("software_equivalence", text)
        if family == "behavioral_mismatch":
            return FailureAnalysis(
                family="host_behavior_mismatch",
                owner_agent="failure_analyst",
                next_action="Localize the first golden-C versus HLS-C mismatch, then ask the HLS-C repair agent for a minimal semantic patch.",
                evidence_needed=("mismatch test index", "argument/index", "expected/actual value", "seed", "local code slice"),
                repair_scope="generated HLS-C only, unless the mismatch is traced to bad argument metadata",
            )
        return FailureAnalysis(
            family=family,
            owner_agent="shift_left_testbench_agent",
            next_action="Repair the host testbench or metadata until the golden-C oracle and generated HLS-C can be compared.",
            evidence_needed=("compiler stderr", "testbench source", "argument metadata", "golden include wrapper"),
            repair_scope="testbench and config metadata",
        )

    if software_status != "pass":
        return FailureAnalysis(
            family="host_equivalence_not_run",
            owner_agent="cosim_operator",
            next_action="Run host software equivalence before Vitis phases.",
            evidence_needed=("software equivalence phase status",),
            repair_scope="verification scheduling",
        )

    shift_left_trace = state.status_for("shift_left_trace")
    if shift_left_trace == "fail":
        return FailureAnalysis(
            family="shift_left_trace_failure",
            owner_agent="shift_left_testbench_agent",
            next_action="Inspect the first paired golden/HLS trace divergence or harness build failure before synthesis.",
            evidence_needed=("shift_left_trace.log", "golden/HLS trace rows", "argument metadata", "trace schema"),
            repair_scope="generated HLS-C, paired-trace harness, or contract metadata",
        )
    coverage_gcov = state.status_for("coverage_gcov")
    if coverage_gcov == "fail":
        return FailureAnalysis(
            family="concrete_coverage_failure",
            owner_agent="shift_left_testbench_agent",
            next_action="Repair the gcov build/instrumentation harness; do not mutate HLS-C from coverage infrastructure evidence alone.",
            evidence_needed=("coverage_gcov.log", "coverage/gcov_report.json", "compiler and gcov versions"),
            repair_scope="coverage toolchain or generated coverage harness",
            status="blocked",
        )
    symbolic_klee = state.status_for("symbolic_klee")
    if symbolic_klee == "fail":
        result = state.phases.get("symbolic_klee")
        metadata = result.metadata if result is not None else {}
        if _is_valid_relational_klee_metadata(metadata):
            return FailureAnalysis(
                family="klee_relational_counterexample",
                owner_agent="failure_analyst",
                next_action=(
                    "Confirm the named bounded KLEE counterexample, localize the divergent "
                    "observable, then apply a minimal generated HLS-C repair and rerun the full ladder."
                ),
                evidence_needed=(
                    "coverage/klee_report.json",
                    "named relational observable",
                    "matching ktest counterexample",
                    "symbolic driver",
                ),
                repair_scope="generated HLS-C only after the driver and contract are audited",
            )
        return FailureAnalysis(
            family="symbolic_execution_failure",
            owner_agent="shift_left_testbench_agent",
            next_action="Inspect the KLEE report schema, scope, contract, and runner error; unvalidated symbolic evidence must not authorize an HLS-C mutation.",
            evidence_needed=("symbolic_klee.log", "coverage/klee_report.json", "symbolic driver", "contract assumptions"),
            repair_scope="symbolic harness, contract metadata, or toolchain (no automatic HLS-C mutation)",
            status="blocked",
        )

    if not run_vitis_requested:
        return FailureAnalysis(
            family="vitis_not_requested",
            owner_agent="cosim_operator",
            next_action="Enable --run-vitis to turn a host-equivalent HLS-C candidate into RTL and check C/RTL CoSim.",
            evidence_needed=("host equivalence pass log", "Vitis installation path", "part and clock settings"),
            repair_scope="tool invocation config",
            status="blocked",
        )

    for phase in ("csim", "csynth", "cosim"):
        status = state.status_for(phase)
        if status == "pass":
            continue
        text = _phase_text(state, phase)
        family = classify_log_family(phase, text)
        if family == "local_hls_backend":
            return FailureAnalysis(
                family="local_hls_backend",
                owner_agent="cosim_operator",
                next_action="A local-hls (Bambu) csynth/cosim failure reflects the golden-C to RTL path "
                "or a Bambu limitation, not a repairable HLS-C defect; inspect the Bambu log, or use the "
                "Vitis backend for HLS-C-accurate cosim. HLS-C is left untouched.",
                evidence_needed=("bambu log excerpt", "top function", "unsupported construct or mismatch"),
                repair_scope="backend/toolchain (no HLS-C mutation)",
                status="blocked",
            )
        if family == "toolchain_unavailable":
            return FailureAnalysis(
                family=family,
                owner_agent="cosim_operator",
                next_action="Install or activate Vitis HLS on PATH, then rerun the verifier from CSim.",
                evidence_needed=("PATH", "vitis_hls lookup result", "tool version"),
                repair_scope="local toolchain environment",
                status="blocked",
            )
        if phase == "cosim" and family in {"behavioral_mismatch", "cosim_failure", "timeout_or_deadlock"}:
            return FailureAnalysis(
                family="rtl_cosim_mismatch" if family == "behavioral_mismatch" else family,
                owner_agent="failure_analyst",
                next_action="Run PMLC: normalize the mismatch, slice backward from failed outputs, instrument suspect variables, then repair HLS-C.",
                evidence_needed=("first failing cycle", "failed outputs", "AST backward slice", "dual trace around suspect variables"),
                repair_scope="HLS-C semantics, interface timing, or testbench synchronization",
            )
        if phase == "csynth":
            return FailureAnalysis(
                family=family,
                owner_agent="hlsc_repair_agent",
                next_action="Patch non-synthesizable HLS-C while preserving the host-equivalence contract.",
                evidence_needed=("synthesis log excerpt", "local code window", "interface pragma ledger", "argument metadata"),
                repair_scope="generated HLS-C and pragmas",
            )
        return FailureAnalysis(
            family=family,
            owner_agent="hlsc_repair_agent",
            next_action="Repair the current candidate using the earliest failing Vitis stage evidence.",
            evidence_needed=("stage log excerpt", "local code window", "must-preserve contract"),
            repair_scope="generated HLS-C or testbench boundary",
        )

    return FailureAnalysis(
        family="functional_equivalence_signed_off",
        owner_agent="rtl_optimizer_agent",
        next_action="Only now run PPA-oriented pragma/interface optimization, accepting a candidate only after the full verifier passes again.",
        evidence_needed=("host equivalence pass", "CSim pass", "CSynth pass", "CoSim pass", "QoR reports"),
        repair_scope="performance pragmas and architecture choices under full regression",
        status="pass",
    )


def render_procedures_markdown() -> str:
    blocks: list[str] = []
    for idx, procedure in enumerate(multi_agent_procedures(), start=1):
        blocks.append(
            "\n".join(
                [
                    f"{idx}. `{procedure.name}` ({procedure.role})",
                    f"   - Owns: {procedure.owns}",
                    f"   - Outputs: {', '.join(procedure.outputs)}",
                    f"   - Stop: {procedure.stop_condition}",
                ]
            )
        )
    return "\n".join(blocks)


def hlsc_generator_policy() -> dict[str, object]:
    """Return the prompt contract for the HLS-C generator side of AUTO RTL."""

    return get_hlsc_generator_contract().to_dict()


def leveri_testbench_policy() -> dict[str, object]:
    """Return the HLS-LeVeri-inspired prompt contract for the testbench side."""

    return get_leveri_testbench_contract().to_dict()
