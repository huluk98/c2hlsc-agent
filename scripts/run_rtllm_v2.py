#!/usr/bin/env python3
"""Run the RTLLM v2.0 benchmark end to end: natural-language spec -> RTL -> iverilog verdict.

Two modes:

``--reference``
    Evaluate the benchmark's *own* ``verified_*.v`` against its own testbench. No LLM client is
    constructed and no model is called. This is the oracle baseline: a design the golden RTL
    cannot pass is a benchmark or simulator defect, not a model failure, so a reference run is
    what tells you whether a red row belongs to the agent or to the harness.

default (LLM)
    Run the multi-agent loop (``rtl_planner`` -> ``rtl_generator`` -> verify ->
    ``rtl_repair_agent``) over every selected design and score it with the official RTLLM
    oracle, byte for byte the rule from the benchmark's ``auto_run.py``: the simulator printed
    ``Pass``/``pass``.

Every number is reported twice where it can flatter: ``func_pass`` (official) next to
``func_pass_strict`` (pass banner *and* no failure banner), and raw rates next to rates adjusted
for :data:`c2hlsc_agent.rtllm_bench.KNOWN_ORACLE_ISSUES`. The adjusted rate never silently
replaces the raw one.

Outputs into ``--out-dir``:

    results.jsonl        one ``DesignResult`` per line, appended as each design finishes, so an
                         interrupted sweep keeps its progress and ``--resume`` continues it
    report.json          aggregate metrics (totals, pass@k, failure families, oracle section)
    report.md            the same run as a human table plus an explicit caveats section
    designs/<name>/      rtl.v, compile.log, sim.log, trace.json for the winning sample

Examples
--------
Oracle baseline over the whole benchmark (no model, needs only iverilog)::

    python scripts/run_rtllm_v2.py --benchmark /path/to/RTLLM --out-dir build/rtllm_ref --reference

Agent run through the local Claude Code CLI, 4 designs in parallel, best-of-5 sampling::

    python scripts/run_rtllm_v2.py --benchmark /path/to/RTLLM --out-dir build/rtllm_opus \
        --workers 4 --samples 5 --max-repair-rounds 2

Exit codes: 0 the sweep completed (whatever the score), 2 the LLM backend is unavailable,
130 interrupted with SIGINT (a partial report is still written).
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

# scripts/ is sys.path[0] when run as a file; add the repo root for c2hlsc_agent.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent import rtllm_agent, rtllm_bench  # noqa: E402
from c2hlsc_agent.config import AgentConfig  # noqa: E402
from c2hlsc_agent.llm import build_llm_client, missing_llm_reason  # noqa: E402

RESULTS_FILE = "results.jsonl"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"

#: Failure bucket for a sample that never reached the simulator because the model call failed.
#: Not one of ``rtllm_bench.FAILURE_FAMILIES`` on purpose: it is a harness/backend outage, and
#: folding it into ``compile_error`` would blame the model for a dropped connection.
LLM_ERROR_FAMILY = "llm_error"
#: A design whose whole run raised inside the driver (bad benchmark dir, agent bug, ...).
DRIVER_ERROR_FAMILY = "driver_error"
UNKNOWN_FAMILY = "unknown"


# --------------------------------------------------------------------------- #
# benchmark checkout
# --------------------------------------------------------------------------- #


def clone_benchmark(url: str, dest: Path) -> None:
    """Shallow-clone ``url`` into ``dest``. Exits with a message, never a traceback."""

    git = shutil.which("git")
    if not git:
        raise SystemExit(
            f"git is not on PATH, so --clone cannot fetch {url}.\n"
            f"Clone it manually into {dest} and rerun without --clone."
        )
    dest = Path(dest)
    if dest.parent and not dest.parent.exists():
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SystemExit(f"cannot create {dest.parent}: {exc}")
    print(f"cloning {url} -> {dest} (--depth 1)", flush=True)
    try:
        proc = subprocess.run(
            [git, "clone", "--depth", "1", url, str(dest)],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(f"failed to run git clone: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = "\n".join(detail[-10:]) if detail else "(no output)"
        raise SystemExit(f"git clone of {url} failed (exit {proc.returncode}):\n{tail}")


def resolve_benchmark(args: argparse.Namespace) -> Path:
    """Locate the RTLLM checkout, cloning it when ``--clone`` was given."""

    raw = args.benchmark or os.environ.get("RTLLM_ROOT")
    if not raw:
        raise SystemExit(
            "no benchmark checkout: pass --benchmark PATH or set RTLLM_ROOT "
            f"(add --clone to fetch {rtllm_bench.DEFAULT_BENCHMARK_URL} into that path)."
        )
    path = Path(raw).expanduser()
    if path.is_dir():
        return path
    if not args.clone:
        raise SystemExit(
            f"benchmark checkout not found: {path}\n"
            f"Pass --clone to shallow-clone {rtllm_bench.DEFAULT_BENCHMARK_URL} into it, "
            "or point --benchmark at an existing RTLLM checkout."
        )
    clone_benchmark(args.clone, path)
    if not path.is_dir():  # pragma: no cover - git said 0 but produced nothing
        raise SystemExit(f"git clone reported success but {path} does not exist")
    return path


# --------------------------------------------------------------------------- #
# results file
# --------------------------------------------------------------------------- #


def repair_trailing_newline(path: Path) -> None:
    """A run killed mid-write can leave a torn final line with no newline; appending
    straight after it would merge the next row into the torn one."""

    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb+") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) != b"\n":
            handle.write(b"\n")


def load_prior_rows(path: Path) -> "list[dict[str, Any]]":
    """Rows from a previous sweep, last one wins per design. Tolerates a torn final line."""

    if not path.exists():
        return []
    by_design: "dict[str, dict[str, Any]]" = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn final line from an interrupted run
            if isinstance(row, dict) and isinstance(row.get("design"), str):
                by_design[row["design"]] = row
    return list(by_design.values())


def check_resume_mode(rows: "Sequence[dict[str, Any]]", mode: str, path: Path) -> None:
    """Refuse to mix a reference baseline and an agent run in one results file.

    Matching on the design name alone would report the golden RTL's verdicts as the model's.
    Rows written before this key existed carry no ``mode`` and are accepted.
    """

    prior = {row.get("mode") for row in rows if row.get("mode")}
    other = sorted(value for value in prior if value != mode)
    if other:
        raise SystemExit(
            f"--resume mode mismatch: {path} holds mode={other[0]!r} rows, this run is mode={mode!r}. "
            "Use a fresh --out-dir."
        )


def _atomic_write(path: Path, text: str) -> None:
    # PID-unique tmp name: two runs sharing an out-dir must not race on it.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# per-design execution
# --------------------------------------------------------------------------- #


def _supports_log(func: Callable[..., Any]) -> bool:
    """True when ``func`` accepts the optional ``log`` callback (fakes in tests do not)."""

    try:
        return "log" in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins / C callables
        return False


def run_llm_design(
    design: rtllm_bench.RtllmDesign,
    client: Any,
    config: rtllm_agent.RtllmAgentConfig,
    workdir: Path,
    log: "Callable[[str], None] | None" = None,
) -> "dict[str, Any]":
    """One design through the multi-agent loop, as a JSON-ready row."""

    run = rtllm_agent.run_design
    if log is not None and _supports_log(run):
        result = run(design, client, config, workdir, log=log)
    else:
        result = run(design, client, config, workdir)
    return result.to_dict()


def _reference_rtl(design: rtllm_bench.RtllmDesign) -> str:
    try:
        return rtllm_bench.reference_rtl_text(design)
    except Exception:  # noqa: BLE001 - a malformed reference must not kill the baseline
        return ""


def run_reference_design(
    design: rtllm_bench.RtllmDesign,
    config: rtllm_agent.RtllmAgentConfig,
    workdir: Path,
) -> "dict[str, Any]":
    """One design's golden RTL through its own testbench, shaped like a ``DesignResult``.

    The baseline is a single deterministic sample: re-running the same golden file cannot
    produce a different verdict, so ``--samples`` is deliberately ignored here.
    """

    sim = rtllm_bench.evaluate_reference(
        design,
        Path(workdir) / "reference",
        compile_timeout=config.compile_timeout,
        sim_timeout=config.sim_timeout,
        apply_shims=config.apply_shims,
    )
    rtl = _reference_rtl(design)
    attempt = rtllm_agent.AttemptRecord(round=0, role="reference", sim=sim, rtl=rtl)
    sample = rtllm_agent.SampleResult(
        design=design.name,
        sample=0,
        syntax_pass=sim.syntax_pass,
        func_pass=sim.func_pass,
        func_pass_strict=sim.func_pass_strict,
        rounds=[attempt],
        contract=None,
        final_rtl=rtl,
    )
    result = rtllm_agent.DesignResult(
        design=design.name,
        category=design.category,
        samples=[sample],
        syntax_success=int(sim.syntax_pass),
        func_success=int(sim.func_pass),
    )
    return result.to_dict()


def execute(
    designs: "Sequence[rtllm_bench.RtllmDesign]",
    worker: "Callable[[rtllm_bench.RtllmDesign], dict[str, Any]]",
    record: "Callable[[dict[str, Any]], None]",
    workers: int,
    stop: threading.Event,
) -> None:
    """Run ``worker`` over ``designs``, recording each row exactly once as it finishes.

    ``record`` is serialized here so callers append without their own locking. When ``stop``
    is set (SIGINT) nothing new is scheduled; in-flight designs still finish and are recorded.
    """

    if workers <= 1:
        for design in designs:
            if stop.is_set():
                return
            record(worker(design))
        return

    lock = threading.Lock()

    def run_one(design: rtllm_bench.RtllmDesign) -> "dict[str, Any] | None":
        if stop.is_set():
            return None
        return worker(design)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, design) for design in designs]
        try:
            for future in as_completed(futures):
                row = future.result()
                if row is None:
                    continue
                with lock:
                    record(row)
        except BaseException:
            # Don't drain the queue: cancel everything not yet started. Recorded rows are
            # already on disk for --resume.
            stop.set()
            for future in futures:
                future.cancel()
            raise


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #


def _samples(row: "dict[str, Any]") -> "list[dict[str, Any]]":
    samples = row.get("samples")
    return [s for s in samples if isinstance(s, dict)] if isinstance(samples, list) else []


def _rounds(sample: "dict[str, Any]") -> "list[dict[str, Any]]":
    rounds = sample.get("rounds")
    return [r for r in rounds if isinstance(r, dict)] if isinstance(rounds, list) else []


def best_sample(row: "dict[str, Any]") -> "dict[str, Any] | None":
    """The sample the artifacts should describe: a functional pass, else a compile, else the first."""

    samples = _samples(row)
    if not samples:
        return None
    for key in ("func_pass", "syntax_pass"):
        for sample in samples:
            if sample.get(key):
                return sample
    return samples[0]


def winning_round(sample: "dict[str, Any]") -> "dict[str, Any] | None":
    """The round that earned the sample's verdict, else its last round."""

    rounds = _rounds(sample)
    if not rounds:
        return None
    for entry in rounds:
        sim = entry.get("sim") or {}
        if sim.get("func_pass"):
            return entry
    return rounds[-1]


