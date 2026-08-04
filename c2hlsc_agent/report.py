from __future__ import annotations

import json
import re
from pathlib import Path

from .agent_loop import classify_failure
from .analyze import AnalysisResult
from .config import AgentConfig
from .convert import GeneratedSource
from .equivalence import VerificationState
from .hlsc_repair_agent import REPAIR_AUDIT_FILENAME, RepairOutcome
from .hls_project import ProjectFiles
from .hls_runner import SHIFT_LEFT_PHASES
from .knowledge_graph import FILENAME as KNOWLEDGE_GRAPH_FILENAME
from .knowledge_graph import refresh_knowledge_graph, write_knowledge_graph
from .leveri_testgen import LEVERI_TESTBENCH_POLICY_ID


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_None_\n"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(out) + "\n"


def final_status(state: VerificationState, run_vitis: bool, diagnostics_has_errors: bool) -> str:
    if diagnostics_has_errors:
        return "fail"
    required = ["software_equivalence"]
    if run_vitis:
        required.extend(["csim", "csynth", "cosim"])
    if any(state.status_for(phase) == "fail" for phase in SHIFT_LEFT_PHASES):
        return "fail"
    if all(state.status_for(phase) == "pass" for phase in required):
        # The PPA workflow-criteria phase gates only when it actually reached a verdict.
        # A "skipped" ppa (tools/RTL missing, no hard criteria) is recorded for
        # transparency but does not fail the run; a "fail" (a declared criterion unmet or
        # the gate-level sim failed) does. This keeps the headline status and the convert
        # exit code in agreement.
        return "fail" if state.status_for("ppa") == "fail" else "pass"
    return "fail"


