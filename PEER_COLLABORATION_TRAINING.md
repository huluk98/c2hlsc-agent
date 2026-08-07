# Training three contributors to work with Codex and GitHub

This guide gives a team lead a repeatable way to teach three people to collaborate on `c2hlsc-agent` without duplicating work, overwriting local changes, or overstating verification evidence.

The objective is not to make three machines behave like one shared folder. The objective is to give three independent contributors one visible queue, one owner per task, one integration path, and one evidence standard.

## The message to give your peers

Use this explanation at the beginning of the training:

> GitHub is our shared memory; issues say who owns what, branches hold work in progress, pull requests show what will change, and `main` contains accepted work. Your local clone is private working space. A push makes your branch visible, a merge changes shared `main`, a fetch discovers remote changes, and a clean fast-forward pull applies them locally. Codex must check ownership and overlap before editing. Verification evidence, not model confidence, decides whether work is complete.

Five rules are non-negotiable:

1. No issue and owner means no implementation edits.
2. One issue has one owner and one branch unless explicit subissues divide the work.
3. No direct implementation commits to `main`.
4. No claim of `pass` without fresh command output or an artifact.
5. No automatic pull, reset, stash, or force-push to hide coordination problems.

## Roles for a three-person team

Rotate the roles after each merged issue so everyone practices the complete workflow:

- **Person A, owner:** claims and implements the issue, maintains the draft PR, and writes the handoff.
- **Person B, reviewer:** checks scope, code, tests, evidence integrity, and overlap.
- **Person C, integrator:** verifies CI and review completion, performs the permitted merge, and leads post-merge synchronization.

The reviewer and integrator may be combined during ordinary work, but keep them separate during the first training exercise so all three people remain engaged.

## Before the training session

Each person should have:

- their own GitHub account with repository access;
- Git, Python 3.9 or newer, and GitHub CLI installed;
- a separate local clone, never a copied `.git` directory or cloud-synced checkout;
- an individual Git name and email;
- `gh auth status` reporting the correct account; and
- the offline unit suite passing after editable installation.

Baseline commands:

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

Do not distribute provider API keys or Vitis credentials during onboarding. Ordinary CI is deliberately offline. Optional LLM, Vitis, SSH, dataset, and QoR environments should be added only for people assigned to those evidence tiers.

## Suggested 90-minute session

| Time | Topic | Observable result |
| --- | --- | --- |
| 0–10 min | Shared mental model | Everyone can explain commit, push, merge, fetch, and pull. |
| 10–20 min | Repository evidence model | Everyone distinguishes golden-C host equivalence, Vitis CoSim, direct RTL simulation, and QoR. |
| 20–35 min | Issue definition and ownership | The team creates one scoped issue and assigns one owner. |
| 35–50 min | Codex preflight and branch | Codex reports status and overlap before the owner edits. |
| 50–65 min | Small implementation and draft PR | The owner pushes one focused commit and opens a draft PR. |
| 65–78 min | Review and evidence handoff | The reviewer checks the diff and evidence using the standard prompt. |
| 78–87 min | Merge and synchronization | The integrator merges; the other two safely fetch and fast-forward. |
| 87–90 min | Debrief | Each person names one anti-pattern and one required handoff field. |

## First supervised exercise

Use a small documentation or test-clarity change that cannot alter generated HLS behavior. The purpose is to practice coordination, not to test domain expertise.

### 1. Define the issue together

Use the **Team work item** form. Ask the group to reject vague wording. A ready issue has:

- one observable outcome;
- two or three checkable acceptance criteria;
- explicit non-goals;
- expected files;
- offline test requirements;
- optional evidence tiers marked in or out of scope;
- no unresolved overlap; and
- one assignee.

Example training issue:

```text
Outcome: Explain one existing CLI fallback more clearly in the README.

Acceptance criteria:
- The command and behavior match the current implementation.
- No runtime code changes.
- Offline unit tests pass.

Non-goals:
- No provider configuration changes.
- No Vitis or QoR claims.

Expected files:
- README.md
```

### 2. Owner performs preflight

The owner opens Codex in a clean clone and uses the task-start prompt later in this guide. Codex must report the authenticated operator, issue owner, branch, local status, remote freshness, open PR overlap, planned files, and next action before editing.

If Codex starts editing without this report, stop it and repeat the prompt. The habit matters more than speed during training.

### 3. Owner creates the branch and draft PR

```text
git switch main
git pull --ff-only origin main
git switch -c work/<issue>-<github-user>-<slug>
git push -u origin HEAD
```

After one meaningful commit, the owner pushes and opens a draft PR. The issue, branch, and PR must agree on scope and ownership.

