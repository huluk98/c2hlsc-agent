#!/usr/bin/env python3
"""Unpack and run a portable AUTO RTL Vitis HLS JSON bundle.

Example:
  python3 c2hlsc_agent/scripts/run_vitis_bundle.py \
    --vitis /tools/Xilinx/Vitis_HLS/2023.2/bin/vitis_hls

The default bundle is the tracked simple_calculator smoke/cosim bundle under
configs/. Pass --bundle to test another bundle.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_bundle() -> Path:
    return repo_root() / "configs" / "simple_calculator_vitis_cosim_bundle.json"


def default_work_dir() -> Path:
    return repo_root() / "build" / "vitis_bundle_run"


def resolve_vitis_hls(vitis: str) -> Path:
    """Accept either a vitis_hls binary path or a likely Vitis install folder."""
    candidate = Path(vitis).expanduser()
    if candidate.is_file():
        return candidate

    possible = [
        candidate / "bin" / "vitis_hls",
        candidate / "Vitis_HLS" / "bin" / "vitis_hls",
        candidate / "Vitis" / "bin" / "vitis_hls",
    ]
    possible.extend(sorted(candidate.glob("Vitis_HLS/*/bin/vitis_hls")))
    possible.extend(sorted(candidate.glob("Vitis/*/bin/vitis_hls")))

    for path in possible:
        if path.is_file():
            return path

    on_path = shutil.which(vitis)
    if on_path:
        return Path(on_path)

    raise FileNotFoundError(
        "Could not find vitis_hls. Pass the full vitis_hls binary path, "
        "or a Vitis/Vitis_HLS install directory containing bin/vitis_hls."
    )


def bundle_file_text(name: str, value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(line, str) for line in value):
        text = "\n".join(value)
        return text + ("\n" if text else "")
    raise ValueError(f"Bundle file {name!r} must contain a string or list of strings")


def unpack_bundle(bundle_path: Path, work_dir: Path, clean: bool) -> dict:
    if clean and work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, dict):
        raise ValueError(f"{bundle_path} does not contain a JSON object field named 'files'")

    for name, value in files.items():
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"Refusing unsafe bundle filename: {name!r}")
        (work_dir / name).write_text(bundle_file_text(name, value), encoding="utf-8")

    if not (work_dir / "run_hls.tcl").is_file():
        raise ValueError("Bundle did not produce run_hls.tcl")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an AUTO RTL Vitis HLS JSON bundle.")
    parser.add_argument(
        "--vitis",
        required=True,
        help="Path to vitis_hls, a Vitis_HLS install directory, or a PATH command name.",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=default_bundle(),
        help=f"JSON bundle to run. Default: {default_bundle()}",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=default_work_dir(),
        help=f"Directory where files are unpacked and Vitis runs. Default: {default_work_dir()}",
    )
    parser.add_argument("--no-clean", action="store_true", help="Do not delete an existing work directory first.")
    parser.add_argument("--unpack-only", action="store_true", help="Only unpack files; do not launch Vitis.")
    parser.add_argument(
        "--log-tail-lines",
        type=int,
        default=120,
        help="Number of Vitis log lines to print when the flow fails. Default: 120.",
    )
    args = parser.parse_args()

    bundle = args.bundle.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()

    vitis_hls = resolve_vitis_hls(args.vitis)
    data = unpack_bundle(bundle, work_dir, clean=not args.no_clean)

    print(f"Bundle: {bundle}")
    print(f"Top: {data.get('top_function', 'unknown')}")
    print(f"Work dir: {work_dir}")
    print(f"Vitis HLS: {vitis_hls}")

    if args.unpack_only:
        print("Unpack-only mode: not running Vitis.")
        return 0

    log_path = work_dir / "vitis_hls.log"
    result = subprocess.run(
        [str(vitis_hls), "-f", "run_hls.tcl"],
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    log_path.write_text(result.stdout, encoding="utf-8", errors="replace")
    if result.returncode == 0:
        print("Vitis HLS flow completed successfully.")
    else:
        print(f"Vitis HLS flow failed with exit code {result.returncode}.", file=sys.stderr)
        print(f"Log: {log_path}", file=sys.stderr)
        if args.log_tail_lines > 0:
            print("\nVitis log tail", file=sys.stderr)
            print("--------------", file=sys.stderr)
            for line in result.stdout.splitlines()[-args.log_tail_lines :]:
                print(line, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
