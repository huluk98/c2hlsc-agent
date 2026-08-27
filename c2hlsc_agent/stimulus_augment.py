"""shift_left_testbench_agent, live: model-proposed directed stimulus, contract-gated.

The deterministic testbench is the floor and stays byte-identical: directed patterns,
seeded random stimulus, sentinel-filled outputs. This agent asks the model for a few
EXTRA input vectors aimed at edge cases -- branch boundaries, wrap-around, values that
maximize intermediate magnitudes -- which the testbench generator appends AFTER the
deterministic tests as constant tables.

Why this is safe by construction:

- The model contributes **data, never code**. Vectors are validated numerically against
  the declared contract (exact array lengths, scalar ranges, integer-ness) and embedded
  as constants by the same deterministic generator that writes the rest of the harness;
  the testbench file itself remains non-model-writable.
- Both the golden ``*_ref`` oracle and the generated HLS top are driven with the SAME
  vector, so an augmented vector can only ever *expose* a behavioral difference, never
  mask one. The worst a bad-but-valid vector can do is find a genuine mismatch.
- The first ``num_tests`` iterations are bit-identical to an unaugmented run (augmented
  vectors append; they never perturb the seeded RNG stream), so results remain
  comparable across runs with and without augmentation.
- Accepted vectors are recorded in ``tb/augmented_vectors.json`` for provenance, since
  a model re-run may propose different ones.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .analyze import AnalysisResult, FunctionArg
from .run_control import RunBudgetExceeded
from .llm import LLMClient, build_stimulus_prompt, extract_json_payload

STIMULUS_AUGMENT_POLICY_ID = "shift_left_stimulus_augment_v1"
AUGMENTED_VECTORS_FILENAME = "augmented_vectors.json"
MAX_AUGMENTED_VECTORS = 8

# Keep integer literals comfortably inside long long so the generated tables compile
# everywhere; the exact 2**63 boundary values are rejected rather than special-cased.
_INT_LIMIT = 2**63 - 2


def _is_float_type(c_type: str) -> bool:
    return "float" in c_type or "double" in c_type


def _coerce_number(value: object, c_type: str) -> int | float | None:
    if isinstance(value, bool):
        return None
    if _is_float_type(c_type):
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
        return None
    if isinstance(value, int) and abs(value) <= _INT_LIMIT:
        return value
    return None


def _required_args(analysis: AnalysisResult) -> list[FunctionArg]:
    """Every argument a vector must supply: all scalars, plus input/inout arrays.

    Output-only arrays are excluded on purpose -- the testbench sentinel-fills them for
    every test, augmented ones included, so a missed write stays visible.
    """

    required: list[FunctionArg] = []
    for arg in analysis.function.args:
        if arg.is_pointer_like and arg.direction == "output":
            continue
        required.append(arg)
    return required


def validate_vectors(
    analysis: AnalysisResult,
    payload: object,
) -> tuple[list[dict[str, object]], list[str]]:
    """(accepted vectors, per-rejection reasons). A vector is all-or-nothing: one bad
    value rejects the vector, never silently patches it."""

    if not isinstance(payload, list):
        return [], ["model response carried no JSON array of vectors"]
    required = _required_args(analysis)
    accepted: list[dict[str, object]] = []
    rejections: list[str] = []
    for index, entry in enumerate(payload):
        if len(accepted) >= MAX_AUGMENTED_VECTORS:
            rejections.append(f"vector {index}: over the cap of {MAX_AUGMENTED_VECTORS}; dropped")
            continue
        if not isinstance(entry, dict):
            rejections.append(f"vector {index}: not an object")
            continue
        vector: dict[str, object] = {}
        problem: str | None = None
        for arg in required:
            value = entry.get(arg.name)
            if arg.is_pointer_like:
                if not isinstance(value, list) or len(value) != (arg.length or 0):
                    problem = f"array {arg.name!r} must carry exactly {arg.length} values"
                    break
                elements = [_coerce_number(item, arg.c_type) for item in value]
                if any(item is None for item in elements):
                    problem = f"array {arg.name!r} carries a non-numeric or out-of-range value"
                    break
                vector[arg.name] = elements
            else:
                scalar = _coerce_number(value, arg.c_type)
                if scalar is None:
                    problem = f"scalar {arg.name!r} is missing, non-numeric, or out of integer range"
                    break
                if arg.scalar_range is not None:
                    lo, hi = arg.scalar_range
                    if not (lo <= scalar <= hi):
                        problem = f"scalar {arg.name!r}={scalar} outside declared range [{lo}, {hi}]"
                        break
                vector[arg.name] = scalar
        if problem is not None:
            rejections.append(f"vector {index}: {problem}")
            continue
        accepted.append(vector)
    return accepted, rejections


def propose_augmented_vectors(
    analysis: AnalysisResult,
    llm: LLMClient,
) -> tuple[list[dict[str, object]], list[str]]:
    """Ask the model for extra directed vectors and validate them against the contract."""

    original_source = analysis.function.source_path.read_text(encoding="utf-8")
    system, user = build_stimulus_prompt(analysis, original_source, MAX_AUGMENTED_VECTORS)
    try:
        payload = extract_json_payload(llm.complete(system, user))
    except RunBudgetExceeded:
        raise  # the caller owns budget policy; only llm_calls exhaustion is recoverable
    except Exception as exc:  # noqa: BLE001 -- optional augmentation, never fatal
        return [], [f"model call failed: {type(exc).__name__}: {exc}"]
    return validate_vectors(analysis, payload)


def write_provenance(
    project_dir: Path,
    vectors: list[dict[str, object]],
    rejections: list[str],
    model: str | None,
) -> Path:
    payload = {
        "policy_id": STIMULUS_AUGMENT_POLICY_ID,
        "model": model,
        "accepted": vectors,
        "rejected": rejections,
        "note": (
            "Accepted vectors are appended to tb/testbench.cpp after the deterministic "
            "tests as constant tables; both the golden oracle and the HLS top receive "
            "the same values."
        ),
    }
    path = project_dir / "tb" / AUGMENTED_VECTORS_FILENAME
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
