"""Audit-memory knowledge base: repair-success cards from audited runs.

Implements the ``audit_memory_agent`` role declared in :mod:`agent_loop` as a live
module. Its declared stop condition — "No reference HLS, hidden labels, or manual
fixes enter prompt-facing memory" — is enforced structurally:

- Cards are distilled ONLY from :class:`RepairOutcome`-shaped audit entries, whose
  ``changes`` never include ``input.c`` or the testbench (the repair agents cannot
  rewrite them), so golden text cannot leak into cards.
- Promotion runs only for runs whose FUNCTIONAL status is ``pass`` (the pre-PPA
  ladder verdict): ``applied``/``applied_llm`` at append time means the patch was
  written, not that it worked. Within a passing run, the chain rule promotes only
  the LAST applied entry per ``(stage, family)`` chain — an earlier applied entry
  followed by another attempt on the same chain demonstrably did not clear it.
- ``oscillation_rejected`` / ``blocked`` / ``no_change`` / ``pass`` entries are never
  promoted.

The store is an append-only JSONL file (default ``~/.c2hlsc/audit_memory.jsonl``,
overridable via config ``audit_memory_path`` then the ``C2HLSC_AUDIT_MEMORY`` env
var). Loads tolerate torn lines (a killed writer must not poison the store) and
duplicates are dropped at retrieval time, so concurrent appends are merely
redundant, never corrupting. Retrieval is stdlib-only: exact ``family`` match
(``stage`` fallback), ranked by token overlap with the current evidence — fast and
local, per the no-paid-API preference.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

DEFAULT_STORE = Path("~/.c2hlsc/audit_memory.jsonl")
_PROMOTABLE = {"applied", "applied_llm"}
_SNIPPET_LIMIT = 600
_DIFF_SUMMARY_LIMIT = 800
_CARD_RENDER_LIMIT = 900
_SALIENT = re.compile(r"error|mismatch|undeclared|undefined|fail|timeout|assert", re.I)
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def resolve_store_path(config: object) -> Path:
    """Store path precedence: config ``audit_memory_path`` > env > default."""

    configured = getattr(config, "audit_memory_path", None)
    if configured:
        return Path(configured).expanduser()
    env = os.environ.get("C2HLSC_AUDIT_MEMORY")
    if env:
        return Path(env).expanduser()
    return DEFAULT_STORE.expanduser()


def _salient_snippet(evidence: str) -> str:
    """Re-extract the salient error lines: the raw excerpt is a TAIL slice whose head
    (often the root error) may already be cut, so cards keep matched lines instead."""

    lines = [line.strip() for line in (evidence or "").splitlines() if line.strip()]
    salient = [line for line in lines if _SALIENT.search(line)]
    picked = salient if salient else lines[-5:]
    return "\n".join(picked)[:_SNIPPET_LIMIT]


def card_from_outcome(outcome: Any, project: str, timestamp: str | None = None) -> dict[str, Any]:
    """Distill one promoted RepairOutcome into a prompt-facing success card."""

    diff_bits: list[str] = []
    for change in getattr(outcome, "changes", ()) or ():
        action = getattr(change, "action", "")
        if action:
            diff_bits.append(action)
        hunks = [line for line in getattr(change, "diff", "").splitlines() if line.startswith("@@")]
        diff_bits.extend(hunks[:4])
    changes = getattr(outcome, "changes", ()) or ()
    before = getattr(changes[0], "before_sha256", "") if changes else ""
    after = getattr(changes[-1], "after_sha256", "") if changes else ""
    family = getattr(outcome, "family", "unknown")
    iteration = getattr(outcome, "iteration", 0)
    card_id = hashlib.sha256(f"{project}|{iteration}|{family}|{after}".encode("utf-8")).hexdigest()[:16]
    return {
        "card_id": card_id,
        "family": family,
        "stage": getattr(outcome, "stage", None),
        "mechanism": "llm" if getattr(outcome, "status", "") == "applied_llm" else "mechanical",
        "evidence_snippet": _salient_snippet(getattr(outcome, "evidence_excerpt", "")),
        "diff_summary": "\n".join(diff_bits)[:_DIFF_SUMMARY_LIMIT],
        "target_files": list(getattr(outcome, "target_files", ()) or ()),
        "repair_scope": getattr(outcome, "repair_scope", ""),
        "next_action": getattr(outcome, "next_action", ""),
        "audited": True,
        "provenance": {
            "project": project,
            "iteration": iteration,
            "before_sha256": before,
            "after_sha256": after,
            "timestamp": timestamp or time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    }


def promotable_outcomes(repair_history: list[Any]) -> list[Any]:
    """The chain rule. Caller guarantees the run's functional status is ``pass``.

    For each ``(stage, family)`` chain, only the LAST entry can have preceded the
    verify that cleared the failure; an applied entry with a later same-chain entry
    demonstrably did not clear it and is not a success. Non-promotable statuses are
    filtered regardless of position.
    """

    last_index: dict[tuple[Any, Any], int] = {}
    for idx, outcome in enumerate(repair_history):
        last_index[(getattr(outcome, "stage", None), getattr(outcome, "family", None))] = idx
    picked = []
    for idx, outcome in enumerate(repair_history):
        if getattr(outcome, "status", "") not in _PROMOTABLE:
            continue
        if not (getattr(outcome, "changes", ()) or ()):
            continue
        if last_index[(getattr(outcome, "stage", None), getattr(outcome, "family", None))] == idx:
            picked.append(outcome)
    return picked


def load_cards(store_path: Path) -> list[dict[str, Any]]:
    """Load all cards; torn/garbage lines are skipped, never fatal."""

    if not store_path.exists():
        return []
    cards: list[dict[str, Any]] = []
    for line in store_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            card = json.loads(line)
        except ValueError:
            continue
        if isinstance(card, dict):
            cards.append(card)
    return cards


def append_cards(store_path: Path, cards: list[dict[str, Any]]) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with store_path.open("a", encoding="utf-8") as handle:
        for card in cards:
            handle.write(json.dumps(card, sort_keys=True) + "\n")


def promote_run(
    store_path: Path,
    repair_history: list[Any],
    project: str,
    functional_status: str,
) -> list[dict[str, Any]]:
    """Promote a finished convert run's audited successes into the store.

    ``functional_status`` must be the PRE-PPA ladder verdict: PPA is a QoR gate and
    never decides whether a repair functionally worked. Returns the freshly appended
    cards (deduplicated against the existing store by card_id).
    """

    if functional_status != "pass" or not repair_history:
        return []
    cards = [card_from_outcome(outcome, project) for outcome in promotable_outcomes(repair_history)]
    if not cards:
        return []
    existing = {card.get("card_id") for card in load_cards(store_path)}
    fresh = [card for card in cards if card["card_id"] not in existing]
    if fresh:
        append_cards(store_path, fresh)
    return fresh


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN.findall(text or "")}


def render_card(card: dict[str, Any]) -> str:
    header = (
        f"[{card.get('stage', '?')}/{card.get('family', '?')}] "
        f"mechanism={card.get('mechanism', '?')} files={', '.join(card.get('target_files', []) or []) or '?'}"
    )
    parts = [header]
    if card.get("evidence_snippet"):
        parts.append(f"failure evidence:\n{card['evidence_snippet']}")
    if card.get("diff_summary"):
        parts.append(f"fix applied:\n{card['diff_summary']}")
    return "\n".join(parts)[:_CARD_RENDER_LIMIT]


def retrieve_cards(
    store_path: Path,
    family: str | None,
    stage: str | None,
    evidence: str,
    limit: int = 3,
) -> list[str]:
    """Rendered cards for a failure: exact family match (stage fallback), ranked by
    evidence token overlap. Duplicate card_ids (concurrent appends) collapse here."""

    seen: set[Any] = set()
    unique: list[dict[str, Any]] = []
    for card in load_cards(store_path):
        card_id = card.get("card_id")
        if card_id in seen:
            continue
        seen.add(card_id)
        unique.append(card)
    matches = [card for card in unique if family and card.get("family") == family]
    if not matches and stage:
        matches = [card for card in unique if card.get("stage") == stage]
    if not matches:
        return []
    evidence_tokens = _tokens(evidence)
    matches.sort(
        key=lambda card: len(evidence_tokens & _tokens(card.get("evidence_snippet", ""))),
        reverse=True,
    )
    return [render_card(card) for card in matches[: max(0, limit)]]
