#!/usr/bin/env python3
"""Verify that a project's generated LeVeri testbenches were not hand-edited.

The LeVeri bundle is rendered deterministically from the interface contract, and the
manifest records the SHA-256 of every generated file (newline-normalized). Generated
testbenches must never be hand-edited — change the contract and regenerate — so any
drift between the on-disk files and the recorded hashes fails this check.

Usage:
    python scripts/check_generated_testbenches.py --project build/vector_add [--project ...]

Exit codes: 0 all checked projects are clean; 1 drift or missing files; 2 usage error.
Projects whose manifest predates hash recording are reported as SKIP (regenerate to
enable the check), not as failures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


MANIFEST_RELPATH = Path("tb") / "leveri_manifest.json"


def _hash_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def check_project(project: Path) -> tuple[str, list[str]]:
    """Return (status, messages) with status in {"ok", "skip", "fail"}."""

    manifest_path = project / MANIFEST_RELPATH
    if not manifest_path.exists():
        return "skip", [f"{project}: no {MANIFEST_RELPATH} (not a generated project?)"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "fail", [f"{project}: unreadable manifest: {exc}"]
    recorded = manifest.get("generated_file_sha256") or {}
    if not recorded:
        return "skip", [
            f"{project}: manifest records no file hashes (generated before hash recording); "
            "regenerate the project to enable the drift check"
        ]
    problems: list[str] = []
    for rel, expected in sorted(recorded.items()):
        path = project / rel
        if not path.exists():
            problems.append(f"{project}: MISSING generated file {rel}")
            continue
        actual = _hash_file(path)
        if actual != expected:
            problems.append(
                f"{project}: DRIFT in {rel} — generated testbenches must not be hand-edited; "
                "change the contract/config and regenerate the project"
            )
    if problems:
        return "fail", problems
    return "ok", [f"{project}: {len(recorded)} generated file(s) match the manifest"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project",
        action="append",
        required=True,
        dest="projects",
        help="generated project directory to check; may be repeated",
    )
    args = parser.parse_args(argv)
    failed = False
    for item in args.projects:
        status, messages = check_project(Path(item).expanduser().resolve())
        prefix = {"ok": "OK", "skip": "SKIP", "fail": "FAIL"}[status]
        for message in messages:
            print(f"[{prefix}] {message}")
        failed = failed or status == "fail"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
