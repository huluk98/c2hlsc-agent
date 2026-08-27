from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analyze import AnalysisResult, FunctionArg, FunctionInfo, _constant_dim
from .config import AgentConfig
from .hlsc_generator import HLSC_GENERATOR_PROMPT_ID, HLSC_GENERATOR_SYSTEM_PROMPT
from .llm import (
    LLMClient,
    build_generator_user_prompt,
    build_nl_reference_prompt,
    extract_hls_source,
    extract_reference_c,
)


@dataclass
class GeneratedSource:
    header: str
    source: str
    transformations: list[str] = field(default_factory=list)
    interface_pragmas: list[dict[str, str]] = field(default_factory=list)
    generator_prompt_id: str = HLSC_GENERATOR_PROMPT_ID


def _include_for_types(args: list[FunctionArg], return_type: str) -> str:
    text = " ".join([return_type] + [arg.c_type for arg in args])
    includes = ["#include <stdint.h>"]
    if "ap_int" in text or "ap_uint" in text:
        includes.append("#include <ap_int.h>")
    return "\n".join(includes)


def _pragma_lines(config: AgentConfig, args: list[FunctionArg]) -> tuple[list[str], list[dict[str, str]]]:
    if not config.allow_pragmas:
        return [], []
    lines: list[str] = []
    rows: list[dict[str, str]] = []
    if config.interface_mode == "default":
        return lines, rows
    for arg in args:
        if config.interface_mode == "s_axilite" or not arg.is_pointer_like:
            line = f"#pragma HLS INTERFACE s_axilite port={arg.name}"
            reason = "scalar/control interface requested by configuration"
        elif config.interface_mode in {"ap_memory", "m_axi", "axis"}:
            mode = config.interface_mode
            line = f"#pragma HLS INTERFACE {mode} port={arg.name}"
            reason = f"{mode} interface requested by configuration; direction is {arg.direction}"
        else:
            continue
        lines.append(line)
        rows.append({"argument": arg.name, "pragma": line, "reason": reason})
    lines.append("#pragma HLS INTERFACE s_axilite port=return")
    rows.append({"argument": "return", "pragma": lines[-1], "reason": "control return interface requested by configuration"})
    return lines, rows


def _file_scope_context(source: str, function: FunctionInfo) -> tuple[str, str]:
    """Split the input's file scope into ``#include`` lines and everything else.

    The conservative generator emits only the top function body, so macros, typedefs,
    constant tables and helper functions defined beside it never reach the generated
    translation unit and it fails to compile. Includes have to stay at file scope; the
    caller places the rest in an anonymous namespace, because the testbench compiles
    the original input.c into the same program and external definitions would collide
    with the copies pulled in for the golden oracle.
    """

    remainder = source.replace(function.definition, "", 1)
    # A leftover prototype for the top would declare a second, never-defined function
    # inside the anonymous namespace.
    remainder = re.sub(
        rf"^[^;{{}}\n]*\b{re.escape(function.name)}\s*\([^;{{}}]*\)\s*;[ \t]*$",
        "",
        remainder,
        flags=re.M,
    )
    includes: list[str] = []
    kept: list[str] = []
    for line in remainder.splitlines():
        if re.match(r"[ \t]*#[ \t]*include\b", line):
            includes.append(line.strip())
        else:
            kept.append(line)
    return "\n".join(dict.fromkeys(includes)), "\n".join(kept).strip()


def _signature_typedefs(context: str) -> str:
    """Type aliases from the input that the generated header's signature may need.

    Only aliases of an existing type are carried. A struct, union or enum *definition*
    would declare a distinct new type in the testbench translation unit, which already
    has the original one from input.c; repeating an identical typedef is legal C++, so
    those are safe. The brace exclusion in the pattern is what draws that line.
    """

    found = [m.group(0).strip() for m in re.finditer(r"^[ \t]*typedef[^;{}]*;", context, re.M)]
    return "\n".join(dict.fromkeys(found))


