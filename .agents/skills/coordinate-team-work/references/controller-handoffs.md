# Controller handoffs

Use this when a bounded generation, repair, Vitis, RTL, dataset, or orchestration run ends in `blocked` or `exhausted`.

## Decision tree

1. Preserve the run directory, latest source, state snapshot, attempt counters, failure fingerprint, and sanitized log path.
2. If an external dependency is unavailable or a human decision is required, classify `blocked`.
3. If the immutable retry budget is consumed, a source/failure fingerprint repeats, or a cycle is detected, classify `exhausted`.
4. Do not silently reset counters, increase the budget, delete the dead-letter record, or restart from an earlier source.
5. Update the owning issue only when authorized. Replace its open status label with `status:blocked` and post the comment below.
6. A human chooses one action: provide the missing dependency, change scope and budget in the issue, create a new follow-up issue, accept a partial result, or close the work.

## Issue comment

```text
Controller handoff: BLOCKED_OR_EXHAUSTED

Run ID: RUN_ID
Terminal phase: PHASE
Last successful checkpoint: PATH_OR_NONE
Attempt budget: USED / LIMIT (unchanged on resume)
Source fingerprint: VALUE
Failure fingerprint: VALUE
Cycle or repeat detected: YES_NO_AND_DETAILS
Latest candidate/source: PATH
Sanitized log or dead-letter record: PATH
Evidence completed: STATUSES_AND_PATHS
Evidence not completed: STATUSES_AND_REASONS
Required human input: ONE_DECISION_OR_DEPENDENCY
Safe next action: EXACT_ACTION
Next owner: @GITHUB_USER
```

Do not paste secrets, licensed-tool credentials, provider prompts containing sensitive data, or machine-specific private paths. Attach a sanitized artifact or summarize it.
