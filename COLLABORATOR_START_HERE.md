# c2hlsc-agent collaborator start here

Share this file with every contributor. It is the single operational guide for
joining the repository, claiming work, using Codex, avoiding duplicate effort,
reviewing evidence, merging safely, and synchronizing each machine.

Replace every ALL_CAPS placeholder before running a command. Run one section at
a time. Never paste this whole document into a terminal.

## Quick links

- Repository: https://github.com/huluk98/c2hlsc-agent
- Shared work queue: https://github.com/huluk98/c2hlsc-agent/issues/12
- Create a work item: https://github.com/huluk98/c2hlsc-agent/issues/new/choose
- Open team work: https://github.com/huluk98/c2hlsc-agent/issues?q=is%3Aissue%20is%3Aopen%20label%3Ateam-work
- Open pull requests: https://github.com/huluk98/c2hlsc-agent/pulls?q=is%3Apr%20is%3Aopen

## The shared mental model

GitHub is the shared source of truth. Each person has an independent local
clone. Local folders are not live mirrors of one another.

- An issue defines the work and its owner.
- A branch holds one owner's implementation.
- A push makes that branch visible on GitHub.
- A pull request exposes the proposed change for review.
- A merge changes shared main.
- A fetch discovers remote changes.
- A clean fast-forward pull applies merged main locally.

A change on one computer does not silently modify another computer. Teammates
see the change after it is committed, pushed, reviewed, merged, fetched, and
applied safely.

## Non-negotiable rules

1. No assigned issue means no implementation edits.
2. One issue has one owner and one branch.
3. Do not implement directly on main.
4. Check the work queue and open PRs before editing.
5. Do not edit another contributor's branch without a recorded handoff.
6. Do not reset, auto-stash, force-checkout, rebase published history, or
   force-push to hide a coordination problem.
7. Do not commit API keys, provider credentials, licensed-tool credentials,
   machine-specific Vitis paths, or generated build output.
8. Never report pass for a check that was not run successfully.
9. Generated or LLM-proposed code is a candidate, not verification evidence.
10. CI and at least one teammate review are required before merge.

## Three rotating roles

- **Owner:** claims one issue, implements it on one branch, maintains its draft
  PR, records evidence, and writes the handoff.
- **Reviewer:** checks scope, correctness, overlap, tests, evidence integrity,
  secrets, and repository-specific verification boundaries.
- **Integrator:** confirms review, CI, and resolved conversations, then performs
  the permitted merge.

Rotate the roles so all three contributors learn the complete workflow.

## Shared issue lifecycle

| State | Meaning | Required action |
| --- | --- | --- |
| status:todo | Ready to claim | Confirm scope and overlap |
| status:in-progress | One owner is implementing | Maintain branch and issue updates |
| status:review | A pull request is under review | Resolve findings and CI |
| status:blocked | A named dependency prevents progress | Record blocker and next owner |
| Closed | Work is merged or intentionally ended | Treat as completed archive |

Use the team-work label and exactly one open status label on every implementation
issue. Link every work item beneath Team Work Queue #12.

## 1. One-time setup

Required locally:

- a personal GitHub account with repository access;
- Git;
- GitHub CLI;
- Python 3.9 or newer; and
- Codex.

Authenticate and clone:

~~~bash
gh auth login
gh auth status
gh repo clone huluk98/c2hlsc-agent
cd c2hlsc-agent
git config user.name 'YOUR_NAME'
git config user.email 'YOUR_EMAIL'
git remote -v
~~~

On Linux, macOS, or WSL:

~~~bash
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
python3 -m unittest discover -s tests
~~~

On native Windows PowerShell:

~~~powershell
py -m pip install -r requirements.txt
py -m pip install -e .
py -m unittest discover -s tests
~~~

GitHub Actions runs on Ubuntu. If native Windows exposes platform-specific path
or Makefile behavior, record it accurately and run the CI-equivalent suite in
Linux or WSL. Do not modify unrelated runtime code without an assigned issue.

Open the repository folder in Codex and paste:

~~~text
Read AGENTS.md and COLLABORATOR_START_HERE.md. Do not edit anything.

