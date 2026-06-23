from __future__ import annotations

import re
from dataclasses import dataclass

from .equivalence import VerificationState


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
            owns="Build a golden-C oracle harness and high-coverage stimuli before synthesis.",
            inputs=("original C/C++", "must-preserve contract", "argument metadata"),
            outputs=("host testbench", "directed/random stimuli", "coverage plan", "input/output trace schema"),
            stop_condition="Host testbench compiles, feeds identical inputs to golden C and HLS-C, and reaches the configured coverage target.",
        ),
        AgentProcedure(
            name="hlsc_generator_agent",
            role="C-to-HLS-C generator",
            owns="Emit synthesizable HLS-C while preserving functional behavior and the external contract.",
            inputs=("original C/C++", "static diagnostics", "must-preserve contract", "testbench expectations"),
            outputs=("hls_top.hpp", "hls_top.cpp", "transformation ledger", "interface pragma ledger"),
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
    )


def _phase_text(state: VerificationState, phase: str) -> str:
    result = state.phases.get(phase)
    if result is None:
        return ""
    return "\n".join(part for part in (result.summary, result.stdout, result.stderr) if part)


def classify_log_family(phase: str, text: str) -> str:
    lowered = text.lower()
    if "vitis_hls not found" in lowered:
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
