#!/usr/bin/env python3
"""Read and verify the GitHub protections required by the team workflow."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any, Optional


DEFAULT_REPOSITORY = "huluk98/c2hlsc-agent"
DEFAULT_BRANCH = "main"
DEFAULT_CHECK = "ci"


def _enabled(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("enabled") is True
    return value is True


def validate_guardrails(
    protection: dict[str, Any],
    repository: dict[str, Any],
    required_check: str = DEFAULT_CHECK,
) -> list[str]:
    """Return human-readable policy violations from GitHub API responses."""
    errors: list[str] = []

    checks = protection.get("required_status_checks") or {}
    contexts = set(checks.get("contexts") or [])
    contexts.update(
        item.get("context")
        for item in checks.get("checks") or []
        if item.get("context")
    )
    if required_check not in contexts:
        errors.append(f"required status check '{required_check}' is missing")
    if checks.get("strict") is not True:
        errors.append("branches are not required to be current before merge")

    reviews = protection.get("required_pull_request_reviews") or {}
    if int(reviews.get("required_approving_review_count") or 0) < 1:
        errors.append("at least one pull-request approval is not required")
    if reviews.get("require_last_push_approval") is not True:
        errors.append("the latest reviewable push does not require teammate approval")
    if reviews.get("dismiss_stale_reviews") is not True:
        errors.append("stale approvals are not dismissed after new changes")

    if not _enabled(protection.get("required_conversation_resolution")):
        errors.append("review conversations need not be resolved")
    if not _enabled(protection.get("enforce_admins")):
        errors.append("administrators can bypass branch protection")
    if _enabled(protection.get("allow_force_pushes")):
        errors.append("force pushes are allowed")
    if _enabled(protection.get("allow_deletions")):
        errors.append("protected-branch deletion is allowed")
    if repository.get("allow_auto_merge") is True:
        errors.append("repository auto-merge is enabled")

    return errors


def gh_api(endpoint: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "gh",
            "api",
            endpoint,
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--required-check", default=DEFAULT_CHECK)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        protection = gh_api(f"repos/{args.repo}/branches/{args.branch}/protection")
        repository = gh_api(f"repos/{args.repo}")
    except FileNotFoundError:
        print("ERROR: GitHub CLI (gh) was not found.", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"ERROR: unable to read GitHub guardrails: {detail}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: GitHub returned invalid JSON: {exc}", file=sys.stderr)
        return 2

    errors = validate_guardrails(protection, repository, args.required_check)
    if errors:
        print(f"Guardrail verification FAILED for {args.repo}:{args.branch}")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Guardrail verification PASSED for {args.repo}:{args.branch}")
    print(f"- required check: {args.required_check}")
    print("- pull request plus non-author/latest-push approval")
    print("- stale-review dismissal and resolved conversations")
    print("- admin enforcement; no force push, deletion, or auto-merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