Report:
1. repository root;
2. authenticated GitHub user;
3. current branch;
4. working-tree state;
5. origin URL; and
6. the collaboration and verification rules you loaded.
~~~

Resolve any mismatch before starting work.

## 2. Create and link a work item

Start at the shared queue:

https://github.com/huluk98/c2hlsc-agent/issues/12

Create a Team work item:

https://github.com/huluk98/c2hlsc-agent/issues/new/choose

The issue must define:

- the observable outcome;
- acceptance criteria;
- non-goals;
- expected files, interfaces, generated formats, or datasets;
- required evidence tiers;
- optional provider, Vitis, simulator, SSH, dataset, or QoR requirements;
- dependencies and possible overlap; and
- one owner.

The form adds team-work and status:todo. Link the new issue under the queue:

~~~bash
gh issue edit 12 --add-sub-issue ISSUE_NUMBER
~~~

## 3. Inspect before claiming

~~~bash
git status --short --branch
git fetch origin --prune
git branch --show-current
git branch --remotes
gh issue list --state open
gh pr list --state open
gh issue view ISSUE_NUMBER --comments
~~~

Compare the planned behavior, files, interfaces, datasets, generated formats,
and evidence tiers with every relevant issue and PR.

Stop if:

- another person owns the issue;
- another issue or PR covers the same outcome;
- expected files or interfaces overlap without an agreed sequence;
- the checkout contains unrelated changes; or
- the requested evidence cannot support the acceptance criteria.

If the issue is unassigned and clear:

~~~bash
gh issue edit ISSUE_NUMBER --add-assignee '@me' --remove-label 'status:todo' --add-label 'status:in-progress'
gh issue comment ISSUE_NUMBER --body 'Claimed by @GITHUB_USER. Branch: work/ISSUE_NUMBER-GITHUB_USER-SHORT_SLUG. Expected files: PATHS. Required evidence: EVIDENCE_TIERS.'
~~~

Issue assignment is the ownership lock.

## 4. Create the owned branch

Run only from a clean checkout:

~~~bash
git switch main
git pull --ff-only origin main
git switch -c work/ISSUE_NUMBER-GITHUB_USER-SHORT_SLUG
git push -u origin HEAD
~~~

Never reuse another person's branch or implement on main.

## 5. Owner prompt for Codex

Paste this into Codex after replacing the placeholders:

~~~text
Coordinate and implement GitHub issue #ISSUE_NUMBER in
huluk98/c2hlsc-agent as @GITHUB_USER.

Outcome: SHORT_OUTCOME
Expected files: EXPECTED_PATHS
Required evidence: REQUIRED_EVIDENCE_TIERS

Follow AGENTS.md and COLLABORATOR_START_HERE.md.

First perform a read-only collaboration preflight:
1. Identify the authenticated GitHub user, repository, current branch,
   working-tree state, remotes, and unpushed commits.
2. Fetch and prune origin without changing checked-out files.
3. Read issue #ISSUE_NUMBER, its assignee and comments, open pull requests, and
   remote branches.
4. Compare acceptance criteria, planned behavior, files, interfaces, generated
   formats, datasets, and evidence tiers for overlap.
5. Report the operator, issue owner, branch, remote freshness, planned files,
   overlap risk, and next authorized action before editing.

Stop without editing if the issue belongs to somebody else, overlap is
unresolved, unrelated local changes exist, or the current branch is main.

If preflight is clean, implement only the acceptance criteria. Preserve
unrelated changes. Keep the offline deterministic path working without
provider SDKs, credentials, or network access. Preserve the original C as the
golden reference. Treat generated or LLM-produced code only as a candidate.

Run every required check. Report pass, fail, blocked, or skipped truthfully.
Make focused commits, push only the named branch, and open or update a draft PR
linked with Closes #ISSUE_NUMBER.

Do not merge, force-push, rewrite history, discard changes, edit another
person's branch, commit secrets or generated output, weaken verification, or
expand scope without updating the issue.

End with the handoff format in this guide.
~~~

## 6. Repository evidence model

The original C implementation is the golden behavioral reference.

