"""audit_memory_agent, live: audited repair successes promoted into retrieval memory.

The stop condition in the agent's declaration is the design constraint here: *no
reference HLS, hidden labels, or manual fixes enter prompt-facing memory*. Concretely:

- A card is promoted ONLY from a ``run_convert`` invocation whose full requested
  verification ladder finished ``pass`` -- the same audited-evidence bar as everything
  else in the pipeline. A repair applied by the standalone ``repair`` command is never
  promoted, because that command does not verify; its report explicitly says to rerun
  verification elsewhere.
- A card carries the failure family, stage, a truncated unified diff of the audited
  change, and provenance (model, timestamp, project basename, top function). It never
  carries the golden ``input.c``, dataset reference code, or full source files.
- Retrieval is bounded (top-``k`` by family/stage match, newest first) and only ever
  reaches a prompt through :func:`c2hlsc_agent.llm.build_repair_prompt`'s dedicated
  section, clearly labelled as context from OTHER runs.

Promotion is active only on LLM-enabled runs: the memory is prompt-facing, so a
deterministic offline run has nothing to gain from writing to it, and ordinary CI (which
exercises the offline path) must stay free of home-directory side effects. The store is
a JSONL file, default ``~/.c2hlsc/repair_cards.jsonl``, overridable with
``--memory-dir`` or ``C2HLSC_MEMORY_DIR``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .hlsc_repair_agent import RepairOutcome

MEMORY_FILENAME = "repair_cards.jsonl"
CARD_SCHEMA_VERSION = 1
_DIFF_LIMIT = 1200
_PROMOTABLE_STATUSES = {"applied", "applied_llm"}


def memory_path(config: object | None = None) -> Path:
    explicit = getattr(config, "memory_dir", None) if config is not None else None
    root = Path(explicit or os.environ.get("C2HLSC_MEMORY_DIR") or (Path.home() / ".c2hlsc"))
    return root.expanduser() / MEMORY_FILENAME


def _card_from_outcome(
    outcome: RepairOutcome,
    top: str,
    project_name: str,
    model: str | None,
    verified_scope: str,
) -> dict[str, object]:
    diff = "\n".join(change.diff for change in outcome.changes if change.diff)
    return {
        "schema_version": CARD_SCHEMA_VERSION,
        "family": outcome.family,
        "stage": outcome.stage,
        "kind": "llm" if outcome.status == "applied_llm" else "mechanical",
        "top": top,
        "summary": outcome.summary[:300],
        "target_files": list(outcome.target_files),
        "diff_excerpt": diff[:_DIFF_LIMIT],
        "model": model,
        "project": project_name,
        # What "verified" actually meant for this card: a host-equivalence-only run and
        # a full-ladder run are very different evidence, and the prompt must not imply
        # the stronger one when only the weaker happened.
        "verified_scope": verified_scope,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def promote_repair_cards(
    project_dir: Path,
    config: object,
    repairs: list[RepairOutcome],
    top: str,
    verified: bool,
    model: str | None = None,
) -> int:
    """Append one card per audited applied repair; returns how many were promoted.

    ``verified`` must mean the verification the run REQUESTED passed end to end AFTER
    these repairs were applied -- the caller asserts that; this function records the
    actual scope (``host_equivalence`` vs ``full_ladder``) on every card so the prompt
    can never imply stronger evidence than the run produced. With ``verified=False``,
    or memory disabled, nothing is touched (not even the memory directory, so a run
    that promotes nothing leaves no trace).
    """

    if not verified or not getattr(config, "use_repair_memory", True):
        return 0
    verified_scope = "full_ladder" if getattr(config, "run_vitis", False) else "host_equivalence"
    cards = [
        _card_from_outcome(outcome, top, project_dir.name, model, verified_scope)
        for outcome in repairs
        if outcome.status in _PROMOTABLE_STATUSES and outcome.changes
    ]
    if not cards:
        return 0
    path = memory_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for card in cards:
            handle.write(json.dumps(card, sort_keys=True) + "\n")
    return len(cards)


def load_cards(config: object | None = None) -> list[dict[str, object]]:
    """All stored cards, oldest first. Malformed lines are skipped, never fatal."""

    path = memory_path(config)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []  # an unreadable store is a degraded prompt, never a failed repair
    cards: list[dict[str, object]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            card = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(card, dict):
            cards.append(card)
    return cards


def relevant_cards(
    config: object | None,
    family: str,
    stage: str | None,
    limit: int = 2,
) -> list[dict[str, object]]:
    """Top-``limit`` cards for a failure, exact (family, stage) matches first, then
    same-family, newest first. Only LLM-applied cards are returned: a mechanical card
    describes a fix the deterministic pass will simply re-apply, so repeating it in a
    prompt is noise."""

    if not getattr(config, "use_repair_memory", True):
        return []
    cards = [card for card in load_cards(config) if card.get("kind") == "llm"]
    cards.reverse()  # newest first
    # Content-dedup so one repeated promotion cannot fill the whole retrieval window.
    seen: set[tuple] = set()
    unique: list[dict[str, object]] = []
    for card in cards:
        key = (card.get("family"), card.get("stage"), card.get("diff_excerpt"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(card)
    exact = [c for c in unique if c.get("family") == family and c.get("stage") == stage]
    same_family = [c for c in unique if c.get("family") == family and c not in exact]
    # Last tier: same stage, any family. Dogfooding showed the analyst can legitimately
    # reclassify the family between the run that stored a card and the run that needs
    # it, and a family-only key then misses cards for the same concrete situation.
    same_stage = [c for c in unique if c.get("stage") == stage and c not in exact and c not in same_family]
    return (exact + same_family + same_stage)[:limit]