### 4. Reviewer checks evidence, not confidence

The reviewer uses the review prompt later in this guide and verifies:

- the diff matches the issue and contains no unrelated cleanup;
- the explanation matches implementation and README behavior;
- offline tests actually ran without optional cloud dependencies;
- every claimed status has output or an artifact;
- optional Vitis, RTL, dataset, and QoR tiers are not falsely implied; and
- no open issue or PR duplicates the work.

For code changes, teach this evidence chain explicitly:

```text
original C --host equivalence--> generated HLS-C
generated HLS-C --Vitis CoSim--> generated RTL
generated RTL --direct testbench--> interface-specific RTL evidence
reports --matching tool and target context--> defensible QoR comparison
```

No arrow silently replaces another arrow. An LLM can propose code at any stage, but it does not become an evidence arrow.

### 5. Integrator merges and leads synchronization

The integrator confirms CI, approval, and resolved conversations, then performs the permitted squash merge. The other contributors run:

```text
git status --short --branch
git fetch origin --prune
git switch main
git pull --ff-only origin main
```

If someone has active feature work, they inspect incoming commits before merging `origin/main` into that feature branch. They do not automatically pull, rebase published history, or force-push.

### 6. Rotate roles

Create a second tiny issue and rotate owner, reviewer, and integrator. Repeat until each person has performed all three roles at least once.

The training is complete only when each person can demonstrate:

- a clean preflight before editing;
- issue ownership and a correctly named branch;
- a draft PR with concrete test evidence;
- a review that catches an unsupported claim; and
- a safe post-merge synchronization.

## Prompt 1: start, claim, implement, and open a draft PR

Replace every value in angle brackets.

```text
Coordinate and implement GitHub issue #<ISSUE> for @<GITHUB_USER> in c2hlsc-agent.

Goal: <SHORT OUTCOME>
Expected scope: <FILES OR COMPONENTS>
Required evidence tiers: <OFFLINE TESTS, HOST, VITIS, RTL, DATASET, OR QOR>

Follow AGENTS.md and CONTRIBUTING.md. First perform only a read-only collaboration preflight:

1. Identify the repository, authenticated GitHub user, default branch, current branch, working-tree state, remotes, and unpushed commits.
2. Fetch and prune origin without changing checked-out files.
3. Read issue #<ISSUE>, its assignee and comments, open PRs, and remote branches.
4. Compare acceptance criteria, planned behavior, files, APIs, generated formats, datasets, and evidence tiers for overlap.
5. Report operator, issue owner, branch, remote freshness, planned files, overlap risk, and next authorized action.

Do not edit if the issue belongs to someone else, overlap is unresolved, unrelated local changes exist, or the current branch is main.

If the issue is unassigned and preflight is clean, this prompt authorizes you to assign only issue #<ISSUE> to @<GITHUB_USER>, add one claim comment with the planned branch and files, fast-forward a clean main, and create and push work/<ISSUE>-<GITHUB_USER>-<SLUG>.

Implement only the issue acceptance criteria. Preserve unrelated changes. Keep the offline deterministic path working without provider SDKs, keys, or network access. Treat LLM output only as a proposal. Run the required checks and evidence tiers, make focused commits, push only the named branch, and open or update a draft PR linked with Closes #<ISSUE>.

Do not merge, force-push, rewrite history, edit another person's branch, discard changes, weaken verification, commit secrets or generated output, or claim unrun evidence.

End with the handoff format from CONTRIBUTING.md.
```

## Prompt 2: synchronize without implementing

```text
Synchronize this c2hlsc-agent clone safely with GitHub. Do not edit source files, commit, push, merge a PR, reset, stash, force-checkout, rebase published history, or force-push.

Read AGENTS.md and CONTRIBUTING.md. Inspect the working tree and unpushed commits, then fetch and prune origin. Report incoming and outgoing commits for the current branch and main.

If the checkout is clean and currently on main, you may fast-forward it only. If this is a feature branch, do not incorporate origin/main automatically. Report incoming commits, likely file overlap, and the exact safe merge command for the owner.

End with current branch, working-tree state, fetched remote state, commits applied, commits pending, overlap risk, and next action.
```

## Prompt 3: review a teammate's pull request

```text
Review pull request #<PR> for c2hlsc-agent. Do not edit or publish changes.

Read its linked issue, AGENTS.md, CONTRIBUTING.md, the relevant README sections, the complete diff, open review comments, and CI results. Check other open issues and PRs for overlap.

Verify golden-C independence, offline deterministic behavior, optional-provider fallback, verification order, exact report statuses, repair auditability, and truthful host, Vitis, RTL, dataset, and QoR claims. Confirm every claimed command or phase has output or an artifact and the diff stays inside scope. Look for secrets, machine-specific paths, and ignored generated outputs.

Return findings ordered by severity with exact file and line references. Then list questions, missing evidence, test coverage, overlap risk, and one recommendation: request changes, approve after named fixes, or ready for human approval. Do not approve or merge unless explicitly asked after presenting the review.
```