def _header_signature(function: FunctionInfo) -> str:
    """The top's declaration with a non-literal outermost array bound dropped.

    A parameter's outermost array bound decays to a pointer and is not part of its
    type, so ``T a[]`` still matches a definition written ``T a[SIZE]``. Dropping it
    keeps the header from depending on a constant it cannot see: the constant lives
    in the generated unit's anonymous namespace, which the header precedes.
    """

    params = []
    for arg in function.args:
        params.append(
            re.sub(
                r"\[([^\]]*)\]",
                lambda m: "[]" if m.group(1).strip() and _constant_dim(m.group(1)) is None else m.group(0),
                arg.raw,
                count=1,
            )
        )
    return f"{function.return_type} {function.name}({', '.join(params)})"


def _generate_conservative_sources(analysis: AnalysisResult, config: AgentConfig) -> GeneratedSource:
    function = analysis.function
    pragma_lines, pragma_rows = _pragma_lines(config, function.args)
    body = function.body.rstrip()
    if pragma_lines:
        body = "\n" + "\n".join(f"  {line}" for line in pragma_lines) + "\n" + body
    try:
        original = function.source_path.read_text(encoding="utf-8")
    except OSError:
        original = ""
    carried_includes, carried_context = _file_scope_context(original, function) if original else ("", "")
    carried_typedefs = _signature_typedefs(carried_context)
    if carried_typedefs:
        # Hoisted into the header for the signature's sake, so they must leave the
        # anonymous namespace: two visible declarations of one alias are ambiguous.
        carried_context = re.sub(r"^[ \t]*typedef[^;{}]*;[ \t]*$", "", carried_context, flags=re.M).strip()
    context_block = ""
    if carried_context:
        context_block = (
            "\n// Carried over from the original file scope: the macros, types, constant\n"
            "// tables and helper functions the top function depends on. Internal linkage\n"
            "// keeps them from colliding with the copies the testbench includes from\n"
            "// input.c to build its golden oracle.\n"
            "namespace {\n" + carried_context + "\n}  // namespace\n"
        )
    header = f"""#ifndef C2HLSC_GENERATED_HLS_TOP_HPP
#define C2HLSC_GENERATED_HLS_TOP_HPP

{_include_for_types(function.args, function.return_type)}
{carried_typedefs}

{_header_signature(function)};

#endif
"""
    source = f"""// Generated by c2hlsc_agent.
// Transformation policy: preserve the original top-function control/data flow unless
// a conservative, equivalence-preserving refactor is explicitly recorded.

#include "hls_top.hpp"
{carried_includes}
{context_block}
{function.return_type} {function.name}({', '.join(arg.raw for arg in function.args)}) {{
{body}
}}
"""
    return GeneratedSource(
        header=header,
        source=source,
        transformations=[
            f"Applied {HLSC_GENERATOR_PROMPT_ID} as the HLS-C generator policy.",
            "Preserved original top-function body and signature for equivalence-first HLS baseline.",
        ]
        + (
            ["Carried the original file-scope macros, types and helpers into the generated unit."]
            if carried_context
            else []
        ),
        interface_pragmas=pragma_rows,
    )


