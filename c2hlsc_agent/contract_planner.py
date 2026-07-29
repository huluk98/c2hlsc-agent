"""Live contract_planner agent: LLM-proposed argument-contract refinements.

Implements the ``contract_planner`` role declared in :mod:`agent_loop` as a real LLM
pass. The regex analyzer's inference is the deterministic baseline; the planner may
*propose* per-argument ``direction`` / ``length`` / ``range`` fields, which are merged
into ``config.arguments`` only where the user's own config left the field unset
(user config wins per-field). The caller then re-runs ``analyze_source`` so the merged
contract takes effect — directions and bounds are baked into ``AnalysisResult`` at
analyze time.

Failure discipline mirrors the generator: any LLM error, unparsable response, or
invalid proposal degrades to the analyzer's own contract (deterministic fallback).
The verifier ladder remains the equivalence gate — a wrong proposal can change what
the testbench drives, but it is always recorded in ``contract_plan.json`` and the
conversion report, and it can never bypass verification.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from .analyze import AnalysisResult
from .config import AgentConfig, ArgumentConfig
from .llm import LLMClient, build_planner_prompt, extract_json_block

_DIRECTIONS = {"input", "output", "inout"}
_PLANNER_FIELDS = ("direction", "length", "range")


@dataclass
class PlanResult:
    """Outcome of one planning pass, written to ``contract_plan.json``."""

    proposals: dict[str, ArgumentConfig] = field(default_factory=dict)
    applied: dict[str, list[str]] = field(default_factory=dict)  # arg -> fields applied
    skipped: dict[str, str] = field(default_factory=dict)  # arg -> reason
    notes: str = ""
    raw_ok: bool = False
    model: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposals": {
                name: {
                    "direction": cfg.direction,
                    "length": cfg.length,
                    "range": list(cfg.range) if cfg.range else None,
                }
                for name, cfg in self.proposals.items()
            },
            "applied": {name: list(fields) for name, fields in self.applied.items()},
            "skipped": dict(self.skipped),
            "notes": self.notes,
            "raw_ok": self.raw_ok,
            "model": self.model,
        }


def _int_or_none(value: Any) -> int | None:
    """Strict integer coercion: bools and non-numerics are rejected, not coerced."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _validate_proposal(name: str, data: Any, known_args: set[str]) -> tuple[ArgumentConfig | None, str]:
    """Validate one raw proposal into an ArgumentConfig; mirrors the structural-gate
    role that ``is_plausible_translation_unit`` plays for generated code."""

    if name not in known_args:
        return None, "unknown argument name"
    if not isinstance(data, dict):
        return None, "proposal is not a mapping"
    direction = data.get("direction")
    if direction is not None:
        if not isinstance(direction, str) or direction not in _DIRECTIONS:
            return None, f"invalid direction {direction!r}"
    length = None
    if data.get("length") is not None:
        length = _int_or_none(data["length"])
        if length is None or length <= 0:
            return None, f"invalid length {data['length']!r}"
    parsed_range: tuple[int, int] | None = None
    if data.get("range") is not None:
        raw_range = data["range"]
        if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
            return None, f"invalid range {raw_range!r}"
        lo, hi = _int_or_none(raw_range[0]), _int_or_none(raw_range[1])
        if lo is None or hi is None or lo > hi:
            return None, f"invalid range {raw_range!r}"
        parsed_range = (lo, hi)
    if direction is None and length is None and parsed_range is None:
        return None, "no usable fields proposed"
    return ArgumentConfig(direction=direction, length=length, range=parsed_range), ""


def plan_contracts(
    analysis: AnalysisResult,
    config: AgentConfig,
    llm: LLMClient,
    original_source: str,
) -> PlanResult:
    """Run the planner and merge validated proposals into ``config.arguments``.

    Only fields the user config leaves unset are filled (user config wins per-field).
    Returns a :class:`PlanResult`; ``result.changed`` tells the caller whether a
    re-analyze is needed.
    """

    result = PlanResult(model=getattr(llm, "model", None))
    system, user = build_planner_prompt(analysis, original_source, config.nl_spec)
    try:
        response = llm.complete(system, user)
    except Exception as exc:  # deterministic fallback, matching _llm_repair's discipline
        print(f"contract_planner LLM call failed ({exc}); keeping the analyzer's contract.", file=sys.stderr)
        return result
    payload = extract_json_block(response)
    if not isinstance(payload, dict):
        return result
    result.raw_ok = True
    notes = payload.get("notes")
    if isinstance(notes, str):
        result.notes = notes.strip()
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        return result

    known = {arg.name for arg in analysis.function.args}
    for raw_name, data in arguments.items():
        name = str(raw_name)
        proposal, reason = _validate_proposal(name, data, known)
        if proposal is None:
            result.skipped[name] = reason
            continue
        result.proposals[name] = proposal
        existing = config.arguments.get(name)
        if existing is None:
            existing = ArgumentConfig()
            config.arguments[name] = existing
        applied_fields = []
        for field_name in _PLANNER_FIELDS:
            value = getattr(proposal, field_name)
            if value is None:
                continue
            if getattr(existing, field_name) is None:
                setattr(existing, field_name, value)
                applied_fields.append(field_name)
        if applied_fields:
            result.applied[name] = applied_fields
        else:
            result.skipped[name] = "user config already sets every proposed field"
    return result
