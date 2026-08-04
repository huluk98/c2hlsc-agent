"""Process-node (工艺节点) registry for the local synthesis + STA flow.

The node is a first-class workflow criterion: configs declare ``ppa.node`` and every
slack/area/power number the pipeline gates on is measured against that node's liberty
libraries. Three open PDKs are wired in:

- ``nangate45`` — 45 nm predictive (FreePDK45). The citable baseline used across the
  HLS/EDA literature; **the default**. Not manufacturable.
- ``sky130hd`` — SkyWater 130 nm high-density. A real foundry PDK (chipIgnite / MPW
  shuttle path); the choice when a design might actually tape out.
- ``asap7`` — ASU 7 nm predictive FinFET (RVT, typical). The modern-node scaling
  datapoint. Not manufacturable.

Liberty files are not vendored into the repo. They resolve, in order, from the
project's ``syn/lib``, the shared cache (``$C2HLSC_PDK_DIR`` or ``~/.c2hlsc/pdk``),
and finally an on-demand download from OpenROAD-flow-scripts (raw.githubusercontent
with a jsDelivr fallback — GitHub raw resets long transfers often enough that the
fallback is load-bearing).

All node timing quantities are expressed in the node's liberty **time unit** (ns for
nangate45/sky130hd, ps for asap7); ``scale_from_ns`` converts the config's ns clock.
Slack targets (``ppa.min_slack``) are likewise in node time units.
"""

from __future__ import annotations

import gzip
import http.client
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Every transport failure a truncated/early-closed HTTP transfer can raise. GitHub
# raw resetting mid-stream surfaces as http.client.IncompleteRead, which is an
# HTTPException — NOT an OSError — so it must be listed explicitly or the jsDelivr
# fallback never engages and the exception escapes run_local_ppa.
_DOWNLOAD_ERRORS = (OSError, EOFError, urllib.error.URLError, http.client.HTTPException)

_ORFS_RAW = "https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms"
_ORFS_CDN = "https://cdn.jsdelivr.net/gh/The-OpenROAD-Project/OpenROAD-flow-scripts@master/flow/platforms"


@dataclass(frozen=True)
class NodeSpec:
    """One process node the PPA flow can target."""

    name: str
    nm: int
    description: str
    manufacturable: bool
    time_unit: str  # liberty time unit: "ns" | "ps"
    scale_from_ns: float  # multiply a ns quantity by this to get node time units
    output_load: float  # set_load value, in the liberty's capacitance unit
    dff_liberty: str  # file for dfflibmap
    abc_liberties: tuple[str, ...]  # files for abc (combinational mapping)
    # filename -> URL-path suffixes under the ORFS platforms tree; ".gz" suffixes are
    # decompressed after download. STA reads every file listed here.
    files: dict[str, str] = field(default_factory=dict)

    @property
    def liberty_files(self) -> tuple[str, ...]:
        return tuple(self.files)

    def clock_period(self, clock_ns: float) -> float:
        return clock_ns * self.scale_from_ns

    def io_delay(self, clock_ns: float) -> float:
        return round(0.2 * self.clock_period(clock_ns), 4)

    def uncertainty_setup(self, clock_ns: float) -> float:
        return round(0.01 * self.clock_period(clock_ns), 4)

    def uncertainty_hold(self, clock_ns: float) -> float:
        return round(0.005 * self.clock_period(clock_ns), 4)


NODES: dict[str, NodeSpec] = {
    "nangate45": NodeSpec(
        name="nangate45",
        nm=45,
        description="Nangate/FreePDK45 45 nm predictive — citable open baseline (default)",
        manufacturable=False,
        time_unit="ns",
        scale_from_ns=1.0,
        output_load=0.02,
        dff_liberty="NangateOpenCellLibrary_typical.lib",
        abc_liberties=("NangateOpenCellLibrary_typical.lib",),
        files={"NangateOpenCellLibrary_typical.lib": "nangate45/lib/NangateOpenCellLibrary_typical.lib"},
    ),
    "sky130hd": NodeSpec(
        name="sky130hd",
        nm=130,
        description="SkyWater 130 nm HD — real foundry PDK, manufacturable (chipIgnite/MPW)",
        manufacturable=True,
        time_unit="ns",
        scale_from_ns=1.0,
        output_load=0.02,
        dff_liberty="sky130_fd_sc_hd__tt_025C_1v80.lib",
        abc_liberties=("sky130_fd_sc_hd__tt_025C_1v80.lib",),
        files={"sky130_fd_sc_hd__tt_025C_1v80.lib": "sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"},
    ),
    "asap7": NodeSpec(
        name="asap7",
        nm=7,
        description="ASAP7 7 nm predictive FinFET (RVT tt) — modern-node scaling datapoint",
        manufacturable=False,
        time_unit="ps",
        scale_from_ns=1000.0,
        output_load=10.0,
        dff_liberty="asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib",
        abc_liberties=(
            "asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib",
            "asap7sc7p5t_AO_RVT_TT_nldm_211120.lib",
            "asap7sc7p5t_OA_RVT_TT_nldm_211120.lib",
            "asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib",
        ),
        files={
            "asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib": "asap7/lib/NLDM/asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib.gz",
            "asap7sc7p5t_AO_RVT_TT_nldm_211120.lib": "asap7/lib/NLDM/asap7sc7p5t_AO_RVT_TT_nldm_211120.lib.gz",
            "asap7sc7p5t_OA_RVT_TT_nldm_211120.lib": "asap7/lib/NLDM/asap7sc7p5t_OA_RVT_TT_nldm_211120.lib.gz",
            "asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib": "asap7/lib/NLDM/asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib.gz",
            "asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib": "asap7/lib/NLDM/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib",
        },
    ),
}