def _llm_candidate(
    analysis: AnalysisResult,
    config: AgentConfig,
    llm: LLMClient,
    conservative: GeneratedSource,
    attempt: int = 0,
) -> GeneratedSource | None:
    """One LLM generation attempt, or ``None`` when unavailable/unparsable."""

    model = getattr(llm, "model", "?")
    try:
        original_source = analysis.function.source_path.read_text(encoding="utf-8")
        user = build_generator_user_prompt(analysis, original_source, nl_spec=getattr(config, "nl_spec", None))
        if attempt:
            user += (
                f"\nThis is independent candidate #{attempt + 1}: take a different pragma/refactoring "
                "strategy than the most obvious one, while preserving equivalence."
            )
        response = llm.complete(HLSC_GENERATOR_SYSTEM_PROMPT, user)
        source = extract_hls_source(response, analysis.function.name, original_source)
    except Exception as exc:
        # Surface the concrete backend failure (main's diagnostic improvement). This note
        # only survives when every candidate fails and the conservative copy is returned.
        conservative.transformations.append(
            f"LLM generation attempt {attempt + 1} failed [{type(exc).__name__}: {exc}]."
        )
        return None
    if not source:
        return None
    return GeneratedSource(
        header=conservative.header,
        source=source,
        transformations=[
            f"LLM HLS-C generator (model={model}, policy={HLSC_GENERATOR_PROMPT_ID}, candidate={attempt + 1}) "
            "produced a synthesizable translation unit."
            + (" User NL design intent was included in the prompt." if getattr(config, "nl_spec", None) else ""),
            "Verifier-gated: output is checked by host equivalence and Vitis CSim/CSynth/CoSim; "
            "failures trigger repair or fall back to the conservative copy.",
        ],
        interface_pragmas=[],
    )


def generate_hls_sources(
    analysis: AnalysisResult,
    config: AgentConfig,
    llm: LLMClient | None = None,
) -> GeneratedSource:
    """Generate HLS-C sources.

    When an LLM client is supplied and ``config.use_llm`` is set, the generator asks
    the model (following the ``hlsc_generator_vitis_beginner_v1`` policy) for a
    synthesizable translation unit and uses it in place of the verbatim copy. The
    conservative deterministic source is always built first and used as the fallback if
    the LLM is unavailable or its output cannot be parsed, so behaviour stays safe and
    the verifier remains the equivalence gate. ``config.nl_spec`` (user natural-language
    design intent) is included in the generator prompt when present.
    """

    conservative = _generate_conservative_sources(analysis, config)
    if llm is None or not getattr(config, "use_llm", False):
        return conservative

    candidate = _llm_candidate(analysis, config, llm, conservative)
    if candidate is None:
        # _llm_candidate records the specific failure reason (from main) on conservative.
        conservative.transformations.append(
            f"LLM HLS-C generation requested (model={getattr(llm, 'model', '?')}) but unavailable or "
            "unparsable; fell back to the conservative top-function copy."
        )
        return conservative
    return candidate


def generate_hls_source_candidates(
    analysis: AnalysisResult,
    config: AgentConfig,
    llm: LLMClient,
    count: int,
) -> list[GeneratedSource]:
    """Generate up to ``count`` independent LLM candidates (structural-gate filtered)."""

    conservative = _generate_conservative_sources(analysis, config)
    candidates: list[GeneratedSource] = []
    seen: set[str] = set()
    for attempt in range(max(1, count)):
        candidate = _llm_candidate(analysis, config, llm, conservative, attempt=attempt)
        if candidate is None:
            continue
        key = "".join(candidate.source.split())
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


class ReferenceGenerationError(RuntimeError):
    """The LLM backend call for the NL golden reference failed (not just unparsable)."""


def generate_reference_c(nl_spec: str, top_name: str, llm: LLMClient) -> str | None:
    """NL-only mode: generate the plain-C golden reference implementation.

    The result becomes ``input.c`` — the equivalence oracle the generated testbench and
    the whole verifier ladder compare against — so the rest of the pipeline runs exactly
    as in the C-input modes. Returns ``None`` when the model answered but the output is
    unparsable; raises :class:`ReferenceGenerationError` when the backend CALL itself
    failed (CLI error, timeout, auth/HTTP error) so the caller can report the real cause
    instead of blaming the model's answer.
    """

    system, user = build_nl_reference_prompt(nl_spec, top_name)
    try:
        response = llm.complete(system, user)
    except Exception as exc:  # noqa: BLE001 — surface the backend failure with context
        raise ReferenceGenerationError(str(exc)) from exc
    return extract_reference_c(response, top_name)
