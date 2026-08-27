from __future__ import annotations

import json
from pathlib import Path

from .agent_loop import classify_failure
from .analyze import AnalysisResult
from .config import AgentConfig
from .convert import GeneratedSource
from .equivalence import VerificationState
from .hlsc_repair_agent import REPAIR_AUDIT_FILENAME, RepairOutcome
from .hls_project import ProjectFiles
from .hls_runner import required_phases
from .leveri_testgen import LEVERI_TESTBENCH_POLICY_ID
from .stimulus import directed_schedule, extra_vectors


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_None_\n"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(out) + "\n"


def final_status(state: VerificationState, run_vitis: bool, diagnostics_has_errors: bool) -> str:
    if diagnostics_has_errors:
        return "fail"
    return (
        "pass"
        if all(state.status_for(phase) == "pass" for phase in required_phases(run_vitis))
        else "fail"
    )


def read_coverage(project_root: Path) -> dict[str, object]:
    """Read whatever the coverage tiers left behind, without running them.

    Missing reports are not an error: gcov and KLEE are opt-in targets, so their absence
    means "not collected", which is reported as such rather than as zero coverage.
    """

    coverage: dict[str, object] = {}
    for name, relative in (("gcov", "coverage/gcov_report.json"), ("klee", "coverage/klee_report.json")):
        path = project_root / relative
        if not path.exists():
            continue
        try:
            coverage[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            coverage[name] = {"status": "unreadable", "report": relative}
    return coverage


def _coverage_lines(coverage: dict[str, object]) -> str:
    gcov = coverage.get("gcov") if isinstance(coverage.get("gcov"), dict) else None
    klee = coverage.get("klee") if isinstance(coverage.get("klee"), dict) else None
    if not gcov and not klee:
        return (
            "- Structural coverage: _not collected_ "
            "(run `make gcov-coverage` / `make klee-coverage`, or `c2hlsc-agent refine`)"
        )
    lines: list[str] = []
    if gcov:
        status = gcov.get("status", "unknown")
        line_pct = gcov.get("line_coverage")
        branch_pct = gcov.get("branch_coverage")
        parts = [f"status `{status}`"]
        if isinstance(line_pct, (int, float)):
            parts.append(f"lines {line_pct:.2f}%")
        if isinstance(branch_pct, (int, float)):
            parts.append(f"branches {branch_pct:.2f}%")
        uncovered = gcov.get("uncovered_lines")
        if isinstance(uncovered, list) and uncovered:
            parts.append(f"{len(uncovered)} uncovered line(s)")
        lines.append(f"- gcov structural coverage: {', '.join(parts)}")
    if klee:
        status = klee.get("status", "unknown")
        ktests = klee.get("ktest_count")
        suffix = f", {ktests} ktest(s)" if isinstance(ktests, int) else ""
        lines.append(f"- KLEE symbolic exploration: status `{status}`{suffix}")
    return "\n".join(lines)


def write_reports(
    project: ProjectFiles,
    analysis: AnalysisResult,
    generated: GeneratedSource,
    config: AgentConfig,
    state: VerificationState,
    iterations: int,
    repairs: list[RepairOutcome] | None = None,
    run_control: dict[str, object] | None = None,
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
    report_files = ["conversion_report.md", "conversion_report.json"]
    if repairs:
        report_files.append(REPAIR_AUDIT_FILENAME)

    schedule = directed_schedule(config)
    directed_summary = ", ".join(f"`{name}`" for name in schedule) or "_none configured_"
    vectors = extra_vectors(config)
    refinement_summary = (
        ", ".join(f"`{vector.origin}`" for vector in vectors) if vectors else "_none_"
    )
    coverage = read_coverage(project.root)
    coverage_summary = _coverage_lines(coverage)

    run_control_section = ''
    if run_control:
        run_id = run_control.get('run_id', '')
        controller_status = run_control.get('status', '')
        controller_reason = run_control.get('reason', '') or '-'
        ledger_file = run_control.get('ledger_file', '')
        usage = dict(run_control.get('usage', {}))
        budget = dict(run_control.get('budget', {}))
        attempts = usage.get('attempts', 0)
        max_attempts = budget.get('max_attempts', 0)
        llm_calls = usage.get('llm_calls', 0)
        max_llm_calls = budget.get('max_llm_calls', 0)
        vitis_runs = usage.get('vitis_runs', 0)
        max_vitis_runs = budget.get('max_vitis_runs', 0)
        run_control_section = (
            '## Bounded Run Controller\n\n'
            f'- Run ID: `{run_id}`\n'
            f'- Controller status: `{controller_status}`\n'
            f'- Reason: {controller_reason}\n'
            f'- Attempts: {attempts}/{max_attempts}\n'
            f'- LLM calls: {llm_calls}/{max_llm_calls}\n'
            f'- Vitis runs: {vitis_runs}/{max_vitis_runs}\n'
            f'- Ledger: `{ledger_file}`\n'
        )

    md = f"""# c2hlsc_agent Conversion Report

## Final Status

**{status.upper()}**

{run_control_section}
## Inputs

- Top function: `{fn.name}`
- Source: `{fn.source_path}`
- Vitis part: `{config.part}`
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
- Directed cases included by generator: {directed_summary}
- Coverage-refinement vectors replayed first: {refinement_summary}
- Pointer/array outputs compared by metadata or inferred direction
{coverage_summary}

## Phase Results

- Software equivalence: `{state.status_for("software_equivalence")}`
- Trace consistency (shift-left dual-tier): `{state.status_for("trace_consistency")}`
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

## Repair Audit Trail

{_table(["Iteration", "Stage", "Family", "Status", "Files", "Summary"], repair_rows)}

## Mismatch Summary

{chr(10).join(f"- {m.to_dict()}" for m in state.mismatches) or "_None captured by agent; inspect phase logs if a test failed._"}
"""
    (project.root / "conversion_report.md").write_text(md, encoding="utf-8")

    machine = {
        'run_control': run_control,
        "status": status,
        "top": fn.name,
        "generator_prompt_id": generated.generator_prompt_id,
        "testbench_policy_id": LEVERI_TESTBENCH_POLICY_ID,
        "software_equivalence": state.status_for("software_equivalence"),
        "trace_consistency": state.status_for("trace_consistency"),
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
        "directed_tests": schedule,
        "extra_vectors": [vector.to_dict() for vector in vectors],
        "coverage": coverage,
    }
    (project.root / "conversion_report.json").write_text(json.dumps(machine, indent=2), encoding="utf-8")