def write_artifacts(out_dir: Path, row: "dict[str, Any]") -> Path:
    """Write designs/<name>/{rtl.v,compile.log,sim.log,trace.json} for the winning sample."""

    directory = out_dir / "designs" / str(row.get("design", "unknown"))
    directory.mkdir(parents=True, exist_ok=True)
    sample = best_sample(row) or {}
    entry = winning_round(sample) or {}
    sim = entry.get("sim") or {}
    rtl = entry.get("rtl") or sample.get("final_rtl") or ""
    (directory / "rtl.v").write_text(str(rtl), encoding="utf-8")
    (directory / "compile.log").write_text(str(sim.get("compile_log") or ""), encoding="utf-8")
    (directory / "sim.log").write_text(str(sim.get("sim_log") or ""), encoding="utf-8")
    (directory / "trace.json").write_text(json.dumps(row, indent=2, sort_keys=True), encoding="utf-8")
    return directory


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


def pass_at_k(n: int, c: int, k: int) -> "float | None":
    """Unbiased pass@k, ``1 - C(n-c, k)/C(n, k)`` -- the estimator RTLLM's auto_run.py uses.

    Returns ``None`` when the design cannot support the estimator (``n < k`` or no samples):
    a design with 1 sample has no honest pass@5, and clamping k would inflate it.
    """

    if n <= 0 or k <= 0 or k > n:
        return None
    if c <= 0:
        return 0.0
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def _mean(values: "Sequence[float]") -> "float | None":
    return sum(values) / len(values) if values else None


