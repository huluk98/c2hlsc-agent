from __future__ import annotations

import difflib
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .agent_loop import classify_failure
from .analyze import AnalysisResult
from .config import AgentConfig
from .equivalence import VerificationState
from .hls_runner import earliest_failing_phase
from .llm import LLMClient, build_repair_prompt, extract_full_file, is_plausible_translation_unit


REPAIR_AGENT_NAME = "hlsc_repair_agent"
REPAIR_AUDIT_FILENAME = "repair_audit.json"
_EVIDENCE_LIMIT = 1600


@dataclass(frozen=True)
class RepairFileChange:
    path: str
    action: str
    before_sha256: str
    after_sha256: str
    diff: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "action": self.action,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "diff": self.diff,
        }


@dataclass(frozen=True)
class RepairOutcome:
    iteration: int
    stage: str | None
    family: str
    owner_agent: str
    status: str
    summary: str
    target_files: tuple[str, ...]
    changes: tuple[RepairFileChange, ...]
    evidence_excerpt: str
    next_action: str
    repair_scope: str

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> dict[str, object]:
        return {
            "iteration": self.iteration,
            "stage": self.stage,
            "family": self.family,
            "owner_agent": self.owner_agent,
            "status": self.status,
            "summary": self.summary,
            "target_files": list(self.target_files),
            "changes": [change.to_dict() for change in self.changes],
            "evidence_excerpt": self.evidence_excerpt,
            "next_action": self.next_action,
            "repair_scope": self.repair_scope,
        }


def load_repair_audit(project_dir: Path) -> list[RepairOutcome]:
    audit_path = project_dir / REPAIR_AUDIT_FILENAME
    if not audit_path.exists():
        return []
    raw = json.loads(audit_path.read_text(encoding="utf-8"))
    outcomes: list[RepairOutcome] = []
    for item in raw:
        changes = tuple(
            RepairFileChange(
                path=str(change["path"]),
                action=str(change["action"]),
                before_sha256=str(change["before_sha256"]),
                after_sha256=str(change["after_sha256"]),
                diff=str(change["diff"]),
            )
            for change in item.get("changes", [])
        )
        outcomes.append(
            RepairOutcome(
                iteration=int(item["iteration"]),
                stage=item.get("stage"),
                family=str(item["family"]),
                owner_agent=str(item["owner_agent"]),
                status=str(item["status"]),
                summary=str(item["summary"]),
                target_files=tuple(str(path) for path in item.get("target_files", [])),
                changes=changes,
                evidence_excerpt=str(item.get("evidence_excerpt", "")),
                next_action=str(item.get("next_action", "")),
                repair_scope=str(item.get("repair_scope", "")),
            )
        )
    return outcomes


def clear_repair_audit(project_dir: Path) -> None:
    audit_path = project_dir / REPAIR_AUDIT_FILENAME
    if audit_path.exists():
        audit_path.unlink()


