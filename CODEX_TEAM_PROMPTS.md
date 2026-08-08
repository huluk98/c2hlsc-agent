# Codex team prompts

These prompts work from a native Windows clone or an Ubuntu clone because they describe outcomes and let Codex select the platform command. Open the repository root in Codex first. Replace placeholders such as `ISSUE_NUMBER` and `PR_NUMBER`.

The repository skill `$coordinate-team-work` is the canonical workflow. Its three project agents support read-only coordination, bounded implementation, and read-only verification. The parent Codex task remains responsible for Git and GitHub writes, and no agent may recursively delegate.

## 1. Onboard a collaborator without changing anything

```text
Use $coordinate-team-work to onboard me to huluk98/c2hlsc-agent. Do not edit files or change GitHub state.

Read AGENTS.md and COLLABORATOR_START_HERE.md. Run the native-platform read-only preflight. Report the repository root, authenticated GitHub account, origin URL, branch, working-tree state, remote freshness, Python version, open pull requests, shared work queue, required evidence model, and exact commands I should use next.

Stop on a wrong repository, failed authentication, or unrelated local changes.
```

## 2. Implement one issue through a draft pull request

```text
Use $coordinate-team-work to implement GitHub issue #ISSUE_NUMBER in huluk98/c2hlsc-agent.

Outcome: SHORT_OUTCOME
Expected paths: EXPECTED_PATHS
Required evidence: EVIDENCE_TIERS

I authorize the normal issue-to-draft-PR workflow for the authenticated GitHub account: read-only preflight; claim the issue only if it is open, unassigned, and non-overlapping; create and push its owned work/<issue>-<user>-<slug> branch from clean current main; make focused edits; run required checks; commit and push only that branch; and open or update a draft PR with Closes #ISSUE_NUMBER.

Report the complete preflight before editing. Stop if another person owns the issue, the checkout is unsafe, another issue or PR overlaps, required evidence is unavailable, or scope must expand. Do not merge, enable auto-merge, rewrite history, reset, stash, force-checkout, force-push, edit another branch, weaken verification, or commit secrets and generated output.

Keep every loop bounded by finite attempts, timeouts, worker limits, checkpoints, and repeated-state detection. End with the repository handoff and one next owner.
```

## 3. Synchronize a clone without implementing

```text
Use $coordinate-team-work to synchronize this c2hlsc-agent clone safely. Do not edit source files, create commits, push, merge a PR, reset, stash, force-checkout, rebase published history, or force-push.

Inspect the working tree and unpushed commits, then fetch and prune origin. Report incoming and outgoing commits for the current branch and main.

If the checkout is clean and on main, fast-forward it only. If it is a feature branch, do not incorporate origin/main automatically; report incoming commits, likely overlap, and the exact safe command for that branch owner. End with commits applied, commits pending, risks, and one next action.
```

## 4. Review a teammate's pull request

```text
Use $coordinate-team-work to review pull request #PR_NUMBER in huluk98/c2hlsc-agent. Keep the operation read-only: do not edit, commit, push, approve, request changes, close, or merge.

Read the linked issue, AGENTS.md, the full diff, open conversations, CI, and evidence. Check overlap with open work. Verify issue alignment, correctness, security, golden-C independence, offline deterministic behavior, verification order, exact status vocabulary, retry and timeout bounds, auditability, secrets, machine-specific paths, generated artifacts, and missing tests.

Lead with actionable findings ordered by severity and exact file/line references. Then list questions, missing evidence, overlap risk, and one recommendation: request changes, ready after named fixes, or ready for human approval. Do not infer pass from absent output.
```

## 5. Prepare an exact handoff

```text
Use $coordinate-team-work to prepare the handoff for the current c2hlsc-agent issue and branch. Do not modify files or GitHub state.

Inspect git status, commits relative to origin/main, the linked issue and PR, changed paths, commands and output, artifacts, CI, reviews, and overlapping work. Return the exact handoff from COLLABORATOR_START_HERE.md: issue, owner, branch, commit, PR, scope, files, every evidence tier, origin/main freshness, overlap, risks, unavailable tools, and one exact next action with one owner.

Use pass, fail, blocked, skipped, or exhausted truthfully. Never fill a missing result by inference.
```

## 6. Convert a blocked or exhausted loop into a human decision

```text
Use $coordinate-team-work to inspect the blocked or exhausted controller run at RUN_PATH for issue #ISSUE_NUMBER. Preserve files and GitHub state until you report.

Read the durable state, immutable attempt budget, source and failure fingerprints, cycle history, latest candidate, sanitized logs, dead-letter record, and completed evidence. Classify the stop as blocked or exhausted and identify the one missing dependency or human decision.

Show the exact sanitized issue comment from the skill's controller-handoff reference. Ask before posting it or changing labels unless this prompt explicitly says: POST THE HANDOFF. Do not silently increase retries, reset counters, erase history, restart from an older source, or expose secrets.
```

## 7. Integrate an approved pull request

```text
Use $coordinate-team-work to integrate pull request #PR_NUMBER in huluk98/c2hlsc-agent.

I authorize a squash merge and remote feature-branch deletion only if the PR targets main, the stable ci check is successful, at least one non-author approval covers the latest reviewable push, conversations are resolved, required evidence is present, and branch protection permits the merge without bypass. First report the exact checks, reviews, merge state, linked issue, and remaining risks.

If every condition is true, perform the squash merge, verify the PR and issue states, and give all collaborators the native Windows and Ubuntu post-merge synchronization commands. Otherwise do not merge; name the unmet condition and its owner. Never enable auto-merge.
```

## Prompt-writing rule for new work

A safe prompt names one issue, one desired outcome, expected paths, required evidence, allowed Git/GitHub writes, forbidden actions, stop conditions, and one handoff format. If those fields are missing, ask Codex for read-only preflight first instead of giving broad implementation authority.