def _rate(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def sample_family(sample: "dict[str, Any]") -> "str | None":
    """The failure bucket for one sample: the final round's family, ``None`` when it passed."""

    if sample.get("func_pass"):
        return None
    rounds = _rounds(sample)
    if not rounds:
        return LLM_ERROR_FAMILY if sample.get("llm_error") else UNKNOWN_FAMILY
    family = (rounds[-1].get("sim") or {}).get("failure_family")
    if family:
        return str(family)
    if rounds[-1].get("llm_error") or sample.get("llm_error"):
        return LLM_ERROR_FAMILY
    return UNKNOWN_FAMILY


def summarize_row(row: "dict[str, Any]") -> "dict[str, Any]":
    """Flatten one ``DesignResult`` row into a report table row."""

    samples = _samples(row)
    n_samples = int(row.get("n_samples") or len(samples))
    syntax_success = int(row.get("syntax_success") or sum(1 for s in samples if s.get("syntax_pass")))
    func_success = int(row.get("func_success") or sum(1 for s in samples if s.get("func_pass")))
    strict_success = sum(1 for s in samples if s.get("func_pass_strict"))
    families = [family for family in (sample_family(s) for s in samples) if family]
    if not samples and row.get("error"):
        families = [DRIVER_ERROR_FAMILY]
    rounds_used = max(
        (len(_rounds(s)) - 1 for s in samples if _rounds(s)),
        default=0,
    )
    duration = 0.0
    shim_applied = False
    for sample in samples:
        for entry in _rounds(sample):
            sim = entry.get("sim") or {}
            try:
                duration += float(sim.get("duration_s") or 0.0)
            except (TypeError, ValueError):
                pass
            shim_applied = shim_applied or bool(sim.get("shim_applied"))
    name = str(row.get("design", "unknown"))
    return {
        "design": name,
        "category": str(row.get("category") or ""),
        "n_samples": n_samples,
        "syntax_success": syntax_success,
        "func_success": func_success,
        "func_success_strict": strict_success,
        "syntax_pass": syntax_success > 0,
        "func_pass": func_success > 0,
        "func_pass_strict": strict_success > 0,
        "repair_rounds_used": rounds_used,
        "failure_family": families[0] if families else None,
        "failure_families": families,
        "shim_applied": shim_applied,
        "known_oracle_issue": rtllm_bench.KNOWN_ORACLE_ISSUES.get(name),
        "llm_error": next((s.get("llm_error") for s in samples if s.get("llm_error")), None),
        "sim_seconds": round(duration, 3),
    }


def _totals(table: "Sequence[dict[str, Any]]", k: int) -> "dict[str, Any]":
    designs = len(table)
    samples = sum(row["n_samples"] for row in table)
    pass1 = [value for value in (pass_at_k(r["n_samples"], r["func_success"], 1) for r in table) if value is not None]
    passk = [value for value in (pass_at_k(r["n_samples"], r["func_success"], k) for r in table) if value is not None]
    syntax1 = [value for value in (pass_at_k(r["n_samples"], r["syntax_success"], 1) for r in table) if value is not None]
    return {
        "designs": designs,
        "designs_syntax_success": sum(1 for row in table if row["syntax_pass"]),
        "designs_func_success": sum(1 for row in table if row["func_pass"]),
        "designs_func_success_strict": sum(1 for row in table if row["func_pass_strict"]),
        "designs_syntax_rate": _rate(sum(1 for row in table if row["syntax_pass"]), designs),
        "designs_func_rate": _rate(sum(1 for row in table if row["func_pass"]), designs),
        "designs_func_rate_strict": _rate(sum(1 for row in table if row["func_pass_strict"]), designs),
        "samples": samples,
        "samples_syntax_success": sum(row["syntax_success"] for row in table),
        "samples_func_success": sum(row["func_success"] for row in table),
        "samples_func_success_strict": sum(row["func_success_strict"] for row in table),
        "samples_syntax_rate": _rate(sum(row["syntax_success"] for row in table), samples),
        "samples_func_rate": _rate(sum(row["func_success"] for row in table), samples),
        "k": k,
        "pass@1": _mean(pass1),
        "pass@k": _mean(passk),
        "syntax@1": _mean(syntax1),
        "pass@1_designs_scored": len(pass1),
        "pass@k_designs_scored": len(passk),
    }


def build_report(
    rows: "Sequence[dict[str, Any]]",
    *,
    mode: str = "llm",
    benchmark: str = "",
    out_dir: str = "",
    k: int = 1,
    backend: "str | None" = None,
    model: "str | None" = None,
    agent_config: "dict[str, Any] | None" = None,
    selected: "Sequence[str]" = (),
    wall_clock_s: float = 0.0,
    interrupted: bool = False,
    resumed: int = 0,
) -> "dict[str, Any]":
    """Aggregate finished ``DesignResult`` rows into the report.json payload.

    Both the raw/official numbers and the oracle-adjusted ones are always present: the
    adjusted rate drops designs listed in ``rtllm_bench.KNOWN_ORACLE_ISSUES``, whose testbench
    cannot be passed by *any* RTL including the benchmark's own, and it is meaningless without
    the raw rate beside it.
    """

    table = sorted((summarize_row(row) for row in rows), key=lambda r: (r["category"], r["design"]))
    sound = [row for row in table if not row["known_oracle_issue"]]

    families: "dict[str, int]" = {}
    for row in table:
        for family in row["failure_families"]:
            families[family] = families.get(family, 0) + 1
    families_by_design: "dict[str, int]" = {}
    for row in table:
        family = row["failure_family"]
        if family and not row["func_pass"]:
            families_by_design[family] = families_by_design.get(family, 0) + 1

    selected_names = list(selected) or [row["design"] for row in table]
    affected = sorted(name for name in selected_names if name in rtllm_bench.KNOWN_ORACLE_ISSUES)
    declared_shims = sorted(
        name for name in getattr(rtllm_bench, "TESTBENCH_SHIMS", {}) if name in selected_names
    )
    return {
        "mode": mode,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": benchmark,
        "out_dir": out_dir,
        "backend": backend,
        "model": model,
        "agent_config": agent_config or {},
        "selected_designs": len(selected_names),
        "completed_designs": len(table),
        "resumed_designs": resumed,
        "interrupted": interrupted,
        "wall_clock_s": round(wall_clock_s, 3),
        "oracle_rule": "official RTLLM auto_run.py rule: simulator stdout contains 'Pass' or 'pass'",
        "totals": _totals(table, k),
        "adjusted": {
            "basis": "designs whose oracle is believed sound (not in rtllm_bench.KNOWN_ORACLE_ISSUES)",
            "excluded_designs": affected,
            **_totals(sound, k),
        },
        "failure_families": families,
        "failure_families_by_design": families_by_design,
        "oracle": {
            "known_issues": dict(rtllm_bench.KNOWN_ORACLE_ISSUES),
            "affected_selected_designs": affected,
            "sound_selected_designs": len(selected_names) - len(affected),
            "shimmed_designs_declared": declared_shims,
            "shimmed_designs_applied": sorted(row["design"] for row in table if row["shim_applied"]),
            "note": (
                "Designs listed in affected_selected_designs cannot be passed by any RTL -- the "
                "benchmark's own verified_*.v fails their testbench under this simulator. The "
                "'adjusted' section drops them; the 'totals' section does not."
            ),
        },
        "designs": table,
    }


# --------------------------------------------------------------------------- #
# report.md
# --------------------------------------------------------------------------- #


def _pct(value: "float | None") -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: "float | None") -> str:
    return "n/a" if value is None else f"{value:.3f}"