def repair_project(
    project_dir: Path,
    analysis: AnalysisResult,
    config: AgentConfig,
    state: VerificationState,
    iteration: int,
    llm: LLMClient | None = None,
) -> RepairOutcome:
    phase = earliest_failing_phase(state, config.run_vitis)
    decision = classify_failure(state, config.run_vitis, analysis.diagnostics.has_errors)
    evidence = _phase_evidence(state, phase)
    changes: list[RepairFileChange] = []

    if phase is None or decision.status == "pass":
        status = "pass"
        summary = "Verification already passes; no repair was attempted."
    elif decision.status == "blocked":
        status = "blocked"
        summary = f"No mechanical repair is available for blocked family {decision.family!r}."
    else:
        changes.extend(_repair_missing_standard_includes(project_dir, evidence))
        changes.extend(_repair_restrict_for_cpp(project_dir, evidence))
        changes.extend(_repair_missing_original_support(project_dir, analysis, evidence))
        changes.extend(_repair_invalid_interface_pragmas(project_dir, phase, decision.family, evidence))
        if changes:
            status = "applied"
            summary = f"Applied {len(changes)} auditable mechanical repair(s); rerun verification from software equivalence."
        elif llm is not None and getattr(config, "use_llm", False):
            llm_changes = _llm_repair(project_dir, analysis, decision, phase, evidence, llm)
            changes.extend(llm_changes)
            if llm_changes:
                status = "applied_llm"
                summary = (
                    f"Applied LLM repair to {', '.join(c.path for c in llm_changes)}; "
                    "rerun verification from software equivalence."
                )
            else:
                status = "no_change"
                summary = f"No conservative or LLM repair matched family {decision.family!r} at stage {phase!r}."
        else:
            status = "no_change"
            summary = f"No conservative mechanical repair matched family {decision.family!r} at stage {phase!r}."

    target_files = tuple(dict.fromkeys(change.path for change in changes))
    outcome = RepairOutcome(
        iteration=iteration,
        stage=phase,
        family=decision.family,
        owner_agent=decision.owner_agent,
        status=status,
        summary=summary,
        target_files=target_files,
        changes=tuple(changes),
        evidence_excerpt=evidence.strip()[:_EVIDENCE_LIMIT],
        next_action=decision.next_action,
        repair_scope=decision.repair_scope,
    )
    _append_audit(project_dir, outcome)
    return outcome


def _llm_repair(
    project_dir: Path,
    analysis: AnalysisResult,
    decision: object,
    phase: str,
    evidence: str,
    llm: LLMClient,
) -> list[RepairFileChange]:
    """Escalate to an LLM for a minimal patch to the generated HLS-C.

    Only ``src/hls_top.cpp`` is ever rewritten. The host-equivalence testbench and the
    golden ``input.c`` oracle are never handed to the model and never overwritten, so the
    verifier ladder stays the equivalence gate even when the model is wrong. The candidate
    patch is structurally validated before it is accepted, so a prose/log response cannot
    be written as source.
    """

    path = project_dir / "src" / "hls_top.cpp"
    if not path.exists():
        return []
    current = path.read_text(encoding="utf-8")
    top = analysis.function.name

    system, user = build_repair_prompt(analysis, decision, phase, evidence, "src/hls_top.cpp", current)
    try:
        response = llm.complete(system, user)
        new_text = extract_full_file(response, must_contain=f"{top}(")
    except Exception as exc:
        print(
            f"c2hlsc repair: LLM patch attempt failed ({type(exc).__name__}: {exc}); "
            "keeping the deterministic result.",
            file=sys.stderr,
        )
        return []
    if not new_text or new_text.strip() == current.strip():
        return []
    if not is_plausible_translation_unit(new_text, top):
        return []

    change = _rewrite_file(
        project_dir,
        path,
        f"llm repair (model={getattr(llm, 'model', '?')}, family={getattr(decision, 'family', '?')}) for {phase} stage",
        new_text,
    )
    return [change] if change else []


def _phase_evidence(state: VerificationState, phase: str | None) -> str:
    if phase is None:
        return ""
    result = state.phases.get(phase)
    if result is None:
        return ""
    parts = [result.summary, result.stdout, result.stderr]
    if result.log_path and result.log_path.exists():
        try:
            parts.append(result.log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(part for part in parts if part)


def _append_audit(project_dir: Path, outcome: RepairOutcome) -> None:
    audit_path = project_dir / REPAIR_AUDIT_FILENAME
    existing: list[object] = []
    if audit_path.exists():
        existing_raw = json.loads(audit_path.read_text(encoding="utf-8"))
        if isinstance(existing_raw, list):
            existing = existing_raw
    existing.append(outcome.to_dict())
    audit_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _relative(project_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(project_dir))
    except ValueError:
        return str(path)


def _rewrite_file(project_dir: Path, path: Path, action: str, new_text: str) -> RepairFileChange | None:
    old_text = path.read_text(encoding="utf-8")
    if new_text == old_text:
        return None
    path.write_text(new_text, encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{_relative(project_dir, path)}",
            tofile=f"b/{_relative(project_dir, path)}",
        )
    )
    return RepairFileChange(
        path=_relative(project_dir, path),
        action=action,
        before_sha256=_sha256(old_text),
        after_sha256=_sha256(new_text),
        diff=diff,
    )


