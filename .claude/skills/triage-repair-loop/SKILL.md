---
name: triage-repair-loop
description: Diagnose why a c2hlsc-agent repair loop is stuck or looping. Reads a project's repair_audit.json, the oscillation-guard history, and the failing-phase evidence, then reports the blocking cause and the next action. Use when a convert/repair run exhausts its iterations, keeps re-proposing the same fix, reports oscillation_rejected, or ends blocked.
---

# Triage a stuck repair loop

Answer one question: **why did this repair loop stop making progress, and what should be
tried next?** The evidence is spread across several files — gather it all before concluding.

## 1. Locate the project

The target is a generated project directory (the `convert`/`repair` output dir), containing
`src/hls_top.cpp` and usually `repair_audit.json`. If the user didn't name one, look under
`build/` for the most recently modified candidate and confirm before proceeding. If there is
no `repair_audit.json`, the loop never ran an LLM repair — say so and check whether the run
was blocked before repair (toolchain unavailable, host equivalence failing at iteration 0).

## 2. Read the audit trail

`repair_audit.json` is a list of `RepairOutcome` records
(`c2hlsc_agent/hlsc_repair_agent.py`). Per iteration, extract:

- `iteration`, `stage` (which ladder phase failed), `family` (classifier bucket)
- `status` — the key field. Watch for:
  - `oscillation_rejected` — the model re-proposed a source state already visited (sha256
    guard). Repeated occurrences mean it's cycling between two fixes.
  - `blocked` families (`toolchain_unavailable`, `local_hls_backend`) — **not** a code
    defect. The loop deliberately does not mutate host-equivalent HLS-C for these.
  - `unchanged`/no `changes` — the model returned the same file or an unparsable one.
- `summary`, `next_action`, `repair_scope`
- `changes[].diff` — what actually changed, and `before_sha256`/`after_sha256`

## 3. Look for the failure patterns

- **Oscillation (A→B→A)**: two or more iterations whose `after_sha256` values repeat, or
  explicit `oscillation_rejected` statuses. The model is bouncing between two fixes because
  each one breaks what the other fixed. Read both diffs and identify the conflicting
  constraint — usually an interface/bitwidth requirement fighting a behavioral one.
- **Same family, no progress**: several iterations sharing one `family` with different diffs
  and identical `stage` failures. The fix class is wrong for the actual defect; check whether
  `classify_failure` (`agent_loop.py`, regex-based) mis-bucketed the log.
- **Evidence starvation**: `evidence_excerpt` is a tool banner rather than a real error.
  Evidence is tail-sliced to the last 4000 chars (`llm._EVIDENCE_LIMIT`) precisely because
  the signature is at the end — if it still looks useless, the underlying phase log
  (`<project>/*.log`, `sim/report/`) is where the real signal is.
- **Blocked, not broken**: a `local_hls_backend` or `toolchain_unavailable` family means the
  backend failed on the *golden* C, so the HLS-C is not at fault. Report it as an
  environment/backend issue, never as a repair failure.

## 4. Cross-reference the prompt context

`_llm_repair` shows the model only the **last 3** outcomes (`llm._history_section`) plus up
to 3 retrieved audit-memory cards (`audit_memory.retrieve_cards`, opt-in via
`--audit-memory`). If the loop ran more than 3 iterations, the model can no longer see its
earliest attempts — a fix rejected at iteration 1 can legitimately reappear at iteration 5,
where only the sha256 guard (which does check *all* history) stops it. Note when this window
is the actual cause of an apparent repeat.

If `--audit-memory` was on, check whether a retrieved card is pulling the model toward a fix
that doesn't fit this failure family — that's a known way to get stuck.

## 5. Report

Produce, in this order:

1. A per-iteration table: iteration, stage, family, status, one-line summary of the change.
2. The single most likely blocking cause, stated plainly in a short paragraph.
3. One concrete next action, chosen from what the evidence supports — e.g. re-run with
   `--audit-memory` off; widen or narrow `repair_scope`; fix the environment (Vitis/Bambu)
   because the failure is `blocked`; hand-edit the conflicting constraint the oscillation
   revealed; or raise `--max-iterations` if the loop was genuinely converging when it ran out.

Do not propose changes that would weaken the oscillation guard or route the golden `input.c`
into the repair prompt — both are deliberate invariants (see `CLAUDE.md`).
