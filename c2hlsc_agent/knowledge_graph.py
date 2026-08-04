"""Deterministic, dependency-free verification knowledge graphs.

The graph is deliberately a small JSON interchange document rather than a graph
database.  It records contracts, generated artifacts, verifier outcomes, repairs,
and evidence *references*.  It never copies source, logs, report bodies, prompts,
or repair evidence into the graph.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA = "c2hlsc-verification-knowledge-graph-v1"
FILENAME = "verification_knowledge_graph.json"

_PHASE_ORDER = (
    "software_equivalence",
    "shift_left_trace",
    "coverage_gcov",
    "symbolic_klee",
    "csim",
    "csynth",
    "cosim",
    "rtl_cosim",
    "ppa",
)

_PHASE_LABELS = {
    "software_equivalence": "Golden-C host equivalence",
    "shift_left_trace": "LeVeri paired-trace comparison",
    "coverage_gcov": "Concrete coverage (gcov)",
    "symbolic_klee": "Bounded golden/HLS-C relational checking (KLEE)",
    "csim": "HLS C simulation",
    "csynth": "HLS synthesis",
    "cosim": "C/RTL co-simulation",
    "rtl_cosim": "Standalone RTL simulation",
    "ppa": "PPA measurement and criteria",
}

_GENERATED_ARTIFACTS = (
    "src/hls_top.hpp",
    "src/hls_top.cpp",
    "tb/testbench.cpp",
    "tb/leveri_golden_tb.cpp",
    "tb/leveri_hls_tb.cpp",
    "tb/leveri_compare.py",
    "tb/run_gcov.py",
    "tb/klee_driver.cpp",
    "tb/run_klee.py",
    "tb/leveri_manifest.json",
    "tb/rtl_vectors_tb.cpp",
    "tb/gen_rtl_tb.py",
    "tb/run_rtl_sim.py",
    "tb/rtl_tb_manifest.json",
    "run_hls.tcl",
    "run_csim.tcl",
    "run_csynth.tcl",
    "run_cosim.tcl",
    "Makefile",
    "run_all.sh",
)

_REPORT_ARTIFACTS = (
    "conversion_report.json",
    "conversion_report.md",
    "repair_audit.json",
    "manual_repair_report.json",
    "qor_report.json",
    "qor_report.md",
    "qor_table.tex",
    "ppa_report.json",
)

_EVIDENCE_ARTIFACTS = (
    "coverage/gcov_report.json",
    "coverage/klee_report.json",
    "coverage/rtl_tb_report.json",
    "leveri_golden_trace.csv",
    "leveri_hls_trace.csv",
    "syn/yosys_area.rpt",
    "syn/sta_report.txt",
    "syn/sta_report.failed.txt",
)

_EVIDENCE_PHASES = {
    "coverage/gcov_report.json": "coverage_gcov",
    "coverage/klee_report.json": "symbolic_klee",
    "coverage/rtl_tb_report.json": "rtl_cosim",
    "leveri_golden_trace.csv": "shift_left_trace",
    "leveri_hls_trace.csv": "shift_left_trace",
    "syn/yosys_area.rpt": "ppa",
    "syn/sta_report.txt": "ppa",
    "syn/sta_report.failed.txt": "ppa",
    "qor_report.json": "ppa",
    "qor_report.md": "ppa",
    "qor_table.tex": "ppa",
    "ppa_report.json": "ppa",
}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _edge_id(source: str, relation: str, target: str) -> str:
    payload = f"{source}\0{relation}\0{target}".encode("utf-8")
    return "edge:" + hashlib.sha256(payload).hexdigest()[:20]


def _add_edge(edges: list[dict[str, str]], source: str, relation: str, target: str) -> None:
    edges.append(
        {
            "id": _edge_id(source, relation, target),
            "source": source,
            "target": target,
            "type": relation,
        }
    )


def _normalise_phase_status(status: Any) -> str:
    value = str(status or "skipped").strip().lower()
    if value in {"pass", "passed", "success", "succeeded"}:
        return "PASS"
    if value in {"fail", "failed", "failure", "error"}:
        return "FAIL"
    if value in {"skip", "skipped", "not_run", "unavailable"}:
        return "SKIP"
    return "BLOCKED"


def _safe_type_metadata(value: Any) -> str:
    """Keep contract-level type syntax without carrying source comments/attributes."""

    text = str(value or "")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//.*", " ", text)
    # The analyzer tokenizes ``/* comment */`` into ``/ comment /`` while deriving
    # ``c_type``. A slash is not valid C/C++ type syntax, so remove those remnants.
    text = re.sub(r"/.*?/", " ", text, flags=re.S)
    text = re.sub(r"__attribute__\s*\(\(.*?\)\)", " ", text, flags=re.S)
    text = re.sub(r"\[\[.*?\]\]", " ", text, flags=re.S)
    # Preserve ordinary C/C++ type punctuation but discard literals and source-like
    # delimiters that are not part of a type contract.
    text = re.sub(r"[^A-Za-z0-9_\s:<>,*&\[\]()]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_klee_properties(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        metadata = {}
    properties: dict[str, Any] = {"requested_scope": "golden_hlsc_relational"}
    for key in (
        "schema",
        "scope",
        "outcome",
        "failure_kind",
        "completed_paths",
        "generated_tests",
        "timed_out",
        "counterexample_count",
        "invocations",
        "observable_count",
        "top",
        "evidence_origin",
    ):
        value = metadata.get(key)
        if isinstance(value, (str, int, bool)) or value is None:
            if value is not None:
                properties[key] = value
    names = metadata.get("counterexample_names")
    if isinstance(names, list):
        properties["counterexample_names"] = sorted(
            {
                name
                for name in names
                if isinstance(name, str)
                and re.fullmatch(
                    r"C2HLSC_RELATIONAL_MISMATCH:(?:return|[A-Za-z_][A-Za-z0-9_]*)",
                    name,
                )
            }
        )
        properties["counterexample_count"] = len(properties["counterexample_names"])
    bounded_lengths = metadata.get("bounded_lengths")
    if isinstance(bounded_lengths, Mapping):
        properties["bounded_lengths"] = {
            str(name): value
            for name, value in bounded_lengths.items()
            if isinstance(name, str) and type(value) is int and value > 0
        }
    scalar_ranges = metadata.get("scalar_ranges")
    if isinstance(scalar_ranges, Mapping):
        properties["scalar_ranges"] = {
            str(name): list(value)
            for name, value in scalar_ranges.items()
            if isinstance(name, str)
            and isinstance(value, list)
            and len(value) == 2
            and all(type(bound) is int for bound in value)
        }
    assumptions = metadata.get("assumptions")
    if isinstance(assumptions, Mapping):
        properties["assumptions"] = {
            key: assumptions[key]
            for key in ("pointer_alias_model", "hidden_state_model", "comparison")
            if isinstance(assumptions.get(key), str)
        }
    artifact_hashes = metadata.get("artifact_sha256")
    if isinstance(artifact_hashes, Mapping):
        properties["artifact_sha256"] = {
            path: digest.lower()
            for path, digest in artifact_hashes.items()
            if isinstance(path, str)
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
        }
    return properties


def _safe_relative(project_dir: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _artifact_node(project_dir: Path, relative: str, kind: str) -> dict[str, Any] | None:
    path = project_dir / relative
    if not path.is_file():
        return None
    safe_relative = _safe_relative(project_dir, path)
    if safe_relative is None or safe_relative == FILENAME:
        return None
    return {
        "id": f"artifact:{safe_relative}",
        "kind": kind,
        "label": Path(safe_relative).name,
        "properties": {
            "path": safe_relative,
            "size_bytes": path.stat().st_size,
        },
    }


def _discover_observation_artifacts(project_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for relative in _REPORT_ARTIFACTS:
        node = _artifact_node(project_dir, relative, "report_artifact")
        if node is not None:
            artifacts.append(node)
    for relative in _EVIDENCE_ARTIFACTS:
        node = _artifact_node(project_dir, relative, "evidence_artifact")
        if node is not None:
            artifacts.append(node)
    # Phase runners can create backend-specific root logs.  Discover only log names;
    # never open them or copy their contents into the graph.
    for path in sorted(project_dir.glob("*.log")):
        relative = _safe_relative(project_dir, path)
        if relative is None:
            continue
        node = _artifact_node(project_dir, relative, "evidence_artifact")
        if node is not None:
            artifacts.append(node)
    return artifacts


def _phase_for_artifact(relative: str) -> str | None:
    if relative in _EVIDENCE_PHASES:
        return _EVIDENCE_PHASES[relative]
    filename = Path(relative).name.lower()
    for phase in ("software_equivalence", "csim", "csynth", "cosim", "ppa"):
        if filename.startswith(phase):
            return phase
    return None


def _deduplicate_and_sort(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = {node["id"]: node for node in graph.get("nodes", [])}
    edges = {edge["id"]: edge for edge in graph.get("edges", [])}
    graph["nodes"] = sorted(nodes.values(), key=lambda node: node["id"])
    graph["edges"] = sorted(edges.values(), key=lambda edge: edge["id"])
    return graph


def _write_graph(path: Path, graph: dict[str, Any]) -> Path:
    graph = _deduplicate_and_sort(graph)
    path.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _add_observation_artifacts(graph: dict[str, Any], project_dir: Path) -> None:
    design_ids = [node["id"] for node in graph["nodes"] if node.get("kind") == "design"]
    design_id = design_ids[0] if design_ids else None
    phase_ids = {
        node.get("properties", {}).get("name"): node["id"]
        for node in graph["nodes"]
        if node.get("kind") == "verification_phase"
    }
    for node in _discover_observation_artifacts(project_dir):
        graph["nodes"].append(node)
        relative = node["properties"]["path"]
        if design_id is not None:
            relation = "HAS_REPORT" if node["kind"] == "report_artifact" else "HAS_EVIDENCE"
            _add_edge(graph["edges"], design_id, relation, node["id"])
        phase = _phase_for_artifact(relative)
        if phase in phase_ids:
            _add_edge(graph["edges"], phase_ids[phase], "PRODUCED_EVIDENCE", node["id"])


def write_knowledge_graph(
    project_dir: Path,
    analysis: Any,
    config: Any,
    state: Any | None = None,
    repair_history: Iterable[Any] = (),
) -> Path:
    """Write and return a per-project verification knowledge-graph path.

    ``analysis`` and ``config`` intentionally use structural typing so this writer can
    remain independent of orchestration code.  ``state`` is expected to expose a
    ``phases`` mapping whose values have ``status`` and optional ``log_path`` fields.
    Repair items may likewise be dataclasses or dictionaries.
    """

    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    function = _field(analysis, "function")
    top = str(_field(function, "name"))
    design_id = "design:project"
    top_id = f"function:{top}"
    nodes: list[dict[str, Any]] = [
        {
            "id": design_id,
            "kind": "design",
            "label": project_dir.name,
            "properties": {
                "clock_period": _field(config, "clock"),
                "cosim_backend": _field(config, "cosim_backend"),
                "part": _field(config, "part"),
                "rtl": _field(config, "rtl"),
                "seed": _field(config, "seed"),
                "test_count": _field(config, "num_tests"),
            },
        },
        {
            "id": top_id,
            "kind": "top_function",
            "label": top,
            "properties": {
                "name": top,
                "return_type": _safe_type_metadata(_field(function, "return_type")),
            },
        },
    ]
    edges: list[dict[str, str]] = []
    _add_edge(edges, design_id, "HAS_TOP", top_id)

    for argument in _field(function, "args", ()):
        name = str(_field(argument, "name"))
        argument_id = f"argument:{name}"
        scalar_range = _field(argument, "scalar_range")
        nodes.append(
            {
                "id": argument_id,
                "kind": "contract_argument",
                "label": name,
                "properties": {
                    "c_type": _safe_type_metadata(_field(argument, "c_type")),
                    "direction": _field(argument, "direction"),
                    "interface": _field(argument, "interface") or _field(config, "interface_mode"),
                    "is_const": bool(_field(argument, "is_const", False)),
                    "length": _field(argument, "length"),
                    "pointer_depth": int(_field(argument, "pointer_depth", 0)),
                    "scalar_range": list(scalar_range) if scalar_range is not None else None,
                },
            }
        )
        _add_edge(edges, top_id, "HAS_ARGUMENT", argument_id)

    for relative in _GENERATED_ARTIFACTS:
        node = _artifact_node(project_dir, relative, "generated_artifact")
        if node is not None:
            nodes.append(node)
            _add_edge(edges, design_id, "HAS_GENERATED_ARTIFACT", node["id"])

    repairs = list(repair_history)
    phases = dict(_field(state, "phases", {}) or {}) if state is not None else {}
    phase_names = list(_PHASE_ORDER)
    phase_names.extend(sorted(name for name in phases if name not in phase_names))
    phase_names.extend(
        sorted(
            {
                str(_field(repair, "stage"))
                for repair in repairs
                if _field(repair, "stage") and str(_field(repair, "stage")) not in phase_names
            }
        )
    )
    for order, name in enumerate(phase_names):
        properties: dict[str, Any] = {"name": name, "order": order}
        if state is not None:
            result = phases.get(name)
            properties["status"] = _normalise_phase_status(_field(result, "status", "skipped"))
            result_metadata = _field(result, "metadata", {})
            if isinstance(result_metadata, Mapping) and result_metadata.get("evidence_origin") in {
                "operator_assumption",
                "validated_external_report",
            }:
                properties["evidence_origin"] = result_metadata["evidence_origin"]
        if name == "symbolic_klee":
            properties.update(_safe_klee_properties(_field(phases.get(name), "metadata", {})))
        phase_id = f"phase:{name}"
        nodes.append(
            {
                "id": phase_id,
                "kind": "verification_phase",
                "label": _PHASE_LABELS.get(name, name.replace("_", " ").title()),
                "properties": properties,
            }
        )
        _add_edge(edges, top_id, "VERIFIED_BY", phase_id)
        if order:
            _add_edge(edges, f"phase:{phase_names[order - 1]}", "PRECEDES", phase_id)
        result = phases.get(name)
        log_path = _field(result, "log_path")
        if log_path:
            log_path = Path(log_path)
            if not log_path.is_absolute():
                log_path = project_dir / log_path
            relative = _safe_relative(project_dir, log_path)
            if relative:
                node = _artifact_node(project_dir, relative, "evidence_artifact")
                if node is not None:
                    nodes.append(node)
                    _add_edge(edges, phase_id, "PRODUCED_EVIDENCE", node["id"])

    for index, repair in enumerate(repairs):
        iteration = int(_field(repair, "iteration", index + 1))
        repair_id = f"repair:{iteration}:{index}"
        stage = _field(repair, "stage")
        changes = _field(repair, "changes", ()) or ()
        nodes.append(
            {
                "id": repair_id,
                "kind": "repair_outcome",
                "label": f"Repair {iteration}",
                "properties": {
                    "changed": bool(changes),
                    "family": _field(repair, "family"),
                    "iteration": iteration,
                    "outcome": _field(repair, "status"),
                    "owner_agent": _field(repair, "owner_agent"),
                    "stage": stage,
                    "target_files": sorted(str(path) for path in (_field(repair, "target_files", ()) or ())),
                },
            }
        )
        _add_edge(edges, design_id, "HAS_REPAIR_OUTCOME", repair_id)
        if stage:
            _add_edge(edges, repair_id, "ADDRESSES", f"phase:{stage}")
        for target in sorted(str(path) for path in (_field(repair, "target_files", ()) or ())):
            relative = _safe_relative(project_dir, project_dir / target)
            if relative is None or relative == "input.c":
                continue
            artifact_id = f"artifact:{relative}"
            if not any(node["id"] == artifact_id for node in nodes):
                node = _artifact_node(project_dir, relative, "generated_artifact")
                if node is not None:
                    nodes.append(node)
            if any(node["id"] == artifact_id for node in nodes):
                _add_edge(edges, repair_id, "MODIFIED", artifact_id)

    graph: dict[str, Any] = {"schema": SCHEMA, "nodes": nodes, "edges": edges}
    _add_observation_artifacts(graph, project_dir)
    return _write_graph(project_dir / FILENAME, graph)


def refresh_knowledge_graph(
    project_dir: Path,
    phase_updates: Mapping[str, Any] | None = None,
) -> Path:
    """Reconcile evidence/report artifact references in an existing graph.

    This is intended for report-only refreshes after conversion reporting, coverage,
    QoR, or PPA tools have emitted new files.  It reads only the graph structure and
    artifact metadata (path and size), never source or evidence file contents. Callers
    that produced a late phase result must pass its status through ``phase_updates`` so
    the graph cannot link fresh evidence while retaining a stale phase verdict.
    """

    project_dir = Path(project_dir)
    path = project_dir / FILENAME
    graph = json.loads(path.read_text(encoding="utf-8"))
    if graph.get("schema") != SCHEMA or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise ValueError(f"{path} is not a {SCHEMA} document")

    removed_ids = {
        node["id"]
        for node in graph["nodes"]
        if node.get("kind") in {"evidence_artifact", "report_artifact"}
    }
    graph["nodes"] = [node for node in graph["nodes"] if node.get("id") not in removed_ids]
    graph["edges"] = [
        edge
        for edge in graph["edges"]
        if edge.get("source") not in removed_ids and edge.get("target") not in removed_ids
    ]
    for node in graph["nodes"]:
        if node.get("kind") != "verification_phase":
            continue
        phase_name = node.get("properties", {}).get("name")
        if phase_updates and phase_name in phase_updates:
            update = phase_updates[phase_name]
            if isinstance(update, Mapping):
                if "status" in update:
                    node["properties"]["status"] = _normalise_phase_status(update["status"])
                if phase_name == "symbolic_klee":
                    node["properties"].update(_safe_klee_properties(update.get("metadata", {})))
            else:
                node["properties"]["status"] = _normalise_phase_status(update)
    _add_observation_artifacts(graph, project_dir)
    return _write_graph(path, graph)