def _ensure_includes(text: str, includes: list[str]) -> str:
    missing = [header for header in includes if f"#include {header}" not in text]
    if not missing:
        return text
    lines = text.splitlines()
    insert_at = 0
    for idx, line in enumerate(lines):
        if line.startswith("#include "):
            insert_at = idx + 1
    if insert_at == 0:
        for idx, line in enumerate(lines):
            if line.startswith("#define "):
                insert_at = idx + 1
                break
    lines[insert_at:insert_at] = [f"#include {header}" for header in missing]
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + suffix


def _includes_needed_from_evidence(evidence: str) -> list[str]:
    checks = [
        (r"\b(size_t|NULL)\b", "<stddef.h>"),
        (r"\b(INT_MIN|INT_MAX|UINT_MAX|ULONG_MAX|LONG_MAX|CHAR_BIT)\b", "<limits.h>"),
        (r"\b(memcpy|memmove|memset|memcmp|strlen|strcpy|strncpy)\b", "<string.h>"),
        (r"\b(sqrt|fabs|sin|cos|tan|pow|floor|ceil)\b", "<math.h>"),
        (r"\b(ap_int|ap_uint)\b|ap_int\.h", "<ap_int.h>"),
    ]
    lowered = evidence.lower()
    if not re.search(r"not declared|not been declared|unknown type name|does not name a type|fatal error|undeclared", lowered):
        return []
    needed: list[str] = []
    for pattern, header in checks:
        if re.search(pattern, evidence):
            needed.append(header)
    return list(dict.fromkeys(needed))


def _repair_missing_standard_includes(project_dir: Path, evidence: str) -> list[RepairFileChange]:
    includes = _includes_needed_from_evidence(evidence)
    if not includes:
        return []
    header_path = project_dir / "src" / "hls_top.hpp"
    if not header_path.exists():
        return []
    old_text = header_path.read_text(encoding="utf-8")
    new_text = _ensure_includes(old_text, includes)
    change = _rewrite_file(
        project_dir,
        header_path,
        f"add missing standard include(s): {', '.join(includes)}",
        new_text,
    )
    return [change] if change else []


def _replace_restrict_tokens(text: str) -> str:
    return re.sub(r"(?<!_)\brestrict\b(?!__)", "__restrict__", text)


def _ensure_testbench_restrict_macro(text: str) -> str:
    marker = "#define restrict __restrict__"
    if marker in text or "extern \"C\"" not in text:
        return text
    return text.replace(
        "extern \"C\" {",
        "#ifndef restrict\n#define restrict __restrict__\n#endif\n\nextern \"C\" {",
        1,
    )


def _repair_restrict_for_cpp(project_dir: Path, evidence: str) -> list[RepairFileChange]:
    if not re.search(r"\brestrict\b", evidence):
        return []
    changes: list[RepairFileChange] = []
    for relative in ("src/hls_top.hpp", "src/hls_top.cpp"):
        path = project_dir / relative
        if not path.exists():
            continue
        change = _rewrite_file(project_dir, path, "replace C restrict with C++ __restrict__", _replace_restrict_tokens(path.read_text(encoding="utf-8")))
        if change:
            changes.append(change)
    testbench = project_dir / "tb" / "testbench.cpp"
    if testbench.exists():
        change = _rewrite_file(
            project_dir,
            testbench,
            "define restrict for macro-included golden C",
            _ensure_testbench_restrict_macro(testbench.read_text(encoding="utf-8")),
        )
        if change:
            changes.append(change)
    return changes


