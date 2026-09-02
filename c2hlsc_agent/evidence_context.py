"""Distilled, prioritized failure evidence for the LLM repair prompts.

The raw repair evidence (``hlsc_repair_agent._phase_evidence``) concatenates
summary + stdout + stderr + the whole phase log; ``equivalence.run_command``
already writes stdout and stderr INTO that log, so the same text is counted
roughly twice, and a blind tail slice then favours whatever the tool printed
last (Vitis scheduling chatter) over what the testbench printed first
(mismatch traces).

This module reads the evidence once, normalizes it (CRLF, absolute paths ->
``<path>/basename``), drops tool-banner/chatter and repeated lines, and emits
sections in priority order under one character budget:

1. distilled mismatch records, with a failing-output rollup,
2. an error-anchored log window (``_ANCHOR_PATTERNS`` picks the anchor),
3. a tail slice as the last resort when nothing anchors.

The mechanical repairs in ``hlsc_repair_agent`` must keep receiving the RAW
evidence: they regex-scan for symbol names and file paths, which the
normalization here would silently break. Only the LLM prompt and the audit
ledger consume the bundle.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from .cosim_verdict import COSIM_FAILURE_MARKERS
from .equivalence import Mismatch, VerificationState, format_mismatch, parse_mismatches

EVIDENCE_BUDGET = 4000  # chars; matches the historical prompt/report truncation
_MAX_MISMATCH_LINES = 12
_MISMATCH_BUDGET_FRACTION = 0.5  # the mismatch section may take at most half the budget
_CONTEXT_BEFORE_ANCHOR = 5  # log lines kept before the anchor line
_MAX_LINE_REPEATS = 3  # identical non-consecutive lines emitted at most this often
_MIN_WINDOW_BUDGET = 80  # below this, a log window is not worth emitting

# The HLS_NL driver testbenches print a second mismatch shape that
# equivalence.parse_mismatches does not know: no arg/index, no seed.
_FIELD_MISMATCH = re.compile(
    r"Mismatch test=(?P<test>\d+)\s+field=(?P<field>\S+)\s+"
    r"expected=(?P<expected>\S+)\s+actual=(?P<actual>\S+)"
)

# Priority-ordered anchors: the first kind with a match wins and the log window
# starts just before its earliest matching line. Mismatch traces outrank
# everything because the generated testbenches print them near the START of a
# CSim/CoSim log -- exactly what a tail slice loses.
_ANCHOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mismatch", re.compile(r"\bMismatch\s+test=")),
    ("test_failure", re.compile(r"(?i)\btest(?:bench)?\s+failed\b")),
    ("assertion", re.compile(r"(?i)\bassert(?:ion)?\b.*\bfailed\b")),
    # gcc/clang/apcc diagnostics from the CSim compile step. Lower-case on
    # purpose: Vitis' own 'ERROR: [...]' lines are the vitis_error kind below,
    # and the first compiler error is the root cause the model needs.
    (
        "compile_error",
        re.compile(r"\b(?:error|fatal error):|\bundefined reference\b|\bunresolved external\b"),
    ),
    (
        "cosim_fail",
        re.compile(
            "(?i)"
            + "|".join(re.escape(marker) for marker in COSIM_FAILURE_MARKERS)
            + r"|\bERROR:\s*\[COSIM\b"
        ),
    ),
    ("vitis_error", re.compile(r"^\s*ERROR:|\breturned error code\b")),
    ("timeout", re.compile(r"(?i)\btimed?\s?out\b|\btimeout\b")),
    ("generic_error", re.compile(r"(?i)\bfail(?:ed|ure)?\b|(?<![0-9] )\berrors?\b")),
)

# Tool banner and informational chatter. A matching line is dropped unless
# _NOISE_KEEP rescues it (e.g. "INFO: [SIM 211-100] 'csim_design' failed").
# Banner lines are indented ('    ** Copyright ...'), hence the leading \s*.
_NOISE_LINE = re.compile(
    r"^\s*(?:"
    r"\*{2,}.*"  # '****** Vitis HLS ...', '**** SW Build ...', '** Copyright ...'
    r"|={4,}|-{4,}|_{4,}"  # separator rules
    r"|source\s+\S+hls\.tcl.*"  # tcl bootstrap line
    r"|Sourcing\s.*"
    r"|INFO:.*"
    r"|Running\s+vitis_hls.*"
    r"|For\s+(?:technical\s+support|more\s+information).*"
    r")$"
)
_NOISE_KEEP = re.compile(
    r"(?i)error|fail|fatal|mismatch|timed?\s?out|timeout|violat|unsupported|cannot|undefined|assert"
)

_REL_PARENT_CHAIN = re.compile(r"(?:\.\./)+")
_WIN_DIR = re.compile(r"[A-Za-z]:[\\/](?:[^\\/\s:'\"]+[\\/])+")
_POSIX_DIR = re.compile(r"(?<![\w<>])/(?:[^/\s:'\"]+/)+")


@dataclass(frozen=True)
class EvidenceBundle:
    text: str
    sections: tuple[str, ...]
    anchor_kind: str | None
    mismatch_count: int
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        """Provenance for the audit ledger; the text itself is evidence_excerpt."""

        return {
            "sections": list(self.sections),
            "anchor_kind": self.anchor_kind,
            "mismatch_count": self.mismatch_count,
            "truncated": self.truncated,
        }


EMPTY_BUNDLE = EvidenceBundle(text="", sections=(), anchor_kind=None, mismatch_count=0, truncated=False)


def build_repair_evidence(
    state: VerificationState,
    phase: str | None,
    limit: int = EVIDENCE_BUDGET,
) -> EvidenceBundle:
    """Distill the failing phase's evidence for the LLM repair prompt.

    Reads the phase log once; when it exists it supersedes stdout/stderr, which
    ``run_command`` already copied into it (the double-count in the raw path).
    """

    if phase is None:
        return EMPTY_BUNDLE
    result = state.phases.get(phase)
    if result is None:
        return EMPTY_BUNDLE
    text = ""
    if result.log_path and result.log_path.exists():
        try:
            text = result.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if not text.strip():
        text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return distill_evidence(
        text,
        summary=result.summary,
        mismatches=state.mismatches,
        phase=phase,
        limit=limit,
    )


def distill_evidence(
    text: str,
    summary: str = "",
    mismatches: Sequence[Mismatch] = (),
    phase: str | None = None,
    limit: int = EVIDENCE_BUDGET,
) -> EvidenceBundle:
    """Distill arbitrary failure text (plus known mismatches) into a bundle."""

    limit = max(200, int(limit))
    truncated = False
    sections: list[str] = []
    parts: list[str] = []

    header = " ".join((summary or "").split())
    if header:
        label = f"[summary {phase}]" if phase else "[summary]"
        parts.append(f"{label} {_normalize_paths(header)}")
        sections.append("summary")

    merged = _merge_mismatches(mismatches, _parse_all_mismatches(text or ""))
    if merged:
        block, cut = _mismatch_block(merged, int(limit * _MISMATCH_BUDGET_FRACTION))
        parts.append(block)
        sections.append("mismatches")
        truncated = truncated or cut

    lines = _clean_lines(text or "")
    anchor_kind: str | None = None
    if lines:
        anchor_kind, anchor_idx = _find_anchor(lines)
        remaining = limit - sum(len(part) + 2 for part in parts)
        if remaining < _MIN_WINDOW_BUDGET:
            truncated = True
        elif anchor_kind is not None:
            block, cut = _window_block(lines, anchor_idx, anchor_kind, remaining)
            parts.append(block)
            sections.append("anchored_window")
            truncated = truncated or cut
        else:
            block, cut = _tail_block(lines, remaining)
            parts.append(block)
            sections.append("tail")
            truncated = truncated or cut

    out = "\n\n".join(parts).strip()
    if len(out) > limit:
        out = out[:limit]
        cutpoint = out.rfind("\n")
        if cutpoint > limit // 2:
            out = out[:cutpoint]
        out = out.rstrip()
        truncated = True
    return EvidenceBundle(
        text=out,
        sections=tuple(sections),
        anchor_kind=anchor_kind,
        mismatch_count=len(merged),
        truncated=truncated,
    )


def _parse_all_mismatches(text: str) -> list[Mismatch]:
    found = parse_mismatches(text)
    for match in _FIELD_MISMATCH.finditer(text):
        found.append(
            Mismatch(
                test_index=int(match.group("test")),
                argument=match.group("field"),
                expected=match.group("expected"),
                actual=match.group("actual"),
            )
        )
    return found


def _merge_mismatches(
    primary: Sequence[Mismatch], parsed: Sequence[Mismatch]
) -> list[Mismatch]:
    merged: list[Mismatch] = []
    seen: set[tuple[object, ...]] = set()
    for mismatch in (*primary, *parsed):
        key = (
            mismatch.test_index,
            mismatch.argument,
            mismatch.element_index,
            mismatch.expected,
            mismatch.actual,
            mismatch.seed,
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(mismatch)
    return merged


def _mismatch_block(merged: Sequence[Mismatch], budget: int) -> tuple[str, bool]:
    counts = Counter(mismatch.argument for mismatch in merged)
    rollup = ", ".join(
        f"{name} ({count})" for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    header = f"[mismatches] {len(merged)} recorded; failing outputs: {rollup}"
    shown = [format_mismatch(mismatch) for mismatch in merged[:_MAX_MISMATCH_LINES]]
    cut = len(merged) > len(shown)

    def render(rows: list[str]) -> str:
        omitted = len(merged) - len(rows)
        tail = [f"... ({omitted} more mismatch(es) omitted)"] if omitted else []
        return "\n".join([header, *rows, *tail])

    block = render(shown)
    while shown and len(block) > budget:
        shown.pop()
        block = render(shown)
        cut = True
    return block, cut


def _clean_lines(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    counts: dict[str, int] = {}
    run_line: str | None = None
    run_count = 0
    dropped_repeats = 0

    def flush() -> None:
        nonlocal run_line, run_count
        if run_line is None:
            return
        out.append(run_line)
        if run_count > 1:
            out.append(f"    (previous line repeated {run_count - 1} more time(s))")
        run_line = None
        run_count = 0

    for raw in normalized.split("\n"):
        line = _normalize_paths(raw.rstrip())
        if _NOISE_LINE.match(line) and not _NOISE_KEEP.search(line):
            continue
        if not line:
            if run_line == "":
                continue
            flush()
            run_line, run_count = "", 1
            continue
        if line == run_line:
            run_count += 1
            continue
        flush()
        seen = counts.get(line, 0)
        if seen >= _MAX_LINE_REPEATS:
            dropped_repeats += 1
            continue
        counts[line] = seen + 1
        run_line, run_count = line, 1
    flush()
    if dropped_repeats:
        out.append(f"    ({dropped_repeats} additional repeated line(s) dropped)")
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def _normalize_paths(text: str) -> str:
    text = _REL_PARENT_CHAIN.sub("<path>/", text)
    text = _WIN_DIR.sub("<path>/", text)
    return _POSIX_DIR.sub("<path>/", text)


def _find_anchor(lines: Sequence[str]) -> tuple[str | None, int]:
    for kind, pattern in _ANCHOR_PATTERNS:
        for idx, line in enumerate(lines):
            if pattern.search(line):
                return kind, idx
    return None, -1


def _take_lines(lines: Sequence[str], start: int, budget: int) -> tuple[list[str], bool]:
    taken: list[str] = []
    used = 0
    for line in lines[start:]:
        cost = len(line) + 1
        if used + cost > budget:
            return taken, True
        taken.append(line)
        used += cost
    return taken, False


def _window_block(
    lines: Sequence[str], anchor_idx: int, anchor_kind: str, budget: int
) -> tuple[str, bool]:
    start = max(0, anchor_idx - _CONTEXT_BEFORE_ANCHOR)
    head = f"[log window; anchor={anchor_kind}]"
    if start > 0:
        head += f"\n... ({start} earlier line(s) omitted)"
    body, cut = _take_lines(lines, start, budget - len(head) - 48)
    if cut:
        omitted = len(lines) - start - len(body)
        body.append(f"... ({omitted} later line(s) omitted)")
    return head + "\n" + "\n".join(body), cut


def _tail_block(lines: Sequence[str], budget: int) -> tuple[str, bool]:
    head = "[log tail]"
    kept: list[str] = []
    used = len(head) + 48
    for line in reversed(lines):
        cost = len(line) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    cut = len(kept) < len(lines)
    if cut:
        head += f"\n... ({len(lines) - len(kept)} earlier line(s) omitted)"
    return head + "\n" + "\n".join(kept), cut
