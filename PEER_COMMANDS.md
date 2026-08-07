# Peer command sheet

Send this file to every contributor. It is the command-focused companion to
[CONTRIBUTING.md](CONTRIBUTING.md) and
[PEER_COLLABORATION_TRAINING.md](PEER_COLLABORATION_TRAINING.md).

Replace every ALL_CAPS placeholder before running a command. Run one section at
a time; do not paste the entire file into a terminal.

## Message to send each peer

~~~text
Repository: https://github.com/huluk98/c2hlsc-agent

Use your own GitHub account and local clone. Read AGENTS.md, CONTRIBUTING.md,
PEER_COMMANDS.md, and the issue assigned to you before editing.

GitHub is our shared source of truth. Each implementation has one issue, one
owner, one branch, and one draft pull request. Do not work directly on main,
another person's branch, or an unassigned issue. Fetch and check open work
before editing. Never reset, auto-stash, force-push, or report an unrun check as
passing.
~~~

## 1. One-time setup for every peer

Required locally: Git, GitHub CLI, Python 3.9 or newer, and Codex.

Authenticate first, then clone:

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

GitHub Actions runs the suite on Ubuntu. If native Windows reports the known
path-separator or Unix Makefile-clean failures, do not hide them or change
unassigned runtime code. Record the Windows result and run the CI-equivalent
suite in Linux or WSL.

Open this repository folder in Codex and paste:

~~~text
Read AGENTS.md, CONTRIBUTING.md, and PEER_COMMANDS.md. Do not edit anything.
Report the repository root, current branch, working-tree state, remote, and the
collaboration and verification rules you loaded.
~~~

Expected result: Codex identifies the root AGENTS.md and reports a clean main
checkout. Resolve any mismatch before starting work.

## 2. Create or claim one work item

Start at the pinned shared queue:

https://github.com/huluk98/c2hlsc-agent/issues/12

Create a Team work item from:

https://github.com/huluk98/c2hlsc-agent/issues/new/choose

The issue must contain the outcome, acceptance criteria, non-goals, expected
files, evidence tiers, dependencies, and one owner. The form automatically adds
the team-work and status:todo labels.

Link the new issue under the shared queue:

~~~bash
gh issue edit 12 --add-sub-issue ISSUE_NUMBER
~~~

Before claiming it:

~~~bash
gh issue list --state open
gh pr list --state open
gh issue view ISSUE_NUMBER --comments
git fetch origin --prune
git branch --remotes
~~~

If the issue is unassigned and does not overlap existing work:

~~~bash
gh issue edit ISSUE_NUMBER --add-assignee '@me' --remove-label 'status:todo' --add-label 'status:in-progress'
gh issue comment ISSUE_NUMBER --body 'Claimed by @GITHUB_USER. Branch: work/ISSUE_NUMBER-GITHUB_USER-SHORT_SLUG. Expected files: PATHS.'
~~~

Stop if another person owns the issue or an open issue or PR covers the same
behavior or files. Agree on a split, dependency order, or handoff first.

## 3. Run the read-only preflight

~~~bash
git status --short --branch
git fetch origin --prune
git branch --show-current
git log --oneline --left-right HEAD...origin/main
gh issue view ISSUE_NUMBER --comments
gh pr list --state open
~~~

The checkout must be clean. The owner, issue, expected files, and evidence
scope must agree before implementation begins.

## 4. Update main and create the owned branch

Run only from a clean checkout:

~~~bash
git switch main
git pull --ff-only origin main
git switch -c work/ISSUE_NUMBER-GITHUB_USER-SHORT_SLUG
git push -u origin HEAD
~~~

Never reuse another contributor's branch. Never implement directly on main.

## 5. Owner prompt to paste into Codex

~~~text
Coordinate and implement GitHub issue #ISSUE_NUMBER in
huluk98/c2hlsc-agent as @GITHUB_USER.

Outcome: SHORT_OUTCOME
Expected files: EXPECTED_PATHS
Required evidence: REQUIRED_EVIDENCE_TIERS

Follow AGENTS.md, CONTRIBUTING.md, and PEER_COMMANDS.md.

First perform a read-only preflight:
1. Identify the authenticated GitHub user, repository, current branch,
   working-tree state, remotes, and unpushed commits.
2. Fetch and prune origin without changing checked-out files.
3. Read issue #ISSUE_NUMBER, its assignee and comments, open pull requests, and
   remote branches.
4. Compare behavior, files, interfaces, generated formats, datasets, and
   evidence tiers for overlap.
5. Report the operator, issue owner, branch, remote freshness, planned files,
   overlap risk, and next authorized action before editing.

Stop without editing if the issue belongs to somebody else, overlap is
unresolved, unrelated local changes exist, or the current branch is main.

If preflight is clean, implement only the acceptance criteria. Preserve the
original C as the golden reference, keep the offline path independent of
provider credentials, and treat generated or LLM-produced code only as a
candidate. Run every required check and report pass, fail, blocked, or skipped
truthfully.

