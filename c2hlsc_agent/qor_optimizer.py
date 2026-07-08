"""rtl_optimizer_agent, live: post-equivalence QoR (PPA) optimization.

Operates on a project that already passes the verification ladder. Loop:

1. Establish the baseline QoR from the Vitis csynth report (running csim+csynth if the
   report is not present yet).
2. Propose candidates: one deterministic pragma candidate (PIPELINE II=1 on innermost
   loops) plus LLM candidates grounded in the synthesis report, the objective, and the
   already-tried history.
3. Gate each candidate in an isolated scratch project: local host equivalence first
   (seconds, no Vitis), then csim+csynth (local or over --vitis-ssh) to score it from
   its csynth report. Candidates that regress timing past the target clock are excluded.
4. Promote the best strictly-improving candidate into the project and re-run the FULL
   ladder (host equivalence -> CSim -> CSynth -> CoSim). Acceptance requires the ladder
   to pass; otherwise the original source is restored.
5. Emit the QoR delta report as JSON, Markdown, and a paper-ready LaTeX table, optionally
   enriched by the local yosys/OpenSTA PPA flow (--ppa-script).

The LLM only proposes; equivalence and the Vitis reports decide, matching the
equivalence-first contract of the rest of the pipeline.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .analyze import AnalysisResult
from .config import AgentConfig
from .hls_runner import run_software_equivalence, run_vitis, verify_project
from .llm import LLMClient, build_qor_prompt, extract_full_file, is_plausible_translation_unit
from .qor import (
    QoRMetrics,
    collect_local_ppa,
    find_csynth_xml,
    objective_score,
    parse_csynth_xml,
    qor_delta,
    render_latex_table,
    render_markdown,
)
from .remote import RemoteVitis
from .report import final_status

OPTIMIZER_AGENT_NAME = "rtl_optimizer_agent"
QOR_DIRNAME = ".qor"
PRE_QOR_BACKUP = "hls_top.cpp.pre_qor"

# The candidate gate only needs input.c, src/, tb/, Makefile and the TCL scripts.
# Heavy artifact dirs (local PPA outputs, waveforms, coverage, RTL golden vectors) are
# excluded — copying them costs ~tens of MB per candidate on a real project.
_STAGE_IGNORES = shutil.ignore_patterns(
    "c2hlsc_project", QOR_DIRNAME, ".candidates", "*.log", "qor_report.*", "qor_table.tex",
    "syn", "waves", "coverage", "rtl_vectors", "*.vcd",
)


@dataclass
class CandidateResult:
    index: int
    kind: str  # "deterministic-pipeline" | "llm"
    status: str  # scored | equiv_fail | csim_fail | csynth_fail | unparsable | duplicate | timing_regressed
    source_sha: str | None = None
    score: float | None = None
    metrics: QoRMetrics | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "kind": self.kind,
            "status": self.status,
            "source_sha": self.source_sha,
            "score": self.score,
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "note": self.note,
        }


@dataclass
class OptimizeOutcome:
    objective: str
    baseline: QoRMetrics
    candidates: list[CandidateResult] = field(default_factory=list)
    winner_index: int | None = None
    accepted: bool = False
    rolled_back: bool = False
    delta: dict[str, dict[str, object]] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "agent": OPTIMIZER_AGENT_NAME,
            "objective": self.objective,
            "baseline": self.baseline.to_dict(),
            "candidates": [c.to_dict() for c in self.candidates],
            "winner_index": self.winner_index,
            "accepted": self.accepted,
            "rolled_back": self.rolled_back,
            "delta": self.delta,
            "summary": self.summary,
        }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pipeline_innermost_loops(source: str) -> str | None:
    """Deterministic candidate: ``#pragma HLS PIPELINE II=1`` inside innermost for-loops.

    A loop is innermost when its body (tracked by brace depth) contains no nested ``for``.
    Loops whose body already carries a PIPELINE pragma are left alone. Returns ``None``
    when the transform changes nothing (no loops found, or all already pipelined).
    """

    lines = source.splitlines()
    # Allow a trailing // comment after the opening brace; require the brace on the
    # header line (brace-on-next-line and unbraced loops are conservatively skipped).
    header = re.compile(r"^(\s*)for\s*\(.*\)\s*\{\s*(//.*)?$")
    # (insert_at_line, indent, depth_at_body, has_nested_for, has_pipeline)
    open_loops: list[dict[str, object]] = []
    insertions: list[tuple[int, str]] = []
    depth = 0
    in_block_comment = False
    for idx, line in enumerate(lines):
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
            continue
        if "/*" in line and "*/" not in line[line.index("/*") :]:
            in_block_comment = True
            continue
        match = header.match(line)
        if match:
            for loop in open_loops:
                loop["nested"] = True
        depth += line.count("{")
        if match:
            open_loops.append({"line": idx, "indent": match.group(1), "body_depth": depth, "nested": False, "pipelined": False})
        upper = line.upper()
        if "#PRAGMA" in upper and "PIPELINE" in upper:
            for loop in open_loops:
                loop["pipelined"] = True
        depth -= line.count("}")
        while open_loops and depth < open_loops[-1]["body_depth"]:
            loop = open_loops.pop()
            if not loop["nested"] and not loop["pipelined"]:
                insertions.append((loop["line"] + 1, f"{loop['indent']}  #pragma HLS PIPELINE II=1"))
    if not insertions:
        return None
    for line_no, text in sorted(insertions, reverse=True):
        lines.insert(line_no, text)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def _stage_candidate(project_dir: Path, index: int, source: str) -> Path:
    cand_dir = project_dir / QOR_DIRNAME / f"cand_{index}"
    if cand_dir.exists():
        shutil.rmtree(cand_dir)
    shutil.copytree(project_dir, cand_dir, ignore=_STAGE_IGNORES)
    (cand_dir / "src" / "hls_top.cpp").write_text(source, encoding="utf-8")
    return cand_dir


def _synth_metrics(project_dir: Path, remote: RemoteVitis | None) -> tuple[QoRMetrics | None, str]:
    """csim+csynth the project and parse its csynth report. Returns (metrics, fail_note)."""

    phases = run_vitis(project_dir, True, remote=remote, upto="csynth")
    if phases["csim"].status != "pass":
        return None, f"csim_fail: {phases['csim'].summary or 'csim failed'}"
    if phases["csynth"].status != "pass":
        return None, f"csynth_fail: {phases['csynth'].summary or 'csynth failed'}"
    xml = find_csynth_xml(project_dir)
    if xml is None:
        return None, "csynth passed but no csynth.xml report was found (remote pull incomplete?)"
    try:
        return parse_csynth_xml(xml), ""
    except RuntimeError as exc:
        return None, f"csynth_fail: {exc}"


def _report_is_fresh(project_dir: Path, xml: Path) -> bool:
    """A csynth report is only trusted when it is at least as new as the sources it
    claims to describe. Stale reports arise from ``repair`` (rewrites src without
    re-running Vitis) and from this optimizer's own rollback path."""

    try:
        report_mtime = xml.stat().st_mtime
        newest_input = max(
            p.stat().st_mtime
            for p in (project_dir / "src" / "hls_top.cpp", project_dir / "tb" / "testbench.cpp")
            if p.exists()
        )
    except (OSError, ValueError):
        return False
    return report_mtime >= newest_input


