#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import tarfile
from pathlib import Path


DEFAULT_FILES = [
    "conversion_report.md",
    "conversion_report.json",
    "software_equivalence.log",
    "csim.log",
    "csynth.log",
    "cosim.log",
    "run_hls.tcl",
    "run_csim.tcl",
    "run_csynth.tcl",
    "run_cosim.tcl",
    "input.c",
    "src/hls_top.cpp",
    "src/hls_top.hpp",
    "tb/testbench.cpp",
]


def copy_if_exists(src: Path, dst: Path) -> None:
    if src.exists() and src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect c2hlsc-agent Vitis debug logs into a tarball.")
    parser.add_argument("--out", required=True, help="Generated project directory, e.g. build/vector_add")
    parser.add_argument("--name", default="", help="Optional bundle base name")
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    if not out_dir.exists():
        raise SystemExit(f"Output directory does not exist: {out_dir}")

    bundle_name = args.name or f"debug_bundle_{out_dir.name}"
    bundle_dir = out_dir / bundle_name
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    for rel in DEFAULT_FILES:
        copy_if_exists(out_dir / rel, bundle_dir / rel)

    project = out_dir / "c2hlsc_project"
    if project.exists():
        for log in project.rglob("*.log"):
            rel = log.relative_to(project)
            copy_if_exists(log, bundle_dir / "c2hlsc_project_logs" / rel)
        for rpt in project.rglob("*.rpt"):
            rel = rpt.relative_to(project)
            copy_if_exists(rpt, bundle_dir / "c2hlsc_project_reports" / rel)

    tar_path = out_dir / f"{bundle_name}.tar.gz"
    if tar_path.exists():
        tar_path.unlink()
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_name)

    print(tar_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
