# Contributing to c2hlsc-agent

GitHub is the coordination and synchronization layer for this repository. Each contributor works in a separate clone or Git worktree. Accepted changes move through a structured issue, one owned branch, a draft pull request, review, CI, and the protected `main` branch.

Do not share a mutable checkout through Dropbox, OneDrive, a network drive, or another live file-sync service. A background job may safely run `git fetch origin --prune` and notify you, but it must not automatically pull into an active working tree.

## Team responsibilities

Rotate these responsibilities between the three contributors:

- **Owner:** claims one issue, implements it on one branch, maintains the draft PR, and produces evidence and a handoff.
- **Reviewer:** checks scope, correctness, tests, evidence integrity, provider boundaries, and overlap with other work.
- **Integrator:** confirms review, CI, and resolved conversations, then performs the permitted merge. The reviewer and integrator may be the same person.

One person owns an issue at a time. Parallel implementation requires explicit subissues with dependencies and non-overlapping files or interfaces.

## One-time contributor setup

```text
git clone https://github.com/huluk98/c2hlsc-agent.git
cd c2hlsc-agent
git config user.name 'Your Name'
git config user.email 'your-address@example.com'
gh auth login
python -m pip install -r requirements.txt
python -m pip install -e .
python -m unittest discover -s tests
```

Read `AGENTS.md`, this file, `PEER_COMMANDS.md`, `PEER_COLLABORATION_TRAINING.md`, and the README sections related to your task. Do not install the optional Anthropic SDK merely to make ordinary CI pass; the offline deterministic and fallback paths are intentional test coverage.

Maintainers should protect `main` by requiring a pull request, one approval from someone other than the owner, passing CI, resolved conversations, and no force pushes or branch deletion. Prefer squash merges.

## Define and claim work

Start from the pinned [Team Work Queue #12](https://github.com/huluk98/c2hlsc-agent/issues/12). Each Team work item is a separate linked sub-issue, receives the team-work and status:todo labels, and remains the canonical record for its scope and handoff.

Create a **Team work item** issue before implementation. Record:

- the observable outcome and acceptance criteria;
- non-goals;
- expected files, APIs, generated formats, or datasets;
- required evidence tier;
- optional LLM, Vitis, simulator, SSH, dataset, or QoR requirements;
- dependencies and possible overlap; and
- one owner.

Search open work before claiming:

```text
git status --short --branch
git fetch origin --prune
gh issue list --state open
gh pr list --state open
git branch --remotes
```

Issue assignment is the ownership lock. Add a claim comment containing the branch name, short plan, and expected files, then replace status:todo with status:in-progress. Use status:review when the draft PR is ready for review and status:blocked only with a named blocker. If another owner or PR already covers the behavior, stop and agree on a split or sequence.

## Start an owned branch

Only after assignment and a clean preflight:

```text
git switch main
git pull --ff-only origin main
git switch -c work/<issue>-<github-user>-<slug>
git push -u origin HEAD
```

Never commit implementation work to `main`. Never reuse another contributor's branch unless the issue records an explicit handoff.

## Implement and publish checkpoints

- Stay inside the issue acceptance criteria and non-goals.
- Update the issue before expanding scope or changing expected files.
- Make small commits that mention `#<issue>`.
- Push meaningful checkpoints so the team can see and recover the work.
- Open a draft PR after the first meaningful commit and include `Closes #<issue>`.
- Do not commit secrets, provider credentials, licensed-tool credentials, local Vitis paths, ignored build outputs, or machine-specific result folders.

## Verification tiers

Every PR must run the offline CI-equivalent checks:

```text
python -m unittest discover -s tests
git diff --check
```

The full ordinary suite must pass without the optional Anthropic SDK, API keys, or network access.

Additional evidence depends on the issue:

1. **Host software equivalence:** required when generated HLS-C behavior, testbench generation, or equivalence handling changes and `g++` plus `make` are available.
2. **Vitis CSim, CSynth, and C/RTL CoSim:** run only when requested and a licensed Vitis installation or declared remote Vitis host is available.
3. **Direct RTL simulation:** separate from Vitis CoSim; use it only when the issue covers the standalone Verilog testbench or synthesized RTL flow.
4. **Dataset batches:** record scope, record IDs, resume state, workers, timeouts, and result artifact locations. Do not generalize a sampled run to the full corpus.
5. **QoR or PPA:** attach fresh reports and record tool, version, target part, clock, constraints, and comparison baseline.

Use the exact statuses emitted by repository reports. An intentionally unrequested Vitis phase may be `skipped`; a required downstream phase may be `blocked` after an earlier failure; an executed or requested failed check remains `fail`. Only fresh successful evidence is `pass`.

The evidence chain matters:

- host equivalence compares original golden C with generated HLS-C;
- Vitis CoSim compares generated HLS-C with generated RTL;
- direct RTL tests exercise the synthesized RTL under their declared interface contract; and
- none of those tiers should be silently substituted for another.

LLM-generated or repaired code is a proposal. Record the backend and deterministic fallback when relevant, but never treat model output as verification evidence.

## Synchronize safely

Fetch before deciding what to apply:

```text
git status --short --branch
git fetch origin --prune
```

Fast-forward a clean local `main`:

```text
git switch main
git pull --ff-only origin main
```

For a published feature branch, inspect incoming commits and then merge accepted `main` changes without rewriting history:

```text
git switch work/<issue>-<github-user>-<slug>
git merge origin/main
```

If conflicts appear, identify the issue and owner for each side before resolving. Do not use reset, forced checkout, automatic stash, or force-push to hide a coordination conflict.

A push does not alter another person's files. A merge updates shared `origin/main`; `fetch` discovers it; a clean fast-forward pull applies it locally.

## Review, merge, and handoff

The reviewer checks issue alignment, duplicate work, architecture boundaries, tests, evidence, optional-provider behavior, Vitis and RTL claims, secrets, generated artifacts, and outstanding risks. The owner addresses feedback on the same branch. The integrator squash-merges only after required approval, CI, and resolved conversations.

Every owner ends with:

```text
Work item: #<issue> - <title>
Owner: @<github-user>
Branch: <branch>
Commit: <sha>
Pull request: <url or not opened>

Scope completed:
- <behavior>

Files changed:
- <path>: <reason>

Checks and evidence:
- Offline unit tests: <pass|fail|blocked|skipped> - <summary>
- git diff --check: <pass|fail|blocked|skipped> - <summary>
- Host equivalence: <status> - <artifact or reason>
- Vitis CSim/CSynth/CoSim: <statuses> - <artifacts or reason>
- Direct RTL, dataset, or QoR: <statuses> - <artifacts or reason>

Coordination:
- origin/main incorporated through: <sha>
- Overlapping issues or PRs: <none or links>
- Risks and unavailable tools: <none or details>
- Next action and owner: <exact action>
```

Never report `pass` for an unrun command or use a vague claim such as `tests look good` in place of output.