def _candidate_missing_symbols(evidence: str) -> list[str]:
    patterns = [
        r"[`'‘\"](?P<symbol>[A-Za-z_]\w*)[`'’\"]\s+was not declared in this scope",
        r"use of undeclared identifier\s+[`'‘\"](?P<symbol>[A-Za-z_]\w*)[`'’\"]",
        r"undefined reference to\s+[`'‘\"](?P<symbol>[A-Za-z_]\w*)",
        r"implicit declaration of function\s+[`'‘\"]?(?P<symbol>[A-Za-z_]\w*)",
    ]
    symbols: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, evidence):
            symbols.append(match.group("symbol"))
    excluded = {
        "NULL",
        "size_t",
        "memcpy",
        "memmove",
        "memset",
        "memcmp",
        "strlen",
        "sqrt",
        "fabs",
        "sin",
        "cos",
        "pow",
    }
    return [symbol for symbol in dict.fromkeys(symbols) if symbol not in excluded]


def _has_function_definition(source: str, symbol: str) -> bool:
    pattern = re.compile(
        rf"(?:^|[;\n}}])\s*(?:static\s+|inline\s+|extern\s+)*[A-Za-z_][\w\s\*]*\s+{re.escape(symbol)}\s*\([^;{{}}]*\)\s*\{{",
        flags=re.S,
    )
    return bool(pattern.search(source))


def _support_include_block(top_name: str) -> str:
    renamed = f"{top_name}_c2hlsc_repair_reference"
    return f"""
// c2hlsc_repair_agent: include copied input source with the top renamed so
// preserved top bodies can call original helper functions and globals.
#ifndef C2HLSC_REPAIR_INCLUDE_ORIGINAL_SUPPORT
#define C2HLSC_REPAIR_INCLUDE_ORIGINAL_SUPPORT
#ifndef restrict
#define restrict __restrict__
#endif
#define {top_name} {renamed}
#include "../input.c"
#undef {top_name}
#endif
"""


def _repair_missing_original_support(
    project_dir: Path,
    analysis: AnalysisResult,
    evidence: str,
) -> list[RepairFileChange]:
    source_path = project_dir / "input.c"
    hls_source = project_dir / "src" / "hls_top.cpp"
    if not source_path.exists() or not hls_source.exists():
        return []
    current = hls_source.read_text(encoding="utf-8")
    if "C2HLSC_REPAIR_INCLUDE_ORIGINAL_SUPPORT" in current:
        return []
    input_source = source_path.read_text(encoding="utf-8")
    top_name = analysis.function.name
    symbols = [symbol for symbol in _candidate_missing_symbols(evidence) if symbol != top_name]
    if not any(_has_function_definition(input_source, symbol) for symbol in symbols):
        return []
    block = _support_include_block(top_name)
    if '#include "hls_top.hpp"' in current:
        new_text = current.replace('#include "hls_top.hpp"', f'#include "hls_top.hpp"{block}', 1)
    else:
        new_text = block.lstrip() + current
    change = _rewrite_file(
        project_dir,
        hls_source,
        "include original source with renamed top to supply helper definitions",
        new_text,
    )
    return [change] if change else []


def _repair_invalid_interface_pragmas(
    project_dir: Path,
    phase: str | None,
    family: str,
    evidence: str,
) -> list[RepairFileChange]:
    if phase not in {"csim", "csynth", "cosim"}:
        return []
    if family != "interface_contract" and not re.search(r"\b(interface|axis|s_axilite|m_axi|ap_memory|port)\b", evidence, re.I):
        return []
    source_path = project_dir / "src" / "hls_top.cpp"
    if not source_path.exists():
        return []
    lines = source_path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    removed = 0
    inserted_comment = False
    for line in lines:
        if re.match(r"\s*#pragma\s+HLS\s+INTERFACE\b", line):
            removed += 1
            if not inserted_comment:
                indent = re.match(r"\s*", line).group(0)
                new_lines.append(
                    f"{indent}// c2hlsc_repair_agent: removed generated INTERFACE pragmas after {phase} interface failure."
                )
                inserted_comment = True
            continue
        new_lines.append(line)
    if removed == 0:
        return []
    new_text = "\n".join(new_lines) + "\n"
    change = _rewrite_file(
        project_dir,
        source_path,
        f"remove {removed} generated interface pragma(s) after {phase} failure",
        new_text,
    )
    return [change] if change else []