def render_markdown(report: "dict[str, Any]") -> str:
    totals = report["totals"]
    adjusted = report["adjusted"]
    oracle = report["oracle"]
    k = totals["k"]
    lines: "list[str]" = []
    lines.append("# RTLLM v2.0 report")
    lines.append("")
    mode = report["mode"]
    engine = (
        "reference (the benchmark's own verified RTL; no model involved)"
        if mode == "reference"
        else f"agent (backend={report.get('backend')}, model={report.get('model')})"
    )
    lines.append(f"- mode: **{mode}** -- {engine}")
    lines.append(f"- benchmark: `{report.get('benchmark')}`")
    lines.append(f"- generated: {report.get('timestamp')}")
    lines.append(
        f"- designs: {report['completed_designs']} completed of {report['selected_designs']} selected"
        + (f" ({report['resumed_designs']} resumed)" if report.get("resumed_designs") else "")
    )
    if report.get("interrupted"):
        lines.append("- **interrupted (SIGINT): this report covers only the designs that finished**")
    lines.append(f"- wall clock: {report['wall_clock_s']:.1f}s")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append("| metric | raw (all selected) | adjusted (sound oracle only) |")
    lines.append("| --- | --- | --- |")
    lines.append(
        f"| syntax pass (designs) | {totals['designs_syntax_success']}/{totals['designs']}"
        f" ({_pct(totals['designs_syntax_rate'])}) | {adjusted['designs_syntax_success']}/{adjusted['designs']}"
        f" ({_pct(adjusted['designs_syntax_rate'])}) |"
    )
    lines.append(
        f"| func pass, official oracle (designs) | {totals['designs_func_success']}/{totals['designs']}"
        f" ({_pct(totals['designs_func_rate'])}) | {adjusted['designs_func_success']}/{adjusted['designs']}"
        f" ({_pct(adjusted['designs_func_rate'])}) |"
    )
    lines.append(
        f"| func pass, strict (designs) | {totals['designs_func_success_strict']}/{totals['designs']}"
        f" ({_pct(totals['designs_func_rate_strict'])}) | {adjusted['designs_func_success_strict']}"
        f"/{adjusted['designs']} ({_pct(adjusted['designs_func_rate_strict'])}) |"
    )
    lines.append(
        f"| func pass (samples) | {totals['samples_func_success']}/{totals['samples']}"
        f" ({_pct(totals['samples_func_rate'])}) | {adjusted['samples_func_success']}/{adjusted['samples']}"
        f" ({_pct(adjusted['samples_func_rate'])}) |"
    )
    lines.append(f"| pass@1 | {_num(totals['pass@1'])} | {_num(adjusted['pass@1'])} |")
    if k != 1:  # with one sample per design pass@k IS pass@1; printing it twice invites misreading
        lines.append(f"| pass@{k} | {_num(totals['pass@k'])} | {_num(adjusted['pass@k'])} |")
    lines.append("")
    lines.append(
        "Official oracle = the benchmark's own rule (stdout contains `Pass`/`pass`). "
        "Strict additionally requires no failure banner and no timeout."
    )
    lines.append("")

    lines.append("## Designs")
    lines.append("")
    lines.append("| design | category | syntax | func | repair rounds | failure family |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in report["designs"]:
        note = " (broken oracle)" if row["known_oracle_issue"] else ""
        syntax = f"{row['syntax_success']}/{row['n_samples']}"
        func = f"{row['func_success']}/{row['n_samples']}"
        family = row["failure_family"] or "-"
        lines.append(
            f"| {row['design']}{note} | {row['category'] or '-'} | {syntax} | {func} | "
            f"{row['repair_rounds_used']} | {family} |"
        )
    lines.append("")

    if report["failure_families"]:
        lines.append("## Failure families (samples)")
        lines.append("")
        lines.append("| family | samples |")
        lines.append("| --- | --- |")
        for family, count in sorted(report["failure_families"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| {family} | {count} |")
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    affected = oracle["affected_selected_designs"]
    if affected:
        lines.append(
            "These selected designs have a **broken oracle**: the benchmark's own `verified_*.v` "
            "fails their testbench under this simulator, so no RTL can score. They are counted in "
            "`totals` and excluded from `adjusted`:"
        )
        lines.append("")
        for name in affected:
            lines.append(f"- `{name}`: {oracle['known_issues'].get(name, 'known upstream issue')}")
    else:
        lines.append("- No selected design is on the known-broken-oracle list.")
    lines.append("")
    applied = oracle["shimmed_designs_applied"]
    declared = oracle["shimmed_designs_declared"]
    if declared or applied:
        lines.append(
            "**Shimmed testbenches.** These designs run against a rewritten *copy* of `testbench.v` "
            "(SystemVerilog that iverilog rejects, translated to equivalent Verilog-2001; no check is "
            "weakened). Disable with `--no-shims` to see the unshimmed verdict:"
        )
        lines.append("")
        for name in sorted(set(declared) | set(applied)):
            mark = "applied" if name in applied else "declared, not applied"
            rationale = ""
            try:
                rationale = rtllm_bench.shim_rationale(name)
            except Exception:  # noqa: BLE001 - the rationale is decoration, not data
                rationale = ""
            lines.append(f"- `{name}` ({mark}){': ' + rationale if rationale else ''}")
        lines.append("")
    lines.append(
        "- Scores are testbench-bounded: they say the design passed the benchmark's stimulus, "
        "not that it is equivalent to the specification over all inputs."
    )
    if report["mode"] != "reference":
        lines.append(
            "- Run the same selection with `--reference` to get the oracle baseline; a design the "
            "reference cannot pass is a harness failure, not a model failure."
        )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_rtllm_v2.py",
        description=(
            "Run the RTLLM v2.0 benchmark: natural-language spec -> RTL (multi-agent loop) -> "
            "iverilog compile + simulate, scored with the official RTLLM oracle."
        ),
        epilog=(
            "Exit codes: 0 the sweep completed (whatever the score), 2 the LLM backend is "
            "unavailable, 130 interrupted (a partial report is still written)."
        ),
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=os.environ.get("RTLLM_ROOT"),
        help="Path to the RTLLM checkout (default: $RTLLM_ROOT)",
    )
    parser.add_argument(
        "--clone",
        nargs="?",
        const=rtllm_bench.DEFAULT_BENCHMARK_URL,
        default=None,
        metavar="URL",
        help=f"Shallow-clone the benchmark into --benchmark when missing (default {rtllm_bench.DEFAULT_BENCHMARK_URL})",
    )
    parser.add_argument("--out-dir", type=Path, required=True, help="Directory for results.jsonl, report.json, report.md and per-design artifacts")
    parser.add_argument("--designs", nargs="+", action="extend", default=[], metavar="NAME", help="Only these design names (repeatable)")
    parser.add_argument("--exclude", nargs="+", action="extend", default=[], metavar="NAME", help="Skip these design names (repeatable)")
    parser.add_argument("--limit", type=int, help="Run at most N designs (after filtering, name-sorted)")
    parser.add_argument("--workers", type=int, default=1, help="Designs evaluated concurrently (default 1)")
    parser.add_argument("--samples", type=int, default=1, help="Independent samples per design; k for pass@k (default 1)")
    parser.add_argument("--max-repair-rounds", type=int, default=2, help="Repair rounds after the first failing verification (default 2)")
    parser.add_argument("--no-plan", action="store_true", help="Skip the rtl_planner contract agent")
    parser.add_argument(
        "--evidence-policy",
        choices=["logs", "none"],
        default="logs",
        help="What the repair agent sees: compile/sim logs (default) or nothing. The golden RTL and testbench source are never shown.",
    )
    parser.add_argument(
        "--reference",
        action="store_true",
        help="Evaluate the benchmark's own verified RTL instead of calling a model: the oracle baseline (no LLM is constructed)",
    )
    parser.add_argument("--resume", action="store_true", help=f"Skip designs already recorded in <out-dir>/{RESULTS_FILE} and append to it")
    parser.add_argument("--sim-timeout", type=int, default=rtllm_bench.DEFAULT_SIM_TIMEOUT, help="Per-simulation timeout in seconds")
    parser.add_argument("--compile-timeout", type=int, default=rtllm_bench.DEFAULT_COMPILE_TIMEOUT, help="Per-compile timeout in seconds")
    parser.add_argument("--no-shims", action="store_true", help="Do not rewrite SystemVerilog-only testbench constructs iverilog rejects")
    parser.add_argument("--llm-backend", default="claude-cli", help="LLM backend: claude-cli (default), anthropic, openai, auto")
    parser.add_argument("--llm-model", default="opus", help="Model id for the backend (default opus)")
    parser.add_argument("--llm-cli-cmd", default="claude", help="Command for the claude-cli backend (default 'claude')")
    parser.add_argument("--verbose", action="store_true", help="Print per-round agent progress")
    return parser


def make_agent_config(args: argparse.Namespace) -> rtllm_agent.RtllmAgentConfig:
    return rtllm_agent.RtllmAgentConfig(
        max_repair_rounds=max(0, args.max_repair_rounds),
        samples=max(1, args.samples),
        plan=not args.no_plan,
        evidence_policy=args.evidence_policy,
        sim_timeout=args.sim_timeout,
        compile_timeout=args.compile_timeout,
        apply_shims=not args.no_shims,
    )


def select_designs(root: Path, args: argparse.Namespace) -> "list[rtllm_bench.RtllmDesign]":
    designs = rtllm_bench.discover_designs(root, include=args.designs, exclude=args.exclude)
    if not designs:
        raise SystemExit(
            f"no RTLLM designs found under {root}"
            + (f" matching --designs {' '.join(args.designs)}" if args.designs else "")
            + ". A design directory needs both design_description.txt and testbench.v."
        )
    if args.limit is not None:
        designs = designs[: max(0, args.limit)]
    return designs


def install_sigint(stop: threading.Event) -> Any:
    """First SIGINT stops scheduling and still writes the report; a second one aborts.

    Returns the previous handler for :func:`restore_sigint`, or ``None`` when the handler
    could not be installed (``main`` called off the main thread, e.g. from a test runner).
    """

    def handler(signum: int, frame: Any) -> None:  # pragma: no cover - signal path
        if stop.is_set():
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            raise KeyboardInterrupt
        stop.set()
        print(
            "\ninterrupt: not scheduling new designs; finishing in-flight work, then writing the "
            "report (Ctrl-C again to abort)",
            flush=True,
        )

    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):  # pragma: no cover - not the main thread
        return None
    return previous


def restore_sigint(previous: Any) -> None:
    """Put the caller's SIGINT handler back; ``main`` must not leave ours installed."""

    if previous is None:
        return
    try:
        signal.signal(signal.SIGINT, previous)
    except (ValueError, OSError, TypeError):  # pragma: no cover - not the main thread
        pass


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    root = resolve_benchmark(args)
    designs = select_designs(root, args)
    mode = "reference" if args.reference else "llm"
    config = make_agent_config(args)

    client = None
    backend = model = None
    if not args.reference:
        # Built once and shared by every worker. The CLI client shells out per call and the
        # API clients are stateless, so sharing is safe as long as nothing mutates them.
        llm_config = AgentConfig(
            use_llm=True,
            llm_backend=args.llm_backend,
            llm_model=args.llm_model,
            llm_cli_cmd=args.llm_cli_cmd,
        )
        client = build_llm_client(llm_config)
        if client is None:
            reason = missing_llm_reason(llm_config) or "no LLM backend available"
            print(f"error: {reason}", file=sys.stderr)
            print(
                "Refusing to run: without a model every design would score 0 and look like a "
                "benchmark result. Use --reference for the no-LLM oracle baseline.",
                file=sys.stderr,
            )
            return 2
        backend = getattr(client, "backend", None) or args.llm_backend
        model = getattr(client, "model", None) or args.llm_model

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / RESULTS_FILE
    work_root = out_dir / "work"

    selected_names = [design.name for design in designs]
    rows: "list[dict[str, Any]]" = []
    pending = designs
    if args.resume:
        prior = load_prior_rows(results_path)
        check_resume_mode(prior, mode, results_path)
        done = {row["design"] for row in prior}
        wanted = set(selected_names)
        # Rows for designs outside this selection stay in the file but out of the report.
        rows = [row for row in prior if row["design"] in wanted]
        pending = [design for design in designs if design.name not in done]
        print(
            f"resume: {len(designs) - len(pending)} designs already done, {len(pending)} to run",
            flush=True,
        )
        repair_trailing_newline(results_path)
    elif results_path.exists() and results_path.stat().st_size:
        # Keep the previous sweep recoverable: truncating here would destroy it if this run
        # crashes before its first design finishes.
        os.replace(results_path, results_path.with_name(results_path.name + ".prev"))

    resumed = len(rows)
    stop = threading.Event()
    previous_sigint = install_sigint(stop)
    started = time.time()

    def log(message: str) -> None:
        print(f"  {message}", flush=True)

    def worker(design: rtllm_bench.RtllmDesign) -> "dict[str, Any]":
        began = time.time()
        workdir = work_root / design.name
        try:
            if args.reference:
                row = run_reference_design(design, config, workdir)
            else:
                row = run_llm_design(design, client, config, workdir, log if args.verbose else None)
        except Exception as exc:  # noqa: BLE001 - one bad design must not kill the sweep
            # n_samples is the sample budget, not 0: a design that blew up is a failure with
            # c=0, and reporting n=0 would drop it out of the pass@k average instead.
            row = {
                "design": design.name,
                "category": design.category,
                "n_samples": 1 if args.reference else max(1, args.samples),
                "syntax_success": 0,
                "func_success": 0,
                "samples": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        row["mode"] = mode
        row["wall_s"] = round(time.time() - began, 3)
        return row

    interrupted = False
    try:
        # Appended and flushed per design so an interrupted sweep keeps everything finished so
        # far and --resume continues from it.
        with results_path.open("a" if args.resume else "w", encoding="utf-8") as handle:

            def record(row: "dict[str, Any]") -> None:
                rows.append(row)
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                handle.flush()
                write_artifacts(out_dir, row)
                summary = summarize_row(row)
                print(
                    f"[{summary['design']}] syntax={summary['syntax_success']}/{summary['n_samples']} "
                    f"func={summary['func_success']}/{summary['n_samples']} "
                    f"rounds={summary['repair_rounds_used']} "
                    f"family={summary['failure_family'] or '-'} "
                    f"{row.get('wall_s', 0.0):.1f}s",
                    flush=True,
                )

            try:
                execute(pending, worker, record, max(1, args.workers), stop)
            except KeyboardInterrupt:
                interrupted = True
                stop.set()
    finally:
        restore_sigint(previous_sigint)

    interrupted = interrupted or stop.is_set()
    report = build_report(
        rows,
        mode=mode,
        benchmark=str(root),
        out_dir=str(out_dir),
        # A reference run has exactly one deterministic sample per design; a pass@5 over n=1
        # would be undefined for every row, so k is pinned to 1 there.
        k=1 if args.reference else max(1, args.samples),
        backend=backend,
        model=model,
        agent_config=config.to_dict() if hasattr(config, "to_dict") else {},
        selected=selected_names,
        wall_clock_s=time.time() - started,
        interrupted=interrupted,
        resumed=resumed,
    )
    _atomic_write(out_dir / REPORT_JSON, json.dumps(report, indent=2, sort_keys=True))
    _atomic_write(out_dir / REPORT_MD, render_markdown(report))

    totals = report["totals"]
    adjusted = report["adjusted"]
    at_k = "" if totals["k"] == 1 else f", pass@{totals['k']}={_num(totals['pass@k'])}"
    print(
        f"\n{mode}: {totals['designs_func_success']}/{totals['designs']} designs func-pass "
        f"({_pct(totals['designs_func_rate'])} official), "
        f"{adjusted['designs_func_success']}/{adjusted['designs']} adjusted "
        f"({_pct(adjusted['designs_func_rate'])}), "
        f"syntax {totals['designs_syntax_success']}/{totals['designs']}, "
        f"pass@1={_num(totals['pass@1'])}{at_k}",
        flush=True,
    )
    print(f"report: {out_dir / REPORT_MD}", flush=True)
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
