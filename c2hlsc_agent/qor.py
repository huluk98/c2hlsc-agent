"""QoR (Quality of Results) metrics: parsing, deltas, objective scoring, and reports.

Sources, in priority order:

- **Vitis csynth report** ``c2hlsc_project/solution1/syn/report/csynth.xml`` — latency,
  initiation interval, resource use (BRAM/DSP/FF/LUT/URAM), and the estimated clock.
  The remote-Vitis pull already brings this back to the local project.
- **Local PPA flow** (optional) — yosys ``stat`` area report and an OpenSTA timing/power
  report, as produced by the ``syn/run_ppa.sh`` recipe (Nangate45). Parsed when present
  so the QoR report can carry ASIC-style area/slack/power next to the FPGA estimates.

The renderers emit the delta table three ways: JSON (machines), Markdown (humans), and a
booktabs LaTeX table (papers).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

CSYNTH_XML_RELPATH = Path("c2hlsc_project/solution1/syn/report/csynth.xml")

# Rough single-number FPGA area proxy: DSP and BRAM are scarce relative to LUT/FF.
_AREA_WEIGHTS = {"lut": 1.0, "ff": 0.5, "dsp": 100.0, "bram": 100.0, "uram": 300.0}


@dataclass
class QoRMetrics:
    """One design point. All fields optional so partial reports still compare."""

    target_clock_ns: float | None = None
    estimated_clock_ns: float | None = None
    latency_best: int | None = None
    latency_worst: int | None = None
    interval_min: int | None = None
    interval_max: int | None = None
    bram: int | None = None
    dsp: int | None = None
    ff: int | None = None
    lut: int | None = None
    uram: int | None = None
    available: dict[str, int] = field(default_factory=dict)
    # optional local ASIC-style PPA (yosys + OpenSTA)
    yosys_cells: int | None = None
    yosys_area_um2: float | None = None
    sta_worst_slack_max_ns: float | None = None
    sta_worst_slack_min_ns: float | None = None
    sta_total_power_w: float | None = None
    # Post-place-and-route sign-off (Vivado, via export_design -flow impl). Kept as one
    # dict rather than a field per resource: these are measured sign-off numbers, not
    # scoring inputs, and they must stay out of area_proxy/qor_delta so the candidate
    # search cannot start optimizing against a metric that costs a P&R run to evaluate.
    impl: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "target_clock_ns": self.target_clock_ns,
            "estimated_clock_ns": self.estimated_clock_ns,
            "latency_best": self.latency_best,
            "latency_worst": self.latency_worst,
            "interval_min": self.interval_min,
            "interval_max": self.interval_max,
            "bram": self.bram,
            "dsp": self.dsp,
            "ff": self.ff,
            "lut": self.lut,
            "uram": self.uram,
            "available": dict(self.available),
            "yosys_cells": self.yosys_cells,
            "yosys_area_um2": self.yosys_area_um2,
            "sta_worst_slack_max_ns": self.sta_worst_slack_max_ns,
            "sta_worst_slack_min_ns": self.sta_worst_slack_min_ns,
            "sta_total_power_w": self.sta_total_power_w,
            "impl": dict(self.impl),
        }

    @property
    def area_proxy(self) -> float | None:
        parts = []
        for name, weight in _AREA_WEIGHTS.items():
            value = getattr(self, name)
            if value is not None:
                parts.append(weight * value)
        return sum(parts) if parts else None

    @property
    def timing_met(self) -> bool | None:
        if self.estimated_clock_ns is None or self.target_clock_ns is None:
            return None
        return self.estimated_clock_ns <= self.target_clock_ns


def find_csynth_xml(project_dir: Path) -> Path | None:
    path = project_dir / CSYNTH_XML_RELPATH
    if path.exists():
        return path
    # Vitis layouts can vary slightly; fall back to a bounded search.
    hits = sorted((project_dir / "c2hlsc_project").glob("*/syn/report/csynth.xml"))
    return hits[0] if hits else None


def _xml_text(root: ET.Element, *paths: str) -> str | None:
    for path in paths:
        node = root.find(path)
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return None


def _to_int(text: str | None) -> int | None:
    try:
        return int(float(text)) if text is not None else None
    except ValueError:
        return None


def _to_float(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def parse_csynth_xml(path: Path) -> QoRMetrics:
    """Parse a Vitis HLS ``csynth.xml`` synthesis report into :class:`QoRMetrics`.

    Raises ``RuntimeError`` (not ``xml.etree`` internals) on a malformed report so
    callers can degrade gracefully instead of crashing mid-optimization.
    """

    try:
        root = ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
    except ET.ParseError as exc:
        raise RuntimeError(f"malformed Vitis synthesis report {path}: {exc}") from exc
    perf = "PerformanceEstimates"
    metrics = QoRMetrics(
        target_clock_ns=_to_float(_xml_text(root, f"{perf}/SummaryOfTimingAnalysis/TargetClockPeriod")),
        estimated_clock_ns=_to_float(_xml_text(root, f"{perf}/SummaryOfTimingAnalysis/EstimatedClockPeriod")),
        latency_best=_to_int(_xml_text(root, f"{perf}/SummaryOfOverallLatency/Best-caseLatency")),
        latency_worst=_to_int(_xml_text(root, f"{perf}/SummaryOfOverallLatency/Worst-caseLatency")),
        interval_min=_to_int(_xml_text(root, f"{perf}/SummaryOfOverallLatency/Interval-min")),
        interval_max=_to_int(_xml_text(root, f"{perf}/SummaryOfOverallLatency/Interval-max")),
    )
    resources = root.find("AreaEstimates/Resources")
    if resources is not None:
        metrics.bram = _to_int(_xml_text(resources, "BRAM_18K"))
        metrics.dsp = _to_int(_xml_text(resources, "DSP", "DSP48E"))
        metrics.ff = _to_int(_xml_text(resources, "FF"))
        metrics.lut = _to_int(_xml_text(resources, "LUT"))
        metrics.uram = _to_int(_xml_text(resources, "URAM"))
    available = root.find("AreaEstimates/AvailableResources")
    if available is not None:
        for child in available:
            value = _to_int(child.text)
            if value is not None:
                metrics.available[child.tag] = value
    return metrics


def parse_yosys_area(path: Path, metrics: QoRMetrics | None = None) -> QoRMetrics:
    """Parse a yosys ``stat`` area report (chip area + cell count) into ``metrics``."""

    metrics = metrics or QoRMetrics()
    text = path.read_text(encoding="utf-8", errors="replace")
    # yosys `stat` emits one "Chip area for module 'X'" line per module and a single
    # "Chip area for top module 'T'" line carrying the whole-design total. On a flat
    # netlist there is only the top line; on a hierarchical one (e.g. Bambu RTL, whose
    # top is a thin wrapper over a `_<top>` datapath) the submodule lines come FIRST, so
    # a plain first-match grabs a functional-unit's area, not the design's. Prefer the
    # "top module" total; fall back to the first per-module line only if it is absent.
    area = re.search(r"Chip area for top module '[^']*':\s*([0-9.]+(?:[eE][+-]?\d+)?)", text)
    if area is None:
        area = re.search(r"Chip area for module '[^']*':\s*([0-9.]+(?:[eE][+-]?\d+)?)", text)
    if area:
        metrics.yosys_area_um2 = float(area.group(1))
    # Three yosys `stat` shapes:
    #  - classic "Number of cells: N"
    #  - with -liberty, a total row carrying area: "759  958.398 cells" (or sci-notation
    #    "606 4.65E+03 cells" once the area passes 4 digits)
    #  - without -liberty (multi-liberty nodes like asap7 use plain `stat`), a bare
    #    total row with no area column: "655 cells"
    cells = re.search(r"Number of cells:\s*(\d+)", text)
    if cells is None:
        cells = re.search(r"^\s*(\d+)\s+[0-9.]+(?:[eE][+-]?\d+)?\s+cells\s*$", text, re.M)
    if cells is None:
        cells = re.search(r"^\s*(\d+)\s+cells\s*$", text, re.M)
    if cells:
        metrics.yosys_cells = int(cells.group(1))
    return metrics


def parse_sta_report(path: Path, metrics: QoRMetrics | None = None) -> QoRMetrics:
    """Parse an OpenSTA report (worst slack + total power) into ``metrics``."""

    metrics = metrics or QoRMetrics()
    text = path.read_text(encoding="utf-8", errors="replace")
    slack_max = re.search(r"worst slack max\s+(-?[0-9.]+)", text)
    if slack_max:
        metrics.sta_worst_slack_max_ns = float(slack_max.group(1))
    slack_min = re.search(r"worst slack min\s+(-?[0-9.]+)", text)
    if slack_min:
        metrics.sta_worst_slack_min_ns = float(slack_min.group(1))
    # report_power table: the Total row's Total-power column (watts, sci notation).
    # Deliberately NO free-text fallback: a bare "power 1nW" line in OpenSTA output is
    # the report_units declaration (a unit, not a measurement) and must not be parsed.
    total_row = re.search(r"^Total\s+.*?([0-9.]+e[+-]?\d+)\s+100\.?\d*%", text, re.M)
    if total_row:
        metrics.sta_total_power_w = float(total_row.group(1))
    return metrics


_IMPL_RESOURCE_KEYS = {
    "SLICE": "slice",
    "LUT": "lut",
    "FF": "ff",
    "DSP": "dsp",
    "BRAM": "bram",
    "SRL": "srl",
    "URAM": "uram",
    "LATCH": "latch",
}


def find_impl_report(project_dir: Path) -> Path | None:
    """Locate the Vivado post-implementation report written by ``export_design -flow impl``.

    The filename and language subdirectory moved between Vitis versions, so prefer the
    canonical ``export_impl.rpt`` and fall back to any report under ``impl/report/``.
    """

    hits = sorted(project_dir.glob("c2hlsc_project/*/impl/report/**/*.rpt"))
    if not hits:
        return None
    for path in hits:
        if path.name == "export_impl.rpt":
            return path
    return hits[0]


def parse_impl_report(path: Path, metrics: QoRMetrics | None = None) -> QoRMetrics:
    """Parse post-place-and-route resources and the achieved clock period.

    These are the only *measured* FPGA numbers the pipeline produces — every other
    resource and timing figure in a report is a csynth estimate. Raises when the file
    carries no recognisable result block, so an empty or truncated report becomes a
    loud failure rather than a silently empty sign-off.
    """

    metrics = metrics or QoRMetrics()
    text = path.read_text(encoding="utf-8", errors="replace")
    impl: dict[str, object] = {}
    for key, name in _IMPL_RESOURCE_KEYS.items():
        found = re.search(rf"^\s*-\s*{key}:\s*(\d+)\s*$", text, re.M | re.I)
        if found:
            impl[name] = int(found.group(1))
    achieved = re.search(r"CP achieved post-implementation:\s*(-?[0-9.]+)", text, re.I)
    if achieved:
        impl["cp_achieved_ns"] = float(achieved.group(1))
    required = re.search(r"CP required:\s*(-?[0-9.]+)", text, re.I)
    if required:
        impl["cp_required_ns"] = float(required.group(1))
    if not impl:
        raise RuntimeError(f"no post-implementation results found in {path}")
    impl["report"] = path.name
    metrics.impl = impl
    return metrics


def collect_local_ppa(project_dir: Path, metrics: QoRMetrics) -> QoRMetrics:
    """Best-effort enrichment from the local yosys/OpenSTA flow, when its reports exist."""

    yosys_rpt = project_dir / "syn" / "yosys_area.rpt"
    if yosys_rpt.exists():
        parse_yosys_area(yosys_rpt, metrics)
    sta_rpt = project_dir / "syn" / "sta_report.txt"
    if sta_rpt.exists():
        parse_sta_report(sta_rpt, metrics)
    return metrics


# --------------------------------------------------------------------------- #
# Deltas and objective scoring
# --------------------------------------------------------------------------- #

_DELTA_FIELDS = (
    "latency_worst",
    "latency_best",
    "interval_max",
    "interval_min",
    "estimated_clock_ns",
    "lut",
    "ff",
    "dsp",
    "bram",
    "uram",
    "yosys_area_um2",
    "sta_worst_slack_max_ns",
    "sta_total_power_w",
)


def qor_delta(baseline: QoRMetrics, candidate: QoRMetrics) -> dict[str, dict[str, object]]:
    """Per-metric ``{baseline, candidate, delta, pct}`` for every comparable field."""

    delta: dict[str, dict[str, object]] = {}
    for name in _DELTA_FIELDS:
        base = getattr(baseline, name)
        cand = getattr(candidate, name)
        if base is None or cand is None:
            continue
        diff = cand - base
        pct = (diff / base * 100.0) if base else None
        delta[name] = {"baseline": base, "candidate": cand, "delta": diff, "pct": pct}
    return delta


OBJECTIVES = ("latency", "area", "balanced")


@dataclass(frozen=True)
class PPATargets:
    """Explicit targets the optimization loop iterates toward. All optional; a run with
    no targets behaves like classic single-pass optimization."""

    max_latency_cycles: int | None = None  # worst-case latency (Vitis csynth)
    min_slack_ns: float | None = None  # worst setup slack (OpenSTA on the mapped netlist)
    max_area_um2: float | None = None  # std-cell area (yosys stat)
    max_power_w: float | None = None  # total power (OpenSTA report_power)

    @property
    def specified(self) -> bool:
        return any(v is not None for v in (self.max_latency_cycles, self.min_slack_ns, self.max_area_um2, self.max_power_w))

    @property
    def needs_local_ppa(self) -> bool:
        return any(v is not None for v in (self.min_slack_ns, self.max_area_um2, self.max_power_w))

    def to_dict(self) -> dict[str, object]:
        return {
            "max_latency_cycles": self.max_latency_cycles,
            "min_slack_ns": self.min_slack_ns,
            "max_area_um2": self.max_area_um2,
            "max_power_w": self.max_power_w,
        }


def targets_from_config(config: object) -> PPATargets:
    """Build :class:`PPATargets` from the config's ``ppa:`` criteria block, so the
    workflow gates on the same numbers whether driven by CLI flags or config."""

    return PPATargets(
        max_latency_cycles=getattr(config, "max_latency_cycles", None),
        min_slack_ns=getattr(config, "min_slack", None),
        max_area_um2=getattr(config, "max_area_um2", None),
        max_power_w=getattr(config, "max_power_w", None),
    )


def slack_headroom(metrics: QoRMetrics, targets: PPATargets | None) -> float | None:
    """The iteration budget: measured worst setup slack minus the ``min_slack`` floor
    (or minus zero when no floor is set). Positive headroom is timing budget that can
    be spent on functionality or frequency; ``None`` when slack was not measured."""

    if metrics.sta_worst_slack_max_ns is None:
        return None
    floor = targets.min_slack_ns if targets is not None and targets.min_slack_ns is not None else 0.0
    return metrics.sta_worst_slack_max_ns - floor


def evaluate_targets(metrics: QoRMetrics, targets: PPATargets, time_unit: str = "ns") -> tuple[bool, list[str], float]:
    """Check ``metrics`` against ``targets``.

    Returns ``(all_met, gap_descriptions, gap_score)`` where ``gap_score`` sums the
    normalized shortfalls (0.0 when every specified target is met; a target whose metric
    is missing counts as fully unmet, 1.0). Lower gap_score = closer to the targets.
    ``time_unit`` (ns/ps) labels the slack gap message on the active process node —
    ``min_slack_ns`` is a field name, not the physical unit, which is ps on asap7.
    """

    gaps: list[str] = []
    gap_score = 0.0

    def check(name: str, value: float | None, limit: float, kind: str) -> None:
        nonlocal gap_score
        if value is None:
            gaps.append(f"{name}: no measurement yet (target {kind} {limit})")
            gap_score += 1.0
            return
        if kind == "<=" and value > limit:
            short = (value - limit) / max(abs(limit), 1e-12)
            gaps.append(f"{name}: {value:g} exceeds target {limit:g} (over by {short * 100:.1f}%)")
            gap_score += min(short, 1.0)
        elif kind == ">=" and value < limit:
            short = (limit - value) / max(abs(limit), 1e-12)
            gaps.append(f"{name}: {value:g} below target {limit:g} (short by {short * 100:.1f}%)")
            gap_score += min(short, 1.0)

    if targets.max_latency_cycles is not None:
        latency = metrics.latency_worst if metrics.latency_worst is not None else metrics.interval_max
        check("latency (worst cycles)", latency, float(targets.max_latency_cycles), "<=")
    if targets.min_slack_ns is not None:
        check(f"worst setup slack ({time_unit})", metrics.sta_worst_slack_max_ns, targets.min_slack_ns, ">=")
    if targets.max_area_um2 is not None:
        check("std-cell area (um^2)", metrics.yosys_area_um2, targets.max_area_um2, "<=")
    if targets.max_power_w is not None:
        check("total power (W)", metrics.sta_total_power_w, targets.max_power_w, "<=")
    return (not gaps, gaps, gap_score)


def objective_score(metrics: QoRMetrics, objective: str, baseline: QoRMetrics | None = None) -> float | None:
    """Scalar score, LOWER is better. ``None`` when the needed metrics are missing.

    - ``latency``: worst-case latency (fallback: max initiation interval).
    - ``area``: weighted FPGA resource proxy (LUT + FF/2 + 100*DSP + 100*BRAM + 300*URAM).
    - ``balanced``: geometric-mean-style product of latency and area ratios vs the
      baseline (requires a baseline; equals 1.0 for the baseline itself).
    """

    if objective == "latency":
        value = metrics.latency_worst if metrics.latency_worst is not None else metrics.interval_max
        return float(value) if value is not None else None
    if objective == "area":
        return metrics.area_proxy
    if objective == "balanced":
        if baseline is None:
            return 1.0
        lat = objective_score(metrics, "latency")
        base_lat = objective_score(baseline, "latency")
        area = metrics.area_proxy
        base_area = baseline.area_proxy
        if None in (lat, base_lat, area, base_area) or 0 in (base_lat, base_area):
            return None
        return (lat / base_lat) * (area / base_area)
    raise ValueError(f"unknown objective {objective!r} (expected one of {OBJECTIVES})")


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #

_ROW_LABELS = {
    "latency_worst": ("Latency (worst, cycles)", "{:d}"),
    "latency_best": ("Latency (best, cycles)", "{:d}"),
    "interval_max": ("Initiation interval (max)", "{:d}"),
    "estimated_clock_ns": ("Estimated clock (ns)", "{:.2f}"),
    "lut": ("LUT", "{:d}"),
    "ff": ("FF", "{:d}"),
    "dsp": ("DSP", "{:d}"),
    "bram": ("BRAM\\_18K", "{:d}"),
    "uram": ("URAM", "{:d}"),
    "yosys_area_um2": ("Std-cell area ($\\mu m^2$)", "{:.1f}"),
    "sta_worst_slack_max_ns": ("Worst setup slack (ns)", "{:.2f}"),
    "sta_total_power_w": ("Total power (W)", "{:.3e}"),
}


def _fmt(fmt: str, value: object) -> str:
    if value is None:
        return "--"
    if fmt == "{:d}":
        return f"{int(value)}"
    return fmt.format(value)


def render_latex_table(delta: dict[str, dict[str, object]], caption: str, label: str = "tab:qor") -> str:
    """A paper-ready booktabs baseline-vs-optimized QoR table."""

    lines = [
        "\\begin{table}[t]",
        "  \\centering",
        f"  \\caption{{{caption}}}",
        f"  \\label{{{label}}}",
        "  \\begin{tabular}{lrrr}",
        "    \\toprule",
        "    Metric & Baseline & Optimized & $\\Delta$ (\\%) \\\\",
        "    \\midrule",
    ]
    for name, (row_label, fmt) in _ROW_LABELS.items():
        if name not in delta:
            continue
        row = delta[name]
        pct = row["pct"]
        pct_text = "--" if pct is None else f"{pct:+.1f}"
        lines.append(
            f"    {row_label} & {_fmt(fmt, row['baseline'])} & {_fmt(fmt, row['candidate'])} & {pct_text} \\\\"
        )
    lines += ["    \\bottomrule", "  \\end{tabular}", "\\end{table}", ""]
    return "\n".join(lines)


def render_markdown(delta: dict[str, dict[str, object]], title: str) -> str:
    lines = [f"# {title}", "", "| Metric | Baseline | Optimized | Delta (%) |", "|---|---|---|---|"]
    for name, (row_label, fmt) in _ROW_LABELS.items():
        if name not in delta:
            continue
        row = delta[name]
        pct = row["pct"]
        pct_text = "--" if pct is None else f"{pct:+.1f}"
        plain_label = row_label.replace("\\_", "_").replace("$\\mu m^2$", "um^2")
        lines.append(f"| {plain_label} | {_fmt(fmt, row['baseline'])} | {_fmt(fmt, row['candidate'])} | {pct_text} |")
    lines.append("")
    return "\n".join(lines)