## Prompt 4: produce a handoff without changing state

```text
Prepare the required c2hlsc-agent handoff for the current issue and branch. Do not modify files or GitHub state.

Read AGENTS.md and CONTRIBUTING.md. Inspect git status, commits relative to main, the linked issue and PR, changed files, check output, evidence artifacts, CI, reviews, and overlap with open PRs. Do not infer a pass when evidence is absent.

Return exactly the CONTRIBUTING.md handoff format, including issue, owner, branch, commit, PR, scope, files, offline tests, git diff check, required host, Vitis, RTL, dataset, and QoR tiers, origin/main freshness, overlap, risks, and one exact next action with its owner.
```

## Prompt 5: resolve overlap before editing

```text
Investigate overlap between issue #<A>/PR #<A_PR> and issue #<B>/PR #<B_PR>. Do not edit, comment, assign, merge, close, or push.

Compare acceptance criteria, planned and changed files, APIs, generated formats, datasets, tests, evidence tiers, and dependency direction. Classify the overlap as none, compatible sequencing, scope split required, or duplicate implementation.

Recommend one ownership plan. Name the owner for each remaining scope, permitted files or interfaces, merge order, required handoff, and which work should stop. Identify decisions that require the contributors rather than guessing.
```

## Daily team rhythm

At the start of a work period:

1. Fetch and inspect GitHub state.
2. Review the issue queue and draft PRs together for five minutes.
3. Confirm one owner per active issue.
4. State expected files and evidence tier before implementation.

At the end of a work period:

1. Push a meaningful checkpoint or clearly report local-only state.
2. Update the issue and draft PR when scope or evidence changed.
3. Produce the standard handoff.
4. Name one exact next action and owner.

## Anti-patterns to correct immediately

- Two people start from the same verbal request without an assigned issue.
- Someone copies a repository folder or `.git` directory between machines.
- Codex edits before fetching and checking open PRs.
- A contributor pulls automatically into a dirty checkout.
- A broad PR mixes a feature, cleanup, formatting, and unrelated documentation.
- A model-generated patch is accepted because it looks plausible.
- Offline tests require cloud credentials or the optional Anthropic SDK.
- Host compilation is described as synthesis, or Vitis CoSim is described as direct original-C-to-RTL proof.
- Missing Vitis, simulator, dataset, or QoR evidence is reported as a pass.
- Someone resolves a conflict with reset, forced checkout, or force-push before speaking to the other owner.
- A handoff says only that work is done or tests look good.

## Coaching questions

Ask these instead of giving only commands:

- Which GitHub issue owns this behavior?
- Who is the single current owner?
- Which files and interfaces are inside and outside scope?
- What other open PR could overlap?
- Which evidence tier does the acceptance criterion require?
- What does this particular pass prove, and what does it not prove?
- Where is the command output or artifact?
- What changed on origin/main since the branch started?
- What is the next action, and who owns it?

## Troubleshooting

### A peer cannot see a teammate's change

Confirm the teammate committed and pushed. Then run `git fetch origin --prune`. If the change was merged, fast-forward a clean local `main`. If it remains on a feature branch, inspect that branch or PR instead of expecting local files to change automatically.

### Two branches touch the same file

File overlap is a warning, not automatically a conflict. Compare behavior and exact lines. Decide on sequencing or split ownership before more edits. Record the decision in both issues or the relevant PR.

### Tests differ between machines

First run the offline CI command with no provider credentials. Record Python version and available `g++`, `make`, Vitis, simulator, and SSH tools. Separate an environment gap from a code regression; do not flatten an unavailable optional tool into a pass.

### A provider or Vitis credential is missing

Do not share a secret in chat, an issue, a PR, or a committed config. Reassign the optional evidence tier to an authorized machine or mark the requested tier accurately and hand it off with the required inputs.

## Team-lead completion checklist

Your peers are ready for unsupervised work when all three can:

- explain why GitHub, not folder mirroring, is the synchronization layer;
- perform read-only preflight before edits;
- define a ready issue and recognize duplicate work;
- use one issue, owner, branch, and draft PR;
- distinguish offline tests, host equivalence, Vitis phases, direct RTL evidence, dataset scope, and QoR;
- review evidence rather than model confidence;
- synchronize without discarding or rewriting work; and
- produce a complete handoff with an exact next action.
