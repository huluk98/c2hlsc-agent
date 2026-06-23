from __future__ import annotations

import json
from pathlib import Path

from .agent_loop import classify_failure
from .analyze import AnalysisResult
from .config import AgentConfig
from .convert import GeneratedSource
from .equivalence import VerificationState
from .hls_project import ProjectFiles


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
    return "pass" if all(state.status_for(phase) == "pass" for phase in required) else "fail"


def write_reports(
    project: ProjectFiles,
    analysis: AnalysisResult,
    generated: GeneratedSource,
    config: AgentConfig,
    state: VerificationState,
    iterations: int,
) -> None:
    status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
    fn = analysis.function
    arg_rows = [[arg.name, arg.c_type, arg.direction, str(arg.length or ""), arg.interface or config.interface_mode] for arg in fn.args]
    type_rows = [[row["name"], row["original"], row["generated"]] for row in analysis.type_mappings]
    pragma_rows = [[row["argument"], row["pragma"], row["reason"]] for row in generated.interface_pragmas]
    unsupported_rows = [[d.severity, d.code, d.message, d.suggestion or ""] for d in analysis.unsupported_constructs]
    generated_files = [str(path.relative_to(project.root)) for path in project.generated_files]
    agent_decision = classify_failure(state, config.run_vitis, analysis.diagnostics.has_errors)

    md = f"""# c2hlsc_agent Conversion Report

## Final Status

**{status.upper()}**

## Inputs

- Top function: `{fn.name}`
- Source: `{fn.source_path}`
- Vitis part: `{config.part}`
- Clock period: `{config.clock}`
- Random seed: `{config.seed}`
- Test count: `{config.num_tests}`

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
- C simulation: `{state.status_for("csim")}`
- C synthesis: `{state.status_for("csynth")}`
- C/RTL co-simulation: `{state.status_for("cosim")}`
- Iterations: {iterations}

## Multi-Agent Loop Assessment

- Current owner: `{agent_decision.owner_agent}`
- Failure family: `{agent_decision.family}`
- Next action: {agent_decision.next_action}
- Repair scope: {agent_decision.repair_scope}
- Evidence needed: {", ".join(agent_decision.evidence_needed)}

## Mismatch Summary

{chr(10).join(f"- {m.to_dict()}" for m in state.mismatches) or "_None captured by agent; inspect phase logs if a test failed._"}
"""
    (project.root / "conversion_report.md").write_text(md, encoding="utf-8")

    machine = {
        "status": status,
        "top": fn.name,
        "software_equivalence": state.status_for("software_equivalence"),
        "csim": state.status_for("csim"),
        "csynth": state.status_for("csynth"),
        "cosim": state.status_for("cosim"),
        "iterations": iterations,
        "mismatches": [m.to_dict() for m in state.mismatches],
        "unsupported_constructs": [d.to_dict() for d in analysis.unsupported_constructs],
        "diagnostics": analysis.diagnostics.to_list(),
        "agent_decision": agent_decision.to_dict(),
        "generated_files": generated_files + ["conversion_report.md", "conversion_report.json"],
        "phases": {name: result.to_dict() for name, result in state.phases.items()},
    }
    (project.root / "conversion_report.json").write_text(json.dumps(machine, indent=2), encoding="utf-8")
