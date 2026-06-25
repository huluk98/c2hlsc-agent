#!/usr/bin/env python3
"""Run c2hlsc-agent with an explicit vitis_hls binary path.

Put your path in vitis_hls_bin_path.txt, edit VITIS_HLS_BIN below, or pass
--vitis-hls-bin on the command line. This script uses the currently active
Python environment, so activate your preferred Conda env first:

    conda activate hlsc
    echo "/path/to/Vitis_HLS/2024.2/bin/vitis_hls" > vitis_hls_bin_path.txt
    python scripts/run_vitis_with_bin.py \
      --config examples/vector_add/config.yaml \
      --out build/vector_add

Or pass it explicitly:

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
# VITIS_HLS_BIN = "/path/to/Vitis_HLS/2024.2/bin/vitis_hls"
VITIS_HLS_BIN = ""


# Optional local text file. Put exactly one line in it:
# /path/to/Vitis_HLS/2024.2/bin/vitis_hls
VITIS_HLS_BIN_FILE = "vitis_hls_bin_path.txt"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_path_file(repo_root: Path) -> str:
    path_file = repo_root / VITIS_HLS_BIN_FILE
    if not path_file.exists():
        return ""
    for line in path_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def _auto_find_vitis_hls() -> str:
    patterns = [
        "/opt/Xilinx/Vitis_HLS/2024.2/bin/vitis_hls",
        "/opt/Xilinx/Vitis_HLS/*/bin/vitis_hls",
        "/tools/Xilinx/Vitis_HLS/2024.2/bin/vitis_hls",
        "/tools/Xilinx/Vitis_HLS/*/bin/vitis_hls",
        str(Path.home() / "Xilinx/Vitis_HLS/2024.2/bin/vitis_hls"),
        str(Path.home() / "Xilinx/Vitis_HLS/*/bin/vitis_hls"),
    ]
    for pattern in patterns:
        for match in sorted(Path("/").glob(pattern.lstrip("/"))):
            if match.is_file() and os.access(match, os.X_OK):
                return str(match)
    return ""


def _split_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Run c2hlsc-agent Vitis verification using an explicit vitis_hls binary path.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python scripts/run_vitis_with_bin.py \\
    --vitis-hls-bin "/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \\
    --config examples/vector_add/config.yaml \\
    --out build/vector_add

  # Or put the path in vitis_hls_bin_path.txt, then:
  python scripts/run_vitis_with_bin.py --config examples/vector_add/config.yaml --out build/vector_add
""",
    )
    parser.add_argument(
        "--vitis-hls-bin",
        default=None,
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
    repo_root = _repo_root()
    vitis_hls_bin = (
        args.vitis_hls_bin
        or os.environ.get("VITIS_HLS_BIN")
        or VITIS_HLS_BIN
        or _read_path_file(repo_root)
        or _auto_find_vitis_hls()
    )
    if not convert_args:
        print("No c2hlsc-agent convert arguments were provided.", file=sys.stderr)
        print("Example: --config examples/vector_add/config.yaml --out build/vector_add", file=sys.stderr)
        return 2

    if not vitis_hls_bin:
        print("vitis_hls path is missing.", file=sys.stderr)
        print("Use one of these options:", file=sys.stderr)
        print("  1. Edit VITIS_HLS_BIN in scripts/run_vitis_with_bin.py", file=sys.stderr)
        print("  2. Put the path in vitis_hls_bin_path.txt", file=sys.stderr)
        print("  3. Pass --vitis-hls-bin /path/to/bin/vitis_hls", file=sys.stderr)
        return 2

    vitis_hls = Path(vitis_hls_bin).expanduser()
    if not vitis_hls.exists():
        print(f"vitis_hls binary does not exist: {vitis_hls}", file=sys.stderr)
        return 2
    if not os.access(vitis_hls, os.X_OK):
        print(f"vitis_hls binary is not executable: {vitis_hls}", file=sys.stderr)
        print(f"Try: chmod +x {vitis_hls}", file=sys.stderr)
        return 2

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