def write_reports(
    project: ProjectFiles,
    analysis: AnalysisResult,
    generated: GeneratedSource,
    config: AgentConfig,
    state: VerificationState,
    iterations: int,
    repairs: list[RepairOutcome] | None = None,
) -> None:
    repairs = repairs or []
    status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
    fn = analysis.function
    arg_rows = [[arg.name, arg.c_type, arg.direction, str(arg.length or ""), arg.interface or config.interface_mode] for arg in fn.args]
    type_rows = [[row["name"], row["original"], row["generated"]] for row in analysis.type_mappings]
    pragma_rows = [[row["argument"], row["pragma"], row["reason"]] for row in generated.interface_pragmas]
    unsupported_rows = [[d.severity, d.code, d.message, d.suggestion or ""] for d in analysis.unsupported_constructs]
    generated_files = [str(path.relative_to(project.root)) for path in project.generated_files]
    agent_decision = classify_failure(state, config.run_vitis, analysis.diagnostics.has_errors)
    klee_result = state.phases.get("symbolic_klee")
    klee_metadata = dict(klee_result.metadata) if klee_result is not None else {}
    raw_counterexample_names = klee_metadata.get("counterexample_names")
    safe_counterexample_names = (
        sorted(
            {
                name
                for name in raw_counterexample_names
                if isinstance(name, str)
                and re.fullmatch(
                    r"C2HLSC_RELATIONAL_MISMATCH:(?:return|[A-Za-z_][A-Za-z0-9_]*)",
                    name,
                )
            }
        )
        if isinstance(raw_counterexample_names, list)
        else []
    )
    klee_metadata["counterexample_names"] = safe_counterexample_names
    klee_metadata["counterexample_count"] = len(safe_counterexample_names)
    repair_rows = [
        [
            str(repair.iteration),
            repair.stage or "",
            repair.family,
            repair.status,
            ", ".join(repair.target_files) or "_None_",
            repair.summary,
        ]
        for repair in repairs
    ]
    report_files = ["conversion_report.md", "conversion_report.json", KNOWLEDGE_GRAPH_FILENAME]
    if repairs:
        report_files.append(REPAIR_AUDIT_FILENAME)
    ppa_phase = state.phases.get("ppa")
    ppa_phase_line = ""
    if ppa_phase is not None:
        # A failing PPA criterion changes final_status, so expose its verdict and reason
        # beside the other gates instead of leaving a headline FAIL unexplained.
        detail = f" — {ppa_phase.summary}" if ppa_phase.summary else ""
        ppa_phase_line = f'- PPA workflow criteria: `{ppa_phase.status}`{detail}'

    md = f"""# c2hlsc_agent Conversion Report

## Final Status

**{status.upper()}**

## Inputs

- Top function: `{fn.name}`
- Source: `{fn.source_path}`
- Vitis part: `{config.part}`
- RTL verification backend: `{config.cosim_backend}`
- Vitis launcher: `{config.vitis_bin if config.cosim_backend in ('vitis', 'vitis-ssh') else 'not used'}`
- Clock period: `{config.clock}`
- Random seed: `{config.seed}`
- Test count: `{config.num_tests}`
- HLS-C generator policy: `{generated.generator_prompt_id}`
- Testbench generator policy: `{LEVERI_TESTBENCH_POLICY_ID}`

## Generated Files

{chr(10).join(f"- `{item}`" for item in generated_files)}

## Type Mapping

{_table(["Name", "Original", "Generated"], type_rows)}
## Argument Directions

{_table(["Argument", "Type", "Direction", "Length", "Interface"], arg_rows)}
## Interface Pragmas

{_table(["Argument", "Pragma", "Reason"], pragma_rows)}
## Transformations

{chr(10).join(f"- {item}" for item in generated.transformations)}

## Unsupported Constructs

{_table(["Severity", "Code", "Message", "Suggestion"], unsupported_rows)}
## Diagnostics

{chr(10).join(f"- [{d.severity}] {d.code}: {d.message}" for d in analysis.diagnostics.items) or "_None_"}

## Test Coverage Summary

- Deterministic random tests: {config.num_tests}
- Directed cases included by generator: zeros, all-ones, min/max, alternating patterns
- Pointer/array outputs compared by metadata or inferred direction

## Phase Results

- Software equivalence: `{state.status_for("software_equivalence")}`
- Paired shift-left traces: `{state.status_for("shift_left_trace")}`
- gcov concrete coverage: `{state.status_for("coverage_gcov")}`
- Relational KLEE (bounded configured domain): `{state.status_for("symbolic_klee")}`
- Relational KLEE scope/outcome: `{klee_metadata.get("scope", "unreported")}` /
  `{klee_metadata.get("outcome", "unreported")}`
- Relational KLEE counterexamples: `{", ".join(klee_metadata.get("counterexample_names", [])) or "none reported"}`
- Interpretation: PASS means no counterexample was found in the explored modeled domain;
  it is not a universal proof beyond declared bounds and assumptions.
- C simulation: `{state.status_for("csim")}`
- C synthesis: `{state.status_for("csynth")}`
- C/RTL co-simulation: `{state.status_for("cosim")}`
{ppa_phase_line}
- Iterations: {iterations}

## Multi-Agent Loop Assessment

- Current owner: `{agent_decision.owner_agent}`
- Failure family: `{agent_decision.family}`
- Next action: {agent_decision.next_action}
- Repair scope: {agent_decision.repair_scope}
- Evidence needed: {", ".join(agent_decision.evidence_needed)}

## Repair Audit Trail

{_table(["Iteration", "Stage", "Family", "Status", "Files", "Summary"], repair_rows)}

## Mismatch Summary

{chr(10).join(f"- {m.to_dict()}" for m in state.mismatches) or "_None captured by agent; inspect phase logs if a test failed._"}
"""
    (project.root / "conversion_report.md").write_text(md, encoding="utf-8")

    machine = {
        "status": status,
        "top": fn.name,
        "part": config.part,
        "clock_ns": config.clock,
        "seed": config.seed,
        "num_tests": config.num_tests,
        "cosim_backend": config.cosim_backend,
        "vitis_bin": config.vitis_bin if config.cosim_backend in ("vitis", "vitis-ssh") else None,
        "generator_prompt_id": generated.generator_prompt_id,
        "testbench_policy_id": LEVERI_TESTBENCH_POLICY_ID,
        "software_equivalence": state.status_for("software_equivalence"),
        "shift_left_trace": state.status_for("shift_left_trace"),
        "coverage_gcov": state.status_for("coverage_gcov"),
        "symbolic_klee": state.status_for("symbolic_klee"),
        "relational_klee": klee_metadata,
        "csim": state.status_for("csim"),
        "csynth": state.status_for("csynth"),
        "cosim": state.status_for("cosim"),
        "iterations": iterations,
        "mismatches": [m.to_dict() for m in state.mismatches],
        "repairs": [repair.to_dict() for repair in repairs],
        "repair_audit_file": REPAIR_AUDIT_FILENAME if repairs else None,
        "unsupported_constructs": [d.to_dict() for d in analysis.unsupported_constructs],
        "diagnostics": analysis.diagnostics.to_list(),
        "agent_decision": agent_decision.to_dict(),
        "generated_files": generated_files + report_files,
        "phases": {name: result.to_dict() for name, result in state.phases.items()},
    }
    (project.root / "conversion_report.json").write_text(json.dumps(machine, indent=2), encoding="utf-8")
    write_knowledge_graph(project.root, analysis, config, state=state, repair_history=repairs)
    refresh_knowledge_graph(project.root)
