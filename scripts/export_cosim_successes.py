#!/usr/bin/env python3
"""Export only full-CoSim-passing HLS_NL generated cases.

This script consumes the report written by run_hls_nl_vitis_batch.py and copies
only rows with status=pass into a clean corpus directory. It keeps the export
small by default: DUT, testbench, run TCL, instruction, and compact evidence.
The bulky Vitis project directory remains in build/ for local debugging.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


MINIMAL_FILES = ("dut.cpp", "tb.cpp", "run_hls.tcl", "instruction.txt")


def load_report(report_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise SystemExit("Report root must be a JSON object")
    if not isinstance(report.get("summary"), dict) or not isinstance(report.get("results"), list):
        raise SystemExit("Report must contain object 'summary' and list 'results'")
    return report


def stable_case_name(row: dict[str, Any]) -> str:
    design_path = Path(str(row["path"]))
    return design_path.name


def copy_minimal_case(row: dict[str, Any], dest_root: Path, include_logs: bool) -> dict[str, Any]:
    source_dir = Path(str(row["path"]))
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Missing source case directory: {source_dir}")

    dest_dir = dest_root / stable_case_name(row)
    dest_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for name in MINIMAL_FILES:
        src = source_dir / name
        if src.is_file():
            shutil.copy2(src, dest_dir / name)
            copied.append(name)

    evidence = {
        "record_id": row.get("record_id"),
        "source_file": row.get("source_file"),
        "design_title": row.get("design_title"),
        "top": row.get("top"),
        "signature": row.get("signature"),
        "oracle_kind": row.get("oracle_kind"),
        "status": row.get("status"),
        "returncode": row.get("returncode"),
        "verilog_files": row.get("verilog_files", []),
        "cosim_artifacts": row.get("cosim_artifacts", []),
        "command": row.get("command"),
        "copied_files": copied,
    }
    if row.get("vitis_log_tail"):
        evidence["vitis_log_tail"] = row["vitis_log_tail"]
    (dest_dir / "cosim_pass_evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    if include_logs and row.get("log"):
        log_path = Path(str(row["log"]))
        if log_path.is_file():
            shutil.copy2(log_path, dest_dir / "vitis_hls.log")

    return {
        "record_id": row.get("record_id"),
        "source_file": row.get("source_file"),
        "top": row.get("top"),
        "oracle_kind": row.get("oracle_kind"),
        "path": str(dest_dir),
        "copied_files": copied,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export only full-CoSim-passing generated HLS_NL cases.")
    parser.add_argument("--report", type=Path, required=True, help="vitis_batch_report.json from run_hls_nl_vitis_batch.py")
    parser.add_argument("--out-dir", type=Path, required=True, help="Clean export directory for passing cases")
    parser.add_argument("--include-logs", action="store_true", help="Copy full Vitis logs for passing cases")
    parser.add_argument("--allow-non-cosim", action="store_true", help="Allow exporting pass rows from non-full-cosim reports")
    return parser


def main_from_args(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    report = load_report(args.report)
    summary = report["summary"]
    if summary.get("mode") != "full_cosim" and not args.allow_non_cosim:
        raise SystemExit(
            f"Refusing to export report mode {summary.get('mode')!r}; rerun with --run-full-cosim "
            "or pass --allow-non-cosim intentionally."
        )

    if args.out_dir.exists():
        shutil.rmtree(args.out_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    passed = [row for row in report["results"] if row.get("status") == "pass"]
    failed = [row for row in report["results"] if row.get("status") != "pass"]
    exported = [copy_minimal_case(row, args.out_dir, args.include_logs) for row in passed]

    export_summary = {
        "source_report": str(args.report),
        "source_input": summary.get("input"),
        "source_mode": summary.get("mode"),
        "source_out_dir": summary.get("out_dir"),
        "part": summary.get("part"),
        "clock": summary.get("clock"),
        "passed": len(passed),
        "failed": len(failed),
        "skipped": len(report.get("skipped", [])),
        "exported_files_per_case": list(MINIMAL_FILES) + ["cosim_pass_evidence.json"],
        "include_logs": args.include_logs,
    }

    (args.out_dir / "manifest.json").write_text(
        json.dumps({"summary": export_summary, "passes": exported, "failed": failed, "skipped": report.get("skipped", [])}, indent=2),
        encoding="utf-8",
    )
    write_jsonl(args.out_dir / "passes.jsonl", exported)
    write_jsonl(args.out_dir / "failed.jsonl", failed)
    write_jsonl(args.out_dir / "skipped.jsonl", list(report.get("skipped", [])))

    readme = [
        "# HLS_NL Full-CoSim Passing Cases",
        "",
        "This directory is generated by `scripts/export_cosim_successes.py`.",
        "Only rows with `status=pass` from a `--run-full-cosim` Vitis batch report are copied here.",
        "",
        f"- Source report: `{args.report}`",
        f"- Passed/exported: {len(passed)}",
        f"- Failed kept for inspection in `failed.jsonl`: {len(failed)}",
        f"- Skipped kept for inspection in `skipped.jsonl`: {len(report.get('skipped', []))}",
        f"- Part: `{summary.get('part')}`",
        f"- Clock: `{summary.get('clock')}`",
        "",
        "Each passing case contains `dut.cpp`, `tb.cpp`, `run_hls.tcl`, `instruction.txt`, and `cosim_pass_evidence.json`.",
        "Bulky Vitis project outputs are intentionally not copied into this export.",
        "",
    ]
    (args.out_dir / "README.md").write_text("\n".join(readme), encoding="utf-8")

    print(json.dumps(export_summary, indent=2))
    return 0


def main() -> int:
    return main_from_args()


if __name__ == "__main__":
    raise SystemExit(main())
