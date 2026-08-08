## Work item

- Closes:
- Primary owner:
- Reviewer:
- Integrator:
- Branch:

## Scope

### Desired outcome

<!-- State the result this pull request delivers. -->

### Acceptance criteria

- [ ] Criterion from the linked issue
- [ ] Criterion from the linked issue

### Non-goals

<!-- State what intentionally remains unchanged. -->

### Overlap check

- Related issues or pull requests:
- Shared files reviewed with:

## Changes

<!-- Summarize the implementation and list the important files. -->

## Evidence

Use only the tiers relevant to this change. Record pass, fail, blocked, or skipped and link logs or artifacts when available.

| Verification tier | Status | Command, artifact, or result |
| --- | --- | --- |
| Offline unit tests |  |  |
| Git diff check |  |  |
| Host equivalence |  |  |
| Vitis CSim |  |  |
| Vitis CSynth |  |  |
| Vitis C/RTL CoSim |  |  |
| Direct RTL testbench |  |  |
| Dataset evaluation |  |  |
| QoR measurement |  |  |

### Bounded-run handoff

- Controller state: not applicable / active / blocked / exhausted
- Attempt budget and checkpoint:
- Source/failure fingerprint or cycle:
- Sanitized log or dead-letter artifact:
- Required human decision and next owner:

## Evidence integrity

- [ ] The original C remains the golden reference.
- [ ] Generated or LLM-proposed code is treated as a candidate, not an oracle.
- [ ] The offline test path does not require Anthropic, another provider, or network access.
- [ ] Optional checks are reported truthfully as pass, fail, blocked, or skipped.
- [ ] Any Vitis CoSim claim is limited to generated HLS-C versus its synthesized RTL.
- [ ] Any direct original-C-to-RTL claim has separate direct-RTL evidence.
- [ ] Any QoR claim includes the target, toolchain, constraints, and report artifact.
- [ ] No credentials, secrets, machine-specific tool paths, or generated build output are committed.
- [ ] Every retry loop has finite attempts, timeouts, worker caps, checkpoints, and repeated-state handling where applicable.

## Sync and handoff

- Latest main commit integrated:
- Unresolved decisions:
- Reviewer should focus on:
- Safe next action:

## Integration guardrails

- [ ] Stable `ci` check passed.
- [ ] A non-author approval covers the latest reviewable push.
- [ ] Review conversations are resolved.
- [ ] `python scripts/verify_github_guardrails.py` passed.
- [ ] Auto-merge remains disabled; no protection bypass is requested.
