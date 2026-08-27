"""contract_planner, live: model-proposed refinements of the argument contract.

The analyzer's regex inference conservatively DEFAULTS what it cannot derive -- most
importantly a pointer bound of 16 with a ``missing-pointer-bound`` warning -- and a wrong
bound silently narrows or misdirects the whole equivalence check. This agent asks the
model to propose directions, lengths, and ranges the C source actually justifies.

Proposals are **never applied automatically**. They are validated against the real
argument list, written to ``contract_proposals.json`` in the project, and noted in the
transformation ledger; a human copies what they agree with into the config
(``arguments.<name>.length`` and friends). That keeps the soundness-relevant knobs --
the testbench's array bounds and stimulus domains -- under explicit human control while
still harvesting what the model can read out of the source.
"""

from __future__ import annotations

import json
from pathlib import Path

from .analyze import AnalysisResult
from .run_control import RunBudgetExceeded
from .llm import LLMClient, build_contract_planner_prompt, extract_json_payload

CONTRACT_PLANNER_POLICY_ID = "contract_planner_proposals_v1"
PROPOSALS_FILENAME = "contract_proposals.json"

_DIRECTIONS = {"input", "output", "inout"}
_MAX_LENGTH = 65536
_RATIONALE_LIMIT = 300


def _validated(
    entry: object, arg_names: set[str]
) -> tuple[dict[str, object] | None, list[str]]:
    """(salvaged proposal or None, per-field rejection notes).

    Salvage is per FIELD, not per entry: the dogfooded model proposed the symbolic
    length "count" alongside a perfectly good direction, and an all-or-nothing drop
    threw away the good field with the bad one. Every value is isinstance-checked
    before any set membership test -- an unhashable value in a membership test is a
    TypeError, and a model response must never be able to crash the run.
    """

    dropped: list[str] = []
    if not isinstance(entry, dict):
        return None, ["entry is not an object"]
    name = entry.get("argument")
    if not isinstance(name, str) or name not in arg_names:
        return None, [f"unknown or non-string argument {name!r}"]
    proposal: dict[str, object] = {"argument": name}
    direction = entry.get("direction")
    if direction is not None:
        if isinstance(direction, str) and direction in _DIRECTIONS:
            proposal["direction"] = direction
        else:
            dropped.append(f"{name}: direction {direction!r} is not input|output|inout")
    length = entry.get("length")
    if length is not None:
        if isinstance(length, int) and not isinstance(length, bool) and 1 <= length <= _MAX_LENGTH:
            proposal["length"] = length
        else:
            dropped.append(
                f"{name}: length {length!r} is not an integer in [1, {_MAX_LENGTH}] "
                "(a symbolic bound belongs in a range proposal on that scalar)"
            )
    scalar_range = entry.get("range")
    if scalar_range is not None:
        if (
            isinstance(scalar_range, list)
            and len(scalar_range) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in scalar_range)
            and scalar_range[0] <= scalar_range[1]
        ):
            proposal["range"] = scalar_range
        else:
            dropped.append(f"{name}: range {scalar_range!r} is not [lo, hi] with integer lo <= hi")
    if len(proposal) == 1:
        return None, dropped or [f"{name}: nothing proposed"]
    rationale = entry.get("rationale")
    proposal["rationale"] = (
        " ".join(str(rationale).split())[:_RATIONALE_LIMIT] if rationale else ""
    )
    return proposal, dropped


def propose_contract(
    analysis: AnalysisResult,
    llm: LLMClient,
) -> tuple[list[dict[str, object]], list[str], str | None]:
    """(validated proposals, per-field rejection notes, backend error or ``None``).

    Invalid values are dropped field-by-field with a recorded reason, so one
    hallucinated field cannot cost the fields the source genuinely supports -- and a
    zero-proposal outcome is distinguishable from "the model proposed nothing": the
    rejections say exactly what was proposed and why it was refused.
    """

    original_source = analysis.function.source_path.read_text(encoding="utf-8")
    system, user = build_contract_planner_prompt(analysis, original_source)
    try:
        payload = extract_json_payload(llm.complete(system, user))
    except RunBudgetExceeded:
        raise  # the caller owns budget policy; only llm_calls exhaustion is recoverable
    except Exception as exc:  # noqa: BLE001 -- optional proposals, never fatal
        return [], [], f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, list):
        return [], [], "model response carried no JSON array"
    arg_names = {arg.name for arg in analysis.function.args}
    proposals: list[dict[str, object]] = []
    rejected: list[str] = []
    for entry in payload:
        proposal, dropped = _validated(entry, arg_names)
        rejected.extend(dropped)
        if proposal is not None:
            proposals.append(proposal)
    return proposals, rejected, None


def write_proposals(
    project_dir: Path,
    proposals: list[dict[str, object]],
    model: str | None,
    error: str | None,
    rejected: list[str] | None = None,
) -> Path:
    payload = {
        "policy_id": CONTRACT_PLANNER_POLICY_ID,
        "applied": False,
        "note": (
            "Proposals are advisory. Copy the ones you agree with into the config as "
            "arguments.<name>.direction/length/range, then rerun convert."
        ),
        "model": model,
        "error": error,
        "proposals": proposals,
        "rejected": list(rejected or []),
    }
    path = project_dir / PROPOSALS_FILENAME
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