~~~text
original C --host equivalence--> generated HLS-C
generated HLS-C --Vitis CoSim--> generated RTL
generated RTL --direct testbench--> interface-specific RTL evidence
reports --matching tool and target context--> defensible QoR comparison
~~~

No arrow silently replaces another arrow.

- Host compilation is not synthesis or RTL evidence.
- Vitis CoSim compares generated HLS-C with generated RTL; it does not by
  itself prove direct original-C-to-RTL equivalence.
- Direct RTL simulation is a separate evidence tier.
- QoR claims require fresh reports with tool, version, target, clock,
  constraints, flow, and baseline.
- Dataset claims must state scope, record IDs, resume state, workers, timeouts,
  and artifact paths.
- Optional unavailable tools are blocked or skipped, never silently passed.

The report status vocabulary is pass, fail, blocked, and skipped.

## 7. Validate the change

Every PR runs:

~~~bash
python3 -m unittest discover -s tests
git diff --check
git status --short --branch
git diff --stat
~~~

Use py instead of python3 on native Windows when appropriate.

Run additional host, Vitis, direct RTL, dataset, or QoR checks only when the
issue requires them. Attach or link the resulting evidence.

## 8. Commit and push

Review the diff and stage only intended paths:

~~~bash
git diff
git add -- PATH_ONE PATH_TWO
git diff --cached --check
git diff --cached --stat
git commit -m 'SHORT SUMMARY (#ISSUE_NUMBER)'
git push
~~~

Do not use a broad staging command when unrelated files exist.

## 9. Open the draft pull request

~~~bash
gh pr create --draft --base main --web
~~~

Fill the repository PR template. Include:

- Closes #ISSUE_NUMBER;
- owner and branch;
- desired outcome and acceptance criteria;
- non-goals;
- overlap check;
- changed behavior and files;
- every command actually run;
- evidence or artifact paths;
- blocked or skipped optional tiers;
- unresolved risks; and
- the next action and owner.

After the draft PR exists:

~~~bash
gh issue edit ISSUE_NUMBER --remove-label 'status:in-progress' --add-label 'status:review'
~~~

If work becomes blocked, replace the current status with status:blocked and add
an issue comment naming the blocker, required input, and next owner.

## 10. Reviewer workflow

Inspect without editing:

~~~bash
gh pr view PR_NUMBER
gh pr diff PR_NUMBER
gh pr checks PR_NUMBER
~~~

Paste into Codex:

~~~text
Review pull request #PR_NUMBER in huluk98/c2hlsc-agent. Do not edit files,
commit, push, approve, or merge.

Read the linked issue, AGENTS.md, COLLABORATOR_START_HERE.md, the complete diff,
open comments, and CI results. Check other open issues and PRs for overlap.

Verify issue alignment, golden-C independence, offline deterministic behavior,
optional-provider fallback, verification order, exact report statuses, repair
auditability, and truthful host, Vitis, RTL, dataset, and QoR claims. Confirm
that every claimed command has output or an artifact. Look for secrets,
machine-specific paths, generated output, unrelated cleanup, and missing tests.

Return findings ordered by severity with exact file and line references. Then
list questions, missing evidence, test coverage, overlap risk, and one
recommendation: request changes, ready after named fixes, or ready for human
approval.
~~~

The human reviewer resolves questions and approves or requests changes through
GitHub. Codex findings are review input, not automatic approval.

## 11. Ready and merge

The owner marks the draft ready only after self-review and required evidence:

~~~bash
gh pr ready PR_NUMBER
~~~

The integrator verifies approval, conversations, and CI:

~~~bash
gh pr checks PR_NUMBER
gh pr view PR_NUMBER --comments
gh pr view PR_NUMBER --json reviewDecision,mergeStateStatus,statusCheckRollup
~~~

Only the authorized integrator merges:

~~~bash
gh pr merge PR_NUMBER --squash --delete-branch
~~~

Do not merge with pending or failing checks, missing required evidence,
unresolved review, or unmet branch protection.

The Closes reference closes the child issue automatically. Closed child issues
are the completed archive beneath Team Work Queue #12.

## 12. Daily synchronization

Inspect first:

~~~bash
git status --short --branch
git fetch origin --prune
git log --oneline --left-right HEAD...origin/main
~~~

