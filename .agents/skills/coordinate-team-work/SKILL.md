---
name: coordinate-team-work
description: Coordinate safe multi-person work in c2hlsc-agent from onboarding and issue preflight through an owned branch, bounded implementation, review, handoff, merge, and synchronization. Use for starting or resuming GitHub issues, checking duplicate work, syncing a Windows or Ubuntu clone, preparing or reviewing a pull request, handling blocked or exhausted controller runs, or teaching collaborators the repository workflow.
---

# Coordinate Team Work

Use GitHub as the shared source of truth while keeping each contributor in an independent clone or worktree. Preserve one issue, one current owner, one branch, and one pull request for each independently reviewable outcome.

## Choose the operation

Classify the request before changing state:

- **Onboard or inspect:** read repository guidance, check tools and identity, and report state. Do not edit.
- **Synchronize:** fetch first; fast-forward only a clean `main`. Do not automatically alter a feature branch.
- **Start or resume implementation:** run collaboration preflight, confirm ownership and scope, then work only on the owned branch.
- **Review:** inspect the issue, full diff, CI, conversations, and evidence without editing or publishing.
- **Handoff:** report exact state and one next action without filling evidence gaps by inference.
- **Merge:** proceed only when the user explicitly asks, repository protection is satisfied, CI is green, a non-author approval exists, and conversations are resolved.

Read `AGENTS.md` first. Use `COLLABORATOR_START_HERE.md` for the complete human workflow and `CODEX_TEAM_PROMPTS.md` for copy-ready requests. Load the reference files in this skill only when their topic applies.

## Establish authority and state

Before implementation, obtain or inspect:

1. authenticated GitHub login and repository;
2. current branch, worktree status, remotes, and unpushed commits;
3. fresh remote refs or equivalent GitHub branch state;
4. issue title, body, state, assignee, labels, and comments;
5. open pull requests and related issues;
6. acceptance criteria, non-goals, expected paths and interfaces;
7. required host, Vitis, RTL, dataset, and QoR evidence; and
8. likely overlap and dependency order.

Report this preflight before editing. When available, use the project `coordination_explorer` agent for the read-only map. Do not let it recursively delegate.

Stop without editing when:

- the issue is unassigned or assigned to someone else;
- the checkout is `main`, detached, dirty with unrelated work, or on another owner's branch;
- another issue or PR covers the same behavior;
- shared files or interfaces lack an agreed sequence;
- the requested evidence cannot support the acceptance criteria; or
- a state-changing action needs authority the user did not grant.

Recommend a scope split, dependency, or recorded handoff instead of guessing.

## Claim and branch safely

Issue assignment is the ownership lock. Claim only when the user asked to implement, the issue is open and unassigned, and overlap is clear. Record the planned branch, paths, and evidence in one issue comment. Use exactly one status label:

- `status:todo`: unclaimed and ready;
- `status:in-progress`: owned implementation;
- `status:review`: published for review;
- `status:blocked`: a named dependency or human decision is required.

Create `work/<issue>-<github-user>-<slug>` only from a clean, current `main`. Push that branch only when publication is authorized. Never reuse another person's branch or implement on `main`.

## Implement inside a bounded envelope

Use the `bounded_implementer` agent only after preflight passes. Give it the issue, accepted plan, exact paths, non-goals, evidence tiers, and finite test or retry budget. The parent remains responsible for Git and GitHub writes.

During implementation:

- preserve unrelated changes and public behavior outside scope;
- preserve original C as the golden reference;
- keep the deterministic offline path independent of provider SDKs and credentials;
- treat generated and LLM-proposed code as candidates;
- add focused tests for changed behavior;
- use finite attempts, timeouts, worker caps, and persistent checkpoints for loops;
- stop on repeated source/failure fingerprints or an exhausted budget; and
- never hide a failed phase or unavailable tool.

Do not recursively spawn agents. The project limit is three concurrent spawned threads, but ordinary issue work should use the smallest number needed and terminate each role after its deliverable.

## Verify and publish

Every change requires the native-platform equivalent of:

```text
python -m unittest discover -s tests
git diff --check
git status --short --branch
git diff --stat
```

Run additional host, Vitis, direct RTL, dataset, or QoR checks only when the issue requires them. Record unavailable required evidence as `blocked`; record an intentionally out-of-scope tier as `skipped`; never call either one `pass`.

Stage only intended paths. Make focused commits and push only the owned branch. Open or update a draft pull request with `Closes #<issue>`, actual commands and outcomes, evidence paths, overlap, risks, and next owner. Move the issue to `status:review` after the draft exists.

## Review and integrate

Use `verification_reviewer` for a read-only evidence review. It does not replace the human GitHub approval. Address findings on the same branch and rerun affected checks.

Mark a draft ready only after self-review and required evidence. Merge only on explicit instruction and only when all branch rules pass:

- pull request targets `main`;
- stable `ci` check succeeds;
- at least one non-author approval exists;
- the latest reviewable push has teammate approval;
- conversations are resolved; and
- the branch is mergeable without bypassing protection.

Prefer squash merge. Never enable or rely on auto-merge for this workflow.

## Synchronize collaborators

A push changes only the remote feature branch. A merge changes remote `main`. Other machines update only after fetch and a clean fast-forward pull.

For exact native PowerShell and Bash commands, read [platform-commands.md](references/platform-commands.md). Do not reset, auto-stash, force-checkout, rebase published history, or force-push to make machines appear synchronized.

## Handle blocked and exhausted runs

When a controller or verification loop stops, preserve its checkpoint and immutable attempt budget. Classify the terminal state as `blocked` or `exhausted`, sanitize logs, and post a human handoff to the owning issue only when authorized. Do not restart with a larger hidden budget or erase fingerprint history.

Read [controller-handoffs.md](references/controller-handoffs.md) for the comment format and recovery decision tree.

## End every operation with a handoff

Report:

- issue, owner, branch, commit, and pull request;
- behavior and files changed;
- every check actually run and its exact result;
- evidence and artifact paths;
- remote-main freshness and overlap;
- blocked, exhausted, skipped, or unavailable work;
- unresolved risks; and
- one exact next action with one owner.

Never use 'done' or 'tests look good' in place of inspectable state.