This prompt authorizes focused file edits, relevant tests, an intentional
commit, pushing only the named branch, and opening or updating a draft pull
request. It does not authorize merging, force-pushing, rewriting history,
discarding changes, editing another person's branch, committing secrets or
generated output, or expanding scope without updating the issue.

End with the handoff format from CONTRIBUTING.md.
~~~

## 6. Validate, commit, push, and open the draft PR

Use the Python command appropriate for the operating system:

~~~bash
python3 -m unittest discover -s tests
git diff --check
git status --short --branch
git diff --stat
~~~

Stage only the intended files:

~~~bash
git add -- PATH_ONE PATH_TWO
git diff --cached --check
git diff --cached --stat
git commit -m 'SHORT SUMMARY (#ISSUE_NUMBER)'
git push
~~~

Open a draft PR in the browser so the repository template is available:

~~~bash
gh pr create --draft --base main --web
~~~

After the draft PR exists:

~~~bash
gh issue edit ISSUE_NUMBER --remove-label 'status:in-progress' --add-label 'status:review'
~~~

In the PR, include Closes #ISSUE_NUMBER, the owner, scope, non-goals, changed
files, commands actually run, evidence or artifact paths, unavailable tools,
overlap, risks, and the next action.

If work cannot proceed, replace the current status with status:blocked and add
a comment naming the blocker, required input, and owner of the next action.

## 7. Safe daily synchronization

Always inspect before applying remote changes:

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

For your own published feature branch, first inspect overlap. If incorporating
main is appropriate:

~~~bash
git switch work/ISSUE_NUMBER-GITHUB_USER-SHORT_SLUG
git merge origin/main
git push
~~~

Do not run the feature-branch merge automatically. If conflicts appear, stop
and identify the owner and issue on both sides before resolving them.

## 8. Reviewer commands and Codex prompt

Read the PR without changing its branch:

~~~bash
gh pr view PR_NUMBER
gh pr diff PR_NUMBER
gh pr checks PR_NUMBER
~~~

Paste into Codex:

~~~text
Review pull request #PR_NUMBER in huluk98/c2hlsc-agent. Do not edit files,
commit, push, approve, or merge.

Read the linked issue, AGENTS.md, CONTRIBUTING.md, the complete diff, open
comments, and CI results. Check other open issues and PRs for overlap.

Verify issue alignment, golden-C independence, offline deterministic behavior,
optional-provider fallback, verification order, exact report statuses, repair
auditability, and truthful host, Vitis, RTL, dataset, and QoR claims. Confirm
that each claimed command has output or an artifact. Look for secrets,
machine-specific paths, generated output, unrelated cleanup, and missing tests.

Return findings ordered by severity with exact file and line references. Then
list questions, missing evidence, test coverage, overlap risk, and one
recommendation: request changes, ready after named fixes, or ready for human
approval.
~~~

The human reviewer resolves questions and uses the GitHub UI to approve or
request changes. Codex findings are review input, not automatic approval.

## 9. Mark ready and merge

The owner marks the draft ready only after self-review and required evidence:

~~~bash
gh pr ready PR_NUMBER
~~~

The integrator verifies approval, resolved conversations, and CI:

~~~bash
gh pr checks PR_NUMBER
gh pr view PR_NUMBER --comments
gh pr view PR_NUMBER --json reviewDecision,mergeStateStatus,statusCheckRollup
~~~

Only the authorized integrator merges:

~~~bash
gh pr merge PR_NUMBER --squash --delete-branch
~~~

Do not merge when checks are pending or failing, required evidence is missing,
review is unresolved, or branch protection has not been satisfied.

## 10. Synchronize after merge

Each peer runs this only from a clean checkout:

~~~bash
git fetch origin --prune
git switch main
git pull --ff-only origin main
git status --short --branch
~~~

A merge changes GitHub main; it does not silently change files on another
machine. Fetch discovers the merge, and the clean fast-forward pull applies it.

## 11. Handoff prompt

~~~text
Prepare the c2hlsc-agent handoff for the current issue and branch. Do not
modify files or GitHub state.

Read AGENTS.md and CONTRIBUTING.md. Inspect git status, commits relative to
main, the linked issue and PR, changed files, check output, evidence artifacts,
CI, reviews, and overlap with open PRs. Do not infer pass when evidence is
absent.

Return the exact CONTRIBUTING.md handoff format: issue, owner, branch, commit,
PR, scope, files, offline tests, git diff check, required host, Vitis, RTL,
dataset and QoR tiers, origin/main freshness, overlap, risks, unavailable
tools, and one exact next action with its owner.
~~~

## Commands nobody should use to solve coordination

Do not use reset, forced checkout, automatic stash, force-push, history
rewrites, or copied .git directories to make disagreement disappear. Stop,
inspect, and coordinate with the other owner.