For a clean main checkout:

~~~bash
git switch main
git pull --ff-only origin main
~~~

For your own published feature branch, inspect overlap first. When appropriate:

~~~bash
git switch work/ISSUE_NUMBER-GITHUB_USER-SHORT_SLUG
git merge origin/main
git push
~~~

Do not incorporate main automatically into a dirty or unowned branch. If
conflicts appear, identify both issues and owners before resolving them.

## 13. Post-merge synchronization

Each contributor runs this from a clean checkout:

~~~bash
git fetch origin --prune
git switch main
git pull --ff-only origin main
git status --short --branch
~~~

## 14. Required handoff

Ask Codex to inspect the current state without changing it:

~~~text
Prepare the c2hlsc-agent handoff for the current issue and branch. Do not
modify files or GitHub state.

Read AGENTS.md and COLLABORATOR_START_HERE.md. Inspect git status, commits
relative to main, the linked issue and PR, changed files, check output,
evidence artifacts, CI, reviews, and overlap with open PRs. Do not infer pass
when evidence is absent.

Return the exact handoff format below with one exact next action and owner.
~~~

Handoff format:

~~~text
Work item: #ISSUE_NUMBER - TITLE
Owner: @GITHUB_USER
Branch: BRANCH
Commit: SHA
Pull request: URL_OR_NOT_OPENED

Scope completed:
- BEHAVIOR

Files changed:
- PATH: REASON

Checks and evidence:
- Offline unit tests: PASS_FAIL_BLOCKED_SKIPPED - SUMMARY
- git diff --check: PASS_FAIL_BLOCKED_SKIPPED - SUMMARY
- Host equivalence: STATUS - ARTIFACT_OR_REASON
- Vitis CSim/CSynth/CoSim: STATUSES - ARTIFACTS_OR_REASON
- Direct RTL, dataset, or QoR: STATUSES - ARTIFACTS_OR_REASON

Coordination:
- origin/main incorporated through: SHA
- Overlapping issues or PRs: NONE_OR_LINKS
- Risks and unavailable tools: NONE_OR_DETAILS
- Next action and owner: EXACT_ACTION
~~~

## 15. Troubleshooting

### A teammate cannot see a change

Confirm the owner committed and pushed. If it was merged, fetch and
fast-forward a clean main. If it remains on a feature branch, inspect its PR;
do not expect local files to change automatically.

### Two people selected the same work

Stop implementation. Compare acceptance criteria, behavior, files, interfaces,
and evidence tiers. Record a scope split, dependency order, or explicit
handoff in both issues.

### Two branches touch the same file

File overlap is a warning, not automatically a conflict. Compare exact behavior
and lines, then decide sequence or ownership before further edits.

### Tests differ between machines

Run the offline CI command without provider credentials. Record Python version
and available compiler, make, Vitis, simulator, SSH, and dataset tools.
Separate an environment difference from a code regression.

### A credential or licensed tool is missing

Never share the secret in chat, an issue, a PR, or committed configuration.
Assign the optional evidence tier to an authorized machine or report it
truthfully as blocked or skipped according to the issue requirement.

### Work must pause

Use status:blocked and comment with:

- the exact blocker;
- evidence that it is blocking;
- the required input or decision;
- the person responsible for the next action; and
- what work remains safe to do.

## Team-lead readiness checklist

Before unsupervised collaboration, each person should demonstrate:

- a correct local clone and GitHub identity;
- a clean baseline test run;
- a work item linked under queue #12;
- an overlap check before edits;
- issue assignment and a correctly named branch;
- a draft PR with actual evidence;
- a review that catches an unsupported claim;
- a complete handoff;
- a safe merge by the integrator; and
- a clean post-merge synchronization.

## Deeper references

- Repository rules: [AGENTS.md](AGENTS.md)
- Contribution policy: [CONTRIBUTING.md](CONTRIBUTING.md)
- Copy-and-run command reference: [PEER_COMMANDS.md](PEER_COMMANDS.md)
- Training exercises and coaching: [PEER_COLLABORATION_TRAINING.md](PEER_COLLABORATION_TRAINING.md)