def resolve_node(name: str) -> NodeSpec:
    try:
        return NODES[name]
    except KeyError:
        raise ValueError(f"unknown process node {name!r}; expected one of {', '.join(sorted(NODES))}") from None


def pdk_cache_dir() -> Path:
    env = os.environ.get("C2HLSC_PDK_DIR")
    return Path(env).expanduser() if env else Path.home() / ".c2hlsc" / "pdk"


def _download(url: str, dest: Path, timeout: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "c2hlsc-agent"})
    with urllib.request.urlopen(request, timeout=timeout) as response, open(dest, "wb") as sink:
        shutil.copyfileobj(response, sink)


def _fetch_file(url_suffix: str, dest: Path, timeout: int = 120) -> str | None:
    """Fetch one liberty (gunzipping ``.gz``) into ``dest``; returns an error note or None.

    ``dest`` is written atomically: the download lands in a ``.part`` file and the
    decompression (when ``.gz``) targets a second ``.part`` file; only a fully
    completed file is ``os.replace``'d onto ``dest``. An interrupted download OR an
    interrupted/corrupt gunzip therefore never leaves a truncated liberty at ``dest``
    that ``locate_liberties`` would later resolve as valid.
    """

    compressed = url_suffix.endswith(".gz")
    raw_tmp = dest.with_suffix(dest.suffix + (".gz.part" if compressed else ".part"))
    out_tmp = dest.with_suffix(dest.suffix + ".out.part")
    errors: list[str] = []
    for base in (_ORFS_RAW, _ORFS_CDN):
        try:
            _download(f"{base}/{url_suffix}", raw_tmp, timeout)
            if compressed:
                with gzip.open(raw_tmp, "rb") as src, open(out_tmp, "wb") as sink:
                    shutil.copyfileobj(src, sink)
                os.replace(out_tmp, dest)  # atomic; dest only ever sees a complete file
                raw_tmp.unlink(missing_ok=True)
            else:
                os.replace(raw_tmp, dest)
            return None
        except _DOWNLOAD_ERRORS as exc:
            errors.append(f"{base.split('/')[2]}: {exc}")
        finally:
            raw_tmp.unlink(missing_ok=True)
            out_tmp.unlink(missing_ok=True)
    return f"download failed for {url_suffix} ({'; '.join(errors)})"


def locate_liberties(
    node: NodeSpec, project_dir: Path | None = None, fetch: bool = True
) -> tuple[dict[str, Path], str | None]:
    """Resolve every liberty file of ``node``.

    Search order per file: ``<project>/syn/lib`` -> PDK cache -> download into the
    cache (when ``fetch``). Returns ``(name -> path, error_note)``; the mapping is
    complete only when the note is ``None``.
    """

    cache = pdk_cache_dir() / node.name
    resolved: dict[str, Path] = {}
    for filename, url_suffix in node.files.items():
        candidates = []
        if project_dir is not None:
            candidates.append(project_dir / "syn" / "lib" / filename)
        candidates.append(cache / filename)
        found = next((p for p in candidates if p.exists() and p.stat().st_size > 0), None)
        if found is None and fetch:
            cache.mkdir(parents=True, exist_ok=True)
            note = _fetch_file(url_suffix, cache / filename)
            if note is None:
                found = cache / filename
            else:
                return resolved, note
        if found is None:
            return resolved, f"liberty {filename} not found (searched project syn/lib and {cache}; fetch disabled)"
        resolved[filename] = found
    return resolved, None