def _metrics_text(metrics: QoRMetrics) -> str:
    payload = {k: v for k, v in metrics.to_dict().items() if v not in (None, {}, [])}
    return json.dumps(payload, indent=2)


_PRAGMA_RE = re.compile(r"#pragma\s+HLS\s+(\w+)[^\n]*", re.I)


def _pragma_summary(baseline_source: str, candidate_source: str) -> str:
    """Human/LLM-readable strategy descriptor: which HLS pragmas the candidate added."""

    def count(source: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for match in _PRAGMA_RE.finditer(source):
            key = match.group(1).upper()
            counts[key] = counts.get(key, 0) + 1
        return counts

    base, cand = count(baseline_source), count(candidate_source)
    added = [f"{name} x{cand[name] - base.get(name, 0)}" for name in sorted(cand) if cand[name] > base.get(name, 0)]
    removed = [f"{name}" for name in sorted(base) if base[name] > cand.get(name, 0)]
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    return "; ".join(parts) or "no pragma changes (refactor only)"


def _llm_candidate_source(
    analysis: AnalysisResult,
    config: AgentConfig,
    llm: LLMClient,
    current_source: str,
    baseline: QoRMetrics,
    objective: str,
    history: list[dict[str, object]],
    attempt: int = 0,
) -> tuple[str | None, str]:
    system, user = build_qor_prompt(
        analysis,
        current_source,
        _metrics_text(baseline),
        objective,
        history=history,
        nl_spec=getattr(config, "nl_spec", None),
        attempt=attempt,
    )
    try:
        response = llm.complete(system, user)
    except Exception as exc:  # noqa: BLE001 — recorded, not fatal
        return None, f"llm_error: {type(exc).__name__}: {exc}"
    top = analysis.function.name
    source = extract_full_file(response, must_contain=f"{top}(")
    if not source or not is_plausible_translation_unit(source, top):
        return None, "unparsable model response"
    if '#include "hls_top.hpp"' not in source:
        source = f'#include "hls_top.hpp"\n\n{source}'
    return source, ""


def optimize_project(
    project_dir: Path,
    analysis: AnalysisResult,
    config: AgentConfig,
    llm: LLMClient | None,
    remote: RemoteVitis | None,
    objective: str = "latency",
    iterations: int = 4,
    cosim_winner: bool = True,
    ppa_script: str | None = None,
    verbose: bool = False,
) -> OptimizeOutcome:
    """Run the post-equivalence QoR loop on ``project_dir`` and write the QoR reports."""

    src_path = project_dir / "src" / "hls_top.cpp"
    baseline_source = src_path.read_text(encoding="utf-8")

    # 1. Baseline metrics: reuse the existing csynth report only when it is fresh
    # (newer than the sources it describes) and parseable; else synthesize once.
    baseline: QoRMetrics | None = None
    xml = find_csynth_xml(project_dir)
    if xml is not None and _report_is_fresh(project_dir, xml):
        try:
            baseline = parse_csynth_xml(xml)
        except RuntimeError:
            baseline = None  # malformed on-disk report; re-establish below
    if baseline is None:
        if verbose:
            print("No fresh csynth report in the project; running csim+csynth for the baseline.")
        baseline, note = _synth_metrics(project_dir, remote)
        if baseline is None:
            raise RuntimeError(f"cannot establish baseline QoR: {note}")
    collect_local_ppa(project_dir, baseline)
    baseline_score = objective_score(baseline, objective, baseline)
    outcome = OptimizeOutcome(objective=objective, baseline=baseline)
    if baseline_score is None:
        raise RuntimeError("baseline csynth report lacks the metrics needed for the objective")
    if verbose:
        print(f"Baseline {objective} score: {baseline_score}")

    # 2-3. Propose and gate candidates.
    seen = {_sha(baseline_source)}
    history: list[dict[str, object]] = []
    best: tuple[float, int, str] | None = None  # (score, index, source)

    def consider(index: int, kind: str, source: str | None, note: str) -> None:
        nonlocal best
        result = CandidateResult(index=index, kind=kind, status="unparsable", note=note)
        outcome.candidates.append(result)
        if source is None:
            history.append({"index": index, "kind": kind, "status": result.status, "note": note})
            return
        sha = _sha(source)
        result.source_sha = sha
        if sha in seen:
            result.status = "duplicate"
            history.append({"index": index, "kind": kind, "status": "duplicate"})
            return
        seen.add(sha)
        cand_dir = _stage_candidate(project_dir, index, source)
        equiv = run_software_equivalence(cand_dir)
        if equiv.status != "pass":
            result.status = "equiv_fail"
            result.note = (equiv.summary or "host equivalence failed").strip()[:300]
            history.append({"index": index, "kind": kind, "status": "equiv_fail", "note": result.note})
            return
        metrics, fail_note = _synth_metrics(cand_dir, remote)
        strategy = _pragma_summary(baseline_source, source)
        if metrics is None:
            result.status = fail_note.split(":", 1)[0] if ":" in fail_note else "csynth_fail"
            result.note = f"{strategy} — {fail_note}"[:300]
            history.append({"index": index, "kind": kind, "status": result.status, "note": result.note})
            return
        result.metrics = metrics
        score = objective_score(metrics, objective, baseline)
        result.score = score
        result.note = strategy
        if metrics.timing_met is False and baseline.timing_met is not False:
            result.status = "timing_regressed"
            history.append({"index": index, "kind": kind, "status": "timing_regressed", "score": score, "note": strategy})
            return
        result.status = "scored"
        history.append({"index": index, "kind": kind, "status": "scored", "score": score, "note": strategy})
        if score is not None and score < (best[0] if best else baseline_score):
            best = (score, index, source)
        if verbose:
            print(f"candidate {index} [{kind}]: score={score} (baseline {baseline_score})")

    index = 0
    deterministic = _pipeline_innermost_loops(baseline_source)
    if deterministic is not None:
        consider(index, "deterministic-pipeline", deterministic, "")
        index += 1
    if llm is not None:
        for attempt in range(max(0, iterations)):
            source, note = _llm_candidate_source(
                analysis, config, llm, baseline_source, baseline, objective, history, attempt=attempt
            )
            consider(index, "llm", source, note)
            index += 1
    elif verbose:
        print("No LLM client: only the deterministic pipeline candidate was tried.")

    # 4. Promote the best strictly-improving candidate through the FULL ladder.
    if best is None:
        scored_any = any(c.status in ("scored", "timing_regressed") for c in outcome.candidates)
        infra_notes = [
            c.note for c in outcome.candidates
            if "vitis_hls not found" in (c.note or "") or "remote vitis unavailable" in (c.note or "")
        ]
        if not scored_any and infra_notes:
            # Don't report a toolchain outage as an optimization result.
            outcome.summary = (
                f"QoR optimization could not synthesize any candidate ({infra_notes[0][:200]}); "
                "no comparison was possible — this is an infrastructure problem, not a QoR verdict."
            )
        else:
            outcome.summary = (
                f"No candidate improved the {objective} objective (baseline score {baseline_score}); "
                "baseline kept."
            )
        _cleanup_candidates(project_dir, keep_index=None)
        _write_reports(project_dir, outcome)
        return outcome

    best_score, best_index, best_source = best
    backup = src_path.parent / PRE_QOR_BACKUP
    # Never clobber an existing backup: on repeated optimize runs it must keep holding
    # the TRUE pre-QoR original, not the previous run's already-optimized source.
    if not backup.exists():
        backup.write_text(baseline_source, encoding="utf-8")
    src_path.write_text(best_source, encoding="utf-8")
    promote_mtime = src_path.stat().st_mtime
    outcome.winner_index = best_index
    if cosim_winner:
        state = verify_project(project_dir, True, verbose=verbose, remote=remote)
        accepted = final_status(state, True, False) == "pass"
    else:
        state = verify_project(project_dir, False, verbose=verbose)
        accepted = final_status(state, False, False) == "pass"
    if not accepted:
        src_path.write_text(baseline_source, encoding="utf-8")
        outcome.rolled_back = True
        # The acceptance run left the REJECTED candidate's synthesis report in the
        # project; delete it so the next optimize run re-establishes a true baseline.
        stale = find_csynth_xml(project_dir)
        if stale is not None:
            stale.unlink(missing_ok=True)
        winner = outcome.candidates[best_index]
        winner.status = "final_ladder_fail"
        winner.note = "won on csynth score but failed the full acceptance ladder; source restored"
        outcome.summary = (
            f"Candidate {best_index} improved {objective} (score {best_score} vs {baseline_score}) but "
            "FAILED the full acceptance ladder; the original source was restored and the stale "
            "synthesis report removed."
        )
        _cleanup_candidates(project_dir, keep_index=best_index)
        _write_reports(project_dir, outcome)
        return outcome

    outcome.accepted = True
    winner_metrics = outcome.candidates[best_index].metrics
    # Refresh from the acceptance run's report ONLY when that run actually re-synthesized
    # in place (cosim path) and the report postdates the promotion — otherwise the file
    # is the baseline's report and would zero every delta.
    if cosim_winner:
        xml = find_csynth_xml(project_dir)
        if xml is not None and xml.stat().st_mtime >= promote_mtime:
            try:
                winner_metrics = parse_csynth_xml(xml)
                outcome.candidates[best_index].metrics = winner_metrics
            except RuntimeError:
                pass  # keep the candidate-directory metrics

    if ppa_script:
        ppa_started = time.time()
        try:
            proc = subprocess.run(
                ["bash", ppa_script], cwd=project_dir, check=False, timeout=1800,
                capture_output=True, text=True,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            proc = None
            print(f"--ppa-script failed: {exc}", file=sys.stderr)
        if proc is not None and proc.returncode != 0:
            print(
                f"--ppa-script exited {proc.returncode}; skipping local PPA enrichment "
                f"({proc.stderr.strip()[-300:]})",
                file=sys.stderr,
            )
        elif proc is not None:
            # Only trust reports the script just produced — never stale ones from an
            # earlier design revision.
            fresh = all(
                not p.exists() or p.stat().st_mtime >= ppa_started
                for p in (project_dir / "syn" / "yosys_area.rpt", project_dir / "syn" / "sta_report.txt")
            )
            if fresh:
                collect_local_ppa(project_dir, winner_metrics)
            else:
                print(
                    "--ppa-script did not refresh syn/ reports; skipping local PPA enrichment.",
                    file=sys.stderr,
                )

    outcome.delta = qor_delta(baseline, winner_metrics)
    improvement = f"score {best_score} vs baseline {baseline_score}"
    outcome.summary = (
        f"Accepted candidate {best_index} ({outcome.candidates[best_index].kind}) for objective "
        f"'{objective}' ({improvement}); full ladder re-verified. Pre-QoR source kept at src/{PRE_QOR_BACKUP}."
    )
    _cleanup_candidates(project_dir, keep_index=best_index)
    _write_reports(project_dir, outcome)
    return outcome


def _cleanup_candidates(project_dir: Path, keep_index: int | None) -> None:
    """Remove losing candidate scratch copies (each can be tens of MB); keep the
    winner's directory for metrics provenance."""

    qor_dir = project_dir / QOR_DIRNAME
    if not qor_dir.exists():
        return
    for entry in qor_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("cand_") and entry.name != f"cand_{keep_index}":
            shutil.rmtree(entry, ignore_errors=True)


def _write_reports(project_dir: Path, outcome: OptimizeOutcome) -> None:
    (project_dir / "qor_report.json").write_text(
        json.dumps(outcome.to_dict(), indent=2), encoding="utf-8"
    )
    title = f"QoR report — objective: {outcome.objective}"
    if outcome.delta:
        (project_dir / "qor_report.md").write_text(
            render_markdown(outcome.delta, title) + f"\n{outcome.summary}\n", encoding="utf-8"
        )
        (project_dir / "qor_table.tex").write_text(
            render_latex_table(
                outcome.delta,
                caption=f"QoR before/after post-equivalence optimization (objective: {outcome.objective}).",
            ),
            encoding="utf-8",
        )
    else:
        (project_dir / "qor_report.md").write_text(f"# {title}\n\n{outcome.summary}\n", encoding="utf-8")
        # A previous accepted run may have left a qor_table.tex; without this, a stale
        # table would contradict the fresh json/md that say the baseline was kept.
        (project_dir / "qor_table.tex").unlink(missing_ok=True)
