#!/usr/bin/env bash
set -euo pipefail

ISSUE=0
RUN_TESTS=0

usage() {
  echo "Usage: bash scripts/team_preflight.sh [--issue NUMBER] [--run-tests]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue)
      [[ $# -ge 2 ]] || { usage >&2; exit 64; }
      ISSUE="$2"
      shift 2
      ;;
    --run-tests)
      RUN_TESTS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

[[ "$ISSUE" =~ ^[0-9]+$ ]] || { echo "--issue must be a non-negative integer" >&2; exit 64; }

for command_name in git gh; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command not found: $command_name" >&2
    exit 127
  }
done

REPO_ROOT="$(git rev-parse --show-toplevel)" || {
  echo "Run this command from inside a Git repository." >&2
  exit 2
}
cd "$REPO_ROOT"

echo "== Authentication =="
gh auth status
LOGIN="$(gh api user --jq '.login')"

echo "== Remote refresh =="
git fetch origin --prune

BRANCH="$(git branch --show-current)"
REMOTE="$(git remote get-url origin)"
HEAD_SHA="$(git rev-parse --short=12 HEAD)"
AHEAD_BEHIND="$(git rev-list --left-right --count HEAD...origin/main)"
STATUS="$(git status --porcelain=v1)"

echo "== Local state =="
echo "Repository: $REPO_ROOT"
echo "Operator:   @$LOGIN"
echo "Origin:     $REMOTE"
echo "Branch:     $BRANCH"
echo "HEAD:       $HEAD_SHA"
echo "HEAD...origin/main counts: $AHEAD_BEHIND"

BLOCKERS=()
if [[ -n "$STATUS" ]]; then
  echo "Working tree: DIRTY"
  printf '%s\n' "$STATUS"
  BLOCKERS+=("The working tree contains changes; identify their owner before editing or synchronizing.")
else
  echo "Working tree: clean"
fi

if [[ ! "$REMOTE" =~ (^|[:/])huluk98/c2hlsc-agent(\.git)?$ ]]; then
  BLOCKERS+=("origin does not point to huluk98/c2hlsc-agent: $REMOTE")
fi

if (( ISSUE > 0 )); then
  echo "== Issue #$ISSUE =="
  gh issue view "$ISSUE" --repo huluk98/c2hlsc-agent --json number,title,state,assignees,labels,url
  ISSUE_STATE="$(gh issue view "$ISSUE" --repo huluk98/c2hlsc-agent --json state --jq '.state')"
  mapfile -t ASSIGNEES < <(gh issue view "$ISSUE" --repo huluk98/c2hlsc-agent --json assignees --jq '.assignees[].login')
  STATUS_LABEL_COUNT="$(gh issue view "$ISSUE" --repo huluk98/c2hlsc-agent --json labels --jq '[.labels[].name | select(startswith("status:"))] | length')"

  [[ "$ISSUE_STATE" == "OPEN" ]] || BLOCKERS+=("Issue #$ISSUE is not open.")
  if (( ${#ASSIGNEES[@]} == 0 )); then
    BLOCKERS+=("Issue #$ISSUE is unassigned; claim it before implementation.")
  elif ! printf '%s\n' "${ASSIGNEES[@]}" | grep -Fqx "$LOGIN"; then
    BLOCKERS+=("Issue #$ISSUE belongs to another contributor: ${ASSIGNEES[*]}")
  fi
  [[ "$STATUS_LABEL_COUNT" == "1" ]] || BLOCKERS+=("Issue #$ISSUE must have exactly one status:* label.")
  if [[ "$BRANCH" != "main" && "$BRANCH" != work/"$ISSUE"-"$LOGIN"-* ]]; then
    BLOCKERS+=("Branch '$BRANCH' does not match work/$ISSUE-$LOGIN-<slug>.")
  fi
fi

echo "== Open pull requests =="
gh pr list --repo huluk98/c2hlsc-agent --state open --limit 50

echo "== Diff check =="
if ! git diff --check; then
  BLOCKERS+=("git diff --check failed.")
fi

if (( RUN_TESTS == 1 )); then
  echo "== Offline unit tests =="
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    echo "Neither python3 nor python was found." >&2
    exit 127
  fi
  if ! "$PYTHON_BIN" -m unittest discover -s tests; then
    BLOCKERS+=("Offline unit tests failed.")
  fi
fi

if (( ${#BLOCKERS[@]} > 0 )); then
  echo "== Preflight verdict: STOP =="
  printf -- '- %s\n' "${BLOCKERS[@]}"
  exit 2
fi

echo "== Preflight verdict: READY =="
if (( ISSUE > 0 )) && [[ "$BRANCH" == "main" ]]; then
  echo "Next: create work/$ISSUE-$LOGIN-<slug> from this clean, current main branch."
else
  echo "Next: compare planned files with the open pull requests before editing."
fi
