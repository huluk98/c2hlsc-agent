#!/usr/bin/env python3
"""Fetch the CHStone benchmark suite into a local, git-ignored directory.

CHStone is **not vendored** into this repository on purpose. Its README states:

    Each program in the CHStone suite is owned by the copyright holder of the
    program. You must follow the copyright of each benchmark program.

There is no unified licence covering the twelve programs -- they come from SoftFloat,
the IJG JPEG library, libgsm, and others -- so redistributing them from a public repo
would mean republishing third-party code of mixed provenance. Fetching on demand keeps
the benchmark reproducible without doing that.

Usage:
    python scripts/fetch_chstone.py                 # clone into third_party/CHStone
    python scripts/fetch_chstone.py --check         # report what is present, fetch nothing
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

MIRROR = "https://github.com/ferrandi/CHStone"
DEFAULT_DEST = Path("third_party/CHStone")

BENCHMARKS = (
    "adpcm", "aes", "blowfish", "dfadd", "dfdiv", "dfmul",
    "dfsin", "gsm", "jpeg", "mips", "motion", "sha",
)


def present(dest: Path) -> list[str]:
    """Benchmark directories that actually exist under ``dest``."""

    return [name for name in BENCHMARKS if (dest / name).is_dir()]


def fetch(dest: Path, force: bool) -> int:
    if dest.exists():
        found = present(dest)
        if len(found) == len(BENCHMARKS) and not force:
            print(f"CHStone already present at {dest} ({len(found)}/12 benchmarks)")
            return 0
        if not force:
            print(
                f"{dest} exists but holds {len(found)}/12 benchmarks; "
                "re-run with --force to replace it",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(dest)

    if shutil.which("git") is None:
        print("git is required to fetch CHStone", file=sys.stderr)
        return 1

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"cloning {MIRROR} -> {dest}")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", MIRROR, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr.strip()[-2000:], file=sys.stderr)
        return result.returncode

    found = present(dest)
    print(f"fetched {len(found)}/12 benchmarks into {dest}")
    print("Licensing: each program is owned by its own copyright holder; see the CHStone README.")
    return 0 if len(found) == len(BENCHMARKS) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help=f"destination (default {DEFAULT_DEST})")
    parser.add_argument("--check", action="store_true", help="report what is present and exit")
    parser.add_argument("--force", action="store_true", help="replace an existing checkout")
    args = parser.parse_args(argv)

    dest = Path(args.dest)
    if args.check:
        found = present(dest)
        print(f"{dest}: {len(found)}/12 benchmarks" + (f" -- {', '.join(found)}" if found else " (run without --check to fetch)"))
        return 0 if len(found) == len(BENCHMARKS) else 1
    return fetch(dest, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
