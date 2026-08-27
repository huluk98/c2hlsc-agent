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


def _validated(entry: object, arg_names: set[str]) -> dict[str, object] | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("argument")
    if name not in arg_names:
        return None
    proposal: dict[str, object] = {"argument": name}
    direction = entry.get("direction")
    if direction is not None:
        if direction not in _DIRECTIONS:
            return None
        proposal["direction"] = direction
    length = entry.get("length")
    if length is not None:
        if not isinstance(length, int) or isinstance(length, bool) or not 1 <= length <= _MAX_LENGTH:
            return None
        proposal["length"] = length
    scalar_range = entry.get("range")
    if scalar_range is not None:
        if (
            not isinstance(scalar_range, list)
            or len(scalar_range) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in scalar_range)
            or scalar_range[0] > scalar_range[1]
        ):
            return None
        proposal["range"] = scalar_range
    if len(proposal) == 1:
        return None  # an argument name with nothing proposed is not a proposal
    rationale = entry.get("rationale")
    proposal["rationale"] = (
        " ".join(str(rationale).split())[:_RATIONALE_LIMIT] if rationale else ""
    )
    return proposal


def propose_contract(
    analysis: AnalysisResult,
    llm: LLMClient,
) -> tuple[list[dict[str, object]], str | None]:
    """Validated contract proposals from the model, plus an error reason (or ``None``).

    Anything out of vocabulary -- an unknown argument, a non-positive length, a reversed
    range -- is dropped entry-by-entry rather than failing the batch, so one hallucinated
    row cannot cost the rows the source genuinely supports.
    """

    original_source = analysis.function.source_path.read_text(encoding="utf-8")
    system, user = build_contract_planner_prompt(analysis, original_source)
    try:
        payload = extract_json_payload(llm.complete(system, user))
    except RunBudgetExceeded:
        raise  # the caller owns budget policy; only llm_calls exhaustion is recoverable
    except Exception as exc:  # noqa: BLE001 -- optional proposals, never fatal
        return [], f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, list):
        return [], "model response carried no JSON array"
    arg_names = {arg.name for arg in analysis.function.args}
    proposals = [p for entry in payload if (p := _validated(entry, arg_names)) is not None]
    return proposals, None


def write_proposals(
    project_dir: Path,
    proposals: list[dict[str, object]],
    model: str | None,
    error: str | None,
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
    }
    path = project_dir / PROPOSALS_FILENAME
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
