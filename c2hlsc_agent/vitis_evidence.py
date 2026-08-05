"""Validate and summarize a native Vitis verification project.

The conversion report is the phase ledger; this module adds artifact-level checks so a
GitHub workflow cannot call an exit-zero launcher run a Vitis proof.  A valid record
requires fresh synthesis metrics, emitted RTL, and a positive C/RTL CoSim marker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .hls_runner import VITIS_COSIM_SUCCESS_MARKERS
from .knowledge_graph import FILENAME as KNOWLEDGE_GRAPH_FILENAME
from .knowledge_graph import refresh_knowledge_graph
from .qor import find_csynth_xml, parse_csynth_xml
from .vitis_command import vitis_tcl_command

SCHEMA = "c2hlsc-vitis-evidence-v1"
REQUIRED_PASS_PHASES = (
    "software_equivalence",
    "shift_left_trace",
    "csim",
    "csynth",
    "cosim",
)


class VitisEvidenceError(RuntimeError):
    """Raised when a project lacks evidence required for native Vitis sign-off."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(project_dir: Path, path: Path) -> str:
    return str(path.relative_to(project_dir))


def validate_vitis_project(project_dir: Path) -> dict[str, object]:
    project_dir = project_dir.resolve()
    report_path = project_dir / "conversion_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VitisEvidenceError(f"conversion report unavailable or invalid: {exc}") from exc
    if not isinstance(report, dict):
        raise VitisEvidenceError("conversion_report.json must contain a JSON object")
    if report.get("status") != "pass":
        raise VitisEvidenceError(f"conversion status is {report.get('status')!r}, not 'pass'")
    if report.get("cosim_backend") != "vitis":
        raise VitisEvidenceError(
            f"expected native Vitis backend, found {report.get('cosim_backend')!r}"
        )

    bad_phases = {
        phase: report.get(phase)
        for phase in REQUIRED_PASS_PHASES
        if report.get(phase) != "pass"
    }
    if bad_phases:
        raise VitisEvidenceError(f"required phases did not pass: {bad_phases}")

    csynth = find_csynth_xml(project_dir)
    if csynth is None:
        raise VitisEvidenceError("csynth passed but no csynth.xml report exists")
    source_inputs = [
        project_dir / "input.c",
        project_dir / "src" / "hls_top.hpp",
        project_dir / "src" / "hls_top.cpp",
        project_dir / "tb" / "testbench.cpp",
    ]
    missing_inputs = [path for path in source_inputs if not path.is_file()]
    if missing_inputs:
        raise VitisEvidenceError(
            "generated source inputs are missing: "
            + ", ".join(_relative(project_dir, path) for path in missing_inputs)
        )
    newest_input_mtime = max(path.stat().st_mtime for path in source_inputs)
    if csynth.stat().st_mtime < newest_input_mtime:
        raise VitisEvidenceError("csynth.xml predates the generated source; report is stale")
    metrics = parse_csynth_xml(csynth)
    missing_metrics = [
        name
        for name in ("target_clock_ns", "estimated_clock_ns", "latency_worst")
        if getattr(metrics, name) is None
    ]
    if metrics.area_proxy is None:
        missing_metrics.append("FPGA area resources")
    if missing_metrics:
        raise VitisEvidenceError(
            "csynth.xml lacks required QoR metrics: " + ", ".join(missing_metrics)
        )

    rtl_files = sorted(
        {
            path
            for pattern in ("*.v", "*.sv")
            for path in (project_dir / "c2hlsc_project").glob(
                f"*/syn/verilog/{pattern}"
            )
            if path.is_file()
        }
    )
    if not rtl_files:
        raise VitisEvidenceError("csynth passed but no Vitis-generated Verilog/SystemVerilog exists")
    stale_rtl = [path for path in rtl_files if path.stat().st_mtime < newest_input_mtime]
    if stale_rtl:
        raise VitisEvidenceError(
            "Vitis-generated RTL predates the generated source: "
            + ", ".join(_relative(project_dir, path) for path in stale_rtl)
        )

    cosim_log = project_dir / "cosim.log"
    try:
        cosim_text = cosim_log.read_text(encoding="utf-8", errors="replace").lower()
    except OSError as exc:
        raise VitisEvidenceError(f"cosim.log is unavailable: {exc}") from exc
    if not any(marker in cosim_text for marker in VITIS_COSIM_SUCCESS_MARKERS):
        raise VitisEvidenceError("cosim.log has no positive C/RTL co-simulation PASS marker")
    newest_rtl_mtime = max(path.stat().st_mtime for path in rtl_files)
    newest_prerequisite_mtime = max(
        newest_input_mtime,
        csynth.stat().st_mtime,
        newest_rtl_mtime,
    )
    if cosim_log.stat().st_mtime < newest_prerequisite_mtime:
        raise VitisEvidenceError("cosim.log predates source, synthesis, or RTL artifacts")

    vitis_bin = str(report.get("vitis_bin") or "")
    if not vitis_bin:
        raise VitisEvidenceError("conversion report does not record the Vitis launcher")
    phases = {phase: report[phase] for phase in REQUIRED_PASS_PHASES}
    return {
        "schema": SCHEMA,
        "status": "pass",
        "backend": "vitis",
        "top": report.get("top"),
        "part": report.get("part"),
        "clock_ns": report.get("clock_ns"),
        "seed": report.get("seed"),
        "num_tests": report.get("num_tests"),
        "vitis_bin": vitis_bin,
        "native_cosim_command": vitis_tcl_command(vitis_bin, "run_cosim.tcl"),
        "phases": phases,
        "optional_shift_left": {
            phase: report.get(phase)
            for phase in ("coverage_gcov", "symbolic_klee")
        },
        "inputs": [
            {"path": _relative(project_dir, path), "sha256": _sha256(path)}
            for path in source_inputs
        ],
        "csynth_report": {
            "path": _relative(project_dir, csynth),
            "sha256": _sha256(csynth),
        },
        "metrics": metrics.to_dict(),
        "rtl": [
            {"path": _relative(project_dir, path), "sha256": _sha256(path)}
            for path in rtl_files
        ],
        "cosim_log": {
            "path": _relative(project_dir, cosim_log),
            "sha256": _sha256(cosim_log),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate fresh native Vitis RTL/CoSim evidence for a generated project."
    )
    parser.add_argument("project", type=Path)
    parser.add_argument("--out", type=Path, help="output JSON (default PROJECT/vitis_evidence.json)")
    args = parser.parse_args(argv)
    try:
        evidence = validate_vitis_project(args.project)
    except VitisEvidenceError as exc:
        print(f"Vitis evidence validation failed: {exc}", file=sys.stderr)
        return 1
    output = args.out or args.project / "vitis_evidence.json"
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    graph_path = args.project / KNOWLEDGE_GRAPH_FILENAME
    if graph_path.exists():
        refresh_knowledge_graph(args.project, phase_updates={"cosim": "pass"})
    print(f"Native Vitis evidence: PASS ({output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
