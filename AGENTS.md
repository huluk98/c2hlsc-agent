# Repository guidance

## Product invariants

- Treat the original C implementation as the golden behavioral reference. Generated or LLM-produced HLS-C must never become its own acceptance oracle.
- Preserve the short-circuit verification ladder: static analysis, host software equivalence, Vitis CSim, CSynth, and C/RTL CoSim. Do not bypass an earlier failure to claim a later success.
- Use the repository status values `pass`, `fail`, `blocked`, and `skipped` exactly as emitted by the implementation and reports. Never promote missing or unrequested evidence to `pass`.
- Keep the deterministic, offline path working without the Anthropic SDK, API keys, or network access. Ordinary CI intentionally exercises this path.
- Treat LLM generation and repair as optional proposal mechanisms. Verification, not model output, decides acceptance.
- Vitis execution is opt-in. Do not describe host compilation as CSim, synthesis, CoSim, direct RTL simulation, or QoR evidence.
- Vitis C/RTL CoSim checks generated HLS-C against generated RTL; it is not by itself proof that the original C and RTL are equivalent. Preserve the host golden-C comparison and the full evidence chain.
- Do not claim latency, area, timing, power, or comparative QoR without fresh reports from a named tool, version, target, clock, and flow.
- Preserve repair and transformation audit records. Do not hide a failed phase, oscillation rejection, deterministic fallback, or unavailable backend.
- Keep every autonomous loop bounded by explicit attempts, timeouts, worker caps, durable checkpoints, and repeated-state detection. Preserve immutable retry budgets across resume and hand `blocked` or `exhausted` runs to a human.
- Never commit API keys, provider tokens, licensed-tool credentials, machine-specific Vitis paths, generated `build/` output, or local dataset results unless an issue explicitly defines a reviewed artifact policy.

## Team collaboration

- Use COLLABORATOR_START_HERE.md as the self-contained onboarding and operational entry point for human and Codex contributors.
- Use the repository skill `$coordinate-team-work` for onboarding, issue work, synchronization, review, blocked-run handoff, and merge-readiness tasks. Copy-ready user prompts are in `CODEX_TEAM_PROMPTS.md`.
- Project agents are deliberately narrow: `coordination_explorer` and `verification_reviewer` are read-only; `bounded_implementer` may edit only after preflight passes. They do not publish GitHub state or recursively spawn agents. The parent agent owns Git and GitHub writes.
- Treat pinned GitHub issue #12 and its linked sub-issues as the shared work queue. Every implementation issue must carry the team-work label and exactly one current status label until it closes.
- Read `CONTRIBUTING.md` and `PEER_COMMANDS.md`. Use `PEER_COLLABORATION_TRAINING.md` for onboarding exercises and coaching.
- Treat GitHub `origin` as the shared source of truth. Local clones and Codex worktrees are isolated working copies, not live-synchronized folders.
- Every implementation task must reference a GitHub issue with acceptance criteria, non-goals, expected files, verification scope, and one current owner. Read-only investigation may proceed before assignment; file edits may not.
- Before editing, identify the operator, inspect `git status`, fetch and prune `origin`, read the issue, inspect open pull requests and remote branches, and compare planned behavior and files for overlap.
- Use one owner and one branch per issue unless explicit subissues divide non-overlapping work. Name normal work branches `work/<issue>-<github-user>-<slug>`.
- Never implement directly on `main`, another contributor's branch, or a checkout containing unrelated uncommitted changes.
- Never reset, overwrite, auto-stash, force-push, or rewrite published history to simplify synchronization.
- If another issue or pull request overlaps, stop before editing and propose a scope split, dependency order, or explicit handoff.
- Open a draft pull request after the first meaningful commit. Link the issue with `Closes #<issue>` and update the issue and PR when scope changes.
- Fetch before synchronization. Fast-forward a clean local `main`; merge fresh `origin/main` into a published feature branch without rewriting its history.
- Require the stable `ci` check, at least one non-author approval, approval of the latest reviewable push, and resolved conversations before merge. Prefer squash merge, never use auto-merge for this workflow, and do not bypass branch protection.
- Prefer native PowerShell, Git, GitHub CLI, and Python on Windows. Ubuntu contributors use the equivalent Bash and `python3` commands; do not require WSL for a Windows checkout.

## Required checks and output

- For every change, run `python -m unittest discover -s tests` and `git diff --check`.
- Ordinary tests must pass without installing the optional Anthropic SDK or using network credentials.
- Run host, Vitis, direct RTL, dataset, or QoR checks only when the issue requires them and the necessary tools are available. Report unrun and unavailable tiers truthfully.
- Before implementation, report the operator, issue owner, branch, working-tree state, remote freshness, intended files, possible overlap, and next authorized action.
- At handoff, report the issue, branch, commit, pull request, changed behavior and files, every check actually run, evidence paths, unavailable tools, unresolved risks, and the exact next action with its owner.
