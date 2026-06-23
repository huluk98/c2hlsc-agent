#!/usr/bin/env python3
"""Run c2hlsc-agent with an explicit vitis_hls binary path.

Edit VITIS_HLS_BIN below, or pass --vitis-hls-bin on the command line.
This script uses the currently active Python environment, so activate your
preferred Conda env first:

    conda activate hlsc
    python scripts/run_vitis_with_bin.py \
      --config examples/vector_add/config.yaml \
      --out build/vector_add

If you do not edit VITIS_HLS_BIN, pass it explicitly:

    python scripts/run_vitis_with_bin.py \
      --vitis-hls-bin "/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
      --config examples/vector_add/config.yaml \
      --out build/vector_add
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


# Paste your path here if you want a one-command runner.
# Example:
# VITIS_HLS_BIN = "/nvme1/vitis2024.2 1113 1001/Vitis_HLS/2024.2/bin/vitis_hls"
VITIS_HLS_BIN = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _split_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Run c2hlsc-agent Vitis verification using an explicit vitis_hls binary path.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python scripts/run_vitis_with_bin.py \\
    --vitis-hls-bin "/nvme1/vitis2024.2 1113 1001/Vitis_HLS/2024.2/bin/vitis_hls" \\
    --config examples/vector_add/config.yaml \\
    --out build/vector_add

  # Or edit VITIS_HLS_BIN at the top of this file, then:
  python scripts/run_vitis_with_bin.py --config examples/vector_add/config.yaml --out build/vector_add
""",
    )
    parser.add_argument(
        "--vitis-hls-bin",
        default=os.environ.get("VITIS_HLS_BIN") or VITIS_HLS_BIN,
        help="Full path to the vitis_hls executable. May contain spaces.",
    )
    parser.add_argument(
        "--print-env",
        action="store_true",
        help="Print the Python and vitis_hls paths before running.",
    )
    known, convert_args = parser.parse_known_args(argv)
    return known, convert_args


def main(argv: list[str] | None = None) -> int:
    args, convert_args = _split_args(list(sys.argv[1:] if argv is None else argv))
    if not convert_args:
        print("No c2hlsc-agent convert arguments were provided.", file=sys.stderr)
        print("Example: --config examples/vector_add/config.yaml --out build/vector_add", file=sys.stderr)
        return 2

    if not args.vitis_hls_bin:
        print("vitis_hls path is missing.", file=sys.stderr)
        print("Either edit VITIS_HLS_BIN in scripts/run_vitis_with_bin.py or pass --vitis-hls-bin.", file=sys.stderr)
        return 2

    vitis_hls = Path(args.vitis_hls_bin).expanduser()
    if not vitis_hls.exists():
        print(f"vitis_hls binary does not exist: {vitis_hls}", file=sys.stderr)
        return 2
    if not os.access(vitis_hls, os.X_OK):
        print(f"vitis_hls binary is not executable: {vitis_hls}", file=sys.stderr)
        print(f"Try: chmod +x {vitis_hls}", file=sys.stderr)
        return 2

    repo_root = _repo_root()
    env = os.environ.copy()
    env["PATH"] = f"{vitis_hls.parent}{os.pathsep}{env.get('PATH', '')}"
    env["VITIS_HLS_BIN"] = str(vitis_hls)
    env.setdefault("XILINX_HLS", str(vitis_hls.parent.parent))
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"

    resolved = shutil.which("vitis_hls", path=env["PATH"])
    if not resolved:
        print("Internal error: vitis_hls was not found after PATH update.", file=sys.stderr)
        return 2

    if args.print_env:
        print(f"Using Python: {sys.executable}")
        print(f"Using vitis_hls: {resolved}")
        print(f"Repo root: {repo_root}")

    command = [
        sys.executable,
        "-m",
        "c2hlsc_agent.cli",
        "convert",
        "--run-vitis",
        *convert_args,
    ]
    print("Running:", " ".join(command))
    return subprocess.call(command, cwd=repo_root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
