#!/usr/bin/env python3
"""Run the RTLLM v2.0 benchmark end to end: natural-language spec -> RTL -> iverilog verdict.

Four modes:

``--external-rtl DIR``
    Score *pre-generated* RTL from some other model through this exact pipeline -- same
    testbenches, same shims, same dual oracle, same failure taxonomy -- so a comparison
    against the agent is like-for-like rather than a quote from someone else's paper. No LLM
    is constructed. ``DIR`` either holds one file per design, or per-trial subdirectories
    (``t1``, ``t2``, ...), in which case each trial is one SAMPLE of the same model and
    ``pass@k`` works exactly as it does under ``--samples N``.

``--reference``
    Evaluate the benchmark's *own* ``verified_*.v`` against its own testbench. No LLM client is
    constructed and no model is called. This is the oracle baseline: a design the golden RTL
    cannot pass is a benchmark or simulator defect, not a model failure, so a reference run is
    what tells you whether a red row belongs to the agent or to the harness.

``--empty-baseline``
    Evaluate a port-only module with NO LOGIC. This is the oracle *floor*, and it is the other
    half of the same question: a design that passes here is passed by any agent whatsoever, so
    its score carries no information. Also no LLM.

default (LLM)
    Run the multi-agent loop (``rtl_planner`` -> ``rtl_generator`` -> verify ->
    ``rtl_repair_agent``) over every selected design and score it with the official RTLLM
    oracle, byte for byte the rule from the benchmark's ``auto_run.py``: the simulator printed
    ``Pass``/``pass``.

Every number is reported twice where it can flatter:

- ``func_pass`` (official) next to ``func_pass_strict`` (pass banner *and* no failure banner);
- ``pass@1_with_repair`` (any round of the sample passed -- the agent's number) next to
  ``pass@1_round0`` (the single generation, the number comparable to published RTLLM pass@1);
- raw rates next to ``adjusted`` rates, which drop the designs no RTL can pass
  (:data:`c2hlsc_agent.rtllm_bench.KNOWN_ORACLE_ISSUES`) *and* the designs an empty module
  passes (:data:`c2hlsc_agent.rtllm_bench.VACUOUS_ORACLE_DESIGNS`), so the correction runs in
  both directions rather than only the flattering one.

No adjusted rate ever silently replaces the raw one, and ``report.md`` prints the run's full
configuration so no table from it is quotable without its settings.

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

Exit codes: 0 the sweep completed (whatever the score), 2 the LLM backend is unavailable at
startup, 3 the sweep completed but at least one design scored 0 because the backend errored
mid-run (not a model result), 130 interrupted with SIGINT (a partial report is still written).
"""

from __future__ import annotations

import argparse
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
#: ``--external-rtl``: the trial shipped no file for this design. Scored as a failed sample
#: rather than dropped, and counted separately in the report so the denominator is auditable.
MISSING_CANDIDATE_FAMILY = "missing_candidate"
#: Stamped into the synthetic ``compile_log`` of a missing candidate so a stored row explains
#: itself without the driver.
MISSING_CANDIDATE_MARKER = "run_rtllm_v2: no candidate RTL file for this design in this trial"


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


#: Row fields that must agree across a resumed sweep. Every one of them changes what the
#: numbers MEAN, and ``build_report`` stamps the report with the *final* invocation's config
#: for all rows -- so mixing them produces a report that misdescribes its own contents.
RESUME_CRITICAL_KEYS = (
    "benchmark",
    "samples",
    "max_repair_rounds",
    "evidence_policy",
    "apply_shims",
    "plan",
    "sim_timeout",
    "compile_timeout",
    # mode alone does not separate two --external-rtl sweeps: both are mode="external", and
    # merging gpt-3.5's rows with gpt-4's would report one model's designs as the other's.
    "external_rtl",
    "external_label",
)


def run_config_fingerprint(
    config: rtllm_agent.RtllmAgentConfig,
    benchmark: Path,
    *,
    backend: "str | None" = None,
    model: "str | None" = None,
    extra: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    """The scoring-relevant configuration stamped into every results row."""

    fingerprint: "dict[str, Any]" = {"benchmark": str(benchmark)}
    fingerprint.update(config.to_dict())
    fingerprint["backend"] = backend
    fingerprint["model"] = model
    fingerprint.update(extra or {})
    return fingerprint


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


def check_resume_compatible(
    rows: "Sequence[dict[str, Any]]",
    mode: str,
    run_config: "dict[str, Any]",
    path: Path,
) -> None:
    """Refuse a resume whose scoring knobs disagree with the rows already on disk.

    ``mode`` alone is not enough. A sweep started at ``--samples 1 --evidence-policy none``,
    interrupted, then resumed at ``--samples 5 --evidence-policy logs`` merges into one
    report that credits some designs a best-of-5 and others a best-of-1, drops the short
    rows out of pass@5 without saying so in report.md, and stamps ``agent_config`` with the
    *last* invocation's settings for all of them. Same for a resume against a different
    ``--benchmark`` checkout. Rows written before this key existed carry no ``run_config``
    and are accepted, since there is nothing to compare.
    """

    check_resume_mode(rows, mode, path)
    for row in rows:
        prior = row.get("run_config")
        if not isinstance(prior, dict):
            continue
        differing = [
            (key, prior.get(key), run_config.get(key))
            for key in RESUME_CRITICAL_KEYS
            if key in prior and prior.get(key) != run_config.get(key)
        ]
        if differing:
            detail = "; ".join(f"{key}: stored {was!r} vs now {now!r}" for key, was, now in differing)
            raise SystemExit(
                f"--resume config mismatch on design {row.get('design')!r} in {path}: {detail}.\n"
                "Averaging rows scored under different settings would misdescribe every "
                "headline number. Use a fresh --out-dir, or rerun with the stored settings."
            )


def _atomic_write(path: Path, text: str) -> None:
    # PID-unique tmp name: two runs sharing an out-dir must not race on it.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# per-design execution
# --------------------------------------------------------------------------- #


def _shutdown(pool: ThreadPoolExecutor, *, wait: bool) -> None:
    """``pool.shutdown`` with ``cancel_futures`` where the interpreter has it (3.9+)."""

    try:
        pool.shutdown(wait=wait, cancel_futures=True)
    except TypeError:  # pragma: no cover - Python < 3.9
        pool.shutdown(wait=wait)


def run_llm_design(
    design: rtllm_bench.RtllmDesign,
    client: Any,
    config: rtllm_agent.RtllmAgentConfig,
    workdir: Path,
    log: "Callable[[str], None] | None" = None,
    stop: "threading.Event | None" = None,
) -> "dict[str, Any]":
    """One design through the multi-agent loop, as a JSON-ready row.

    ``log`` and ``stop`` are passed through unconditionally. They used to be probed for
    with ``inspect.signature`` so that test fakes could omit them, which meant a real
    signature drift silently turned ``--verbose`` into a no-op instead of raising.
    """

    result = rtllm_agent.run_design(design, client, config, workdir, log=log, stop=stop)
    return result.to_dict()


def _reference_rtl(design: rtllm_bench.RtllmDesign) -> str:
    try:
        return rtllm_bench.reference_rtl_text(design)
    except Exception:  # noqa: BLE001 - a malformed reference must not kill the baseline
        return ""


def _empty_stub_rtl(design: rtllm_bench.RtllmDesign) -> str:
    try:
        return rtllm_bench.empty_stub_rtl(design)
    except Exception:  # noqa: BLE001 - a malformed reference must not kill the baseline
        return ""


def run_reference_design(
    design: rtllm_bench.RtllmDesign,
    config: rtllm_agent.RtllmAgentConfig,
    workdir: Path,
    *,
    empty_baseline: bool = False,
) -> "dict[str, Any]":
    """One design's golden RTL through its own testbench, shaped like a ``DesignResult``.

    The baseline is a single deterministic sample: re-running the same golden file cannot
    produce a different verdict, so ``--samples`` is deliberately ignored here.

    With ``empty_baseline`` the *port-only stub* is run instead of the golden RTL. That is
    the mirror measurement: the reference gives the oracle's ceiling, the empty stub gives
    its floor, and a design that passes the floor is one whose score means nothing.
    """

    evaluate = rtllm_bench.evaluate_empty_stub if empty_baseline else rtllm_bench.evaluate_reference
    sim = evaluate(
        design,
        Path(workdir) / ("empty" if empty_baseline else "reference"),
        compile_timeout=config.compile_timeout,
        sim_timeout=config.sim_timeout,
        apply_shims=config.apply_shims,
    )
    rtl = _empty_stub_rtl(design) if empty_baseline else _reference_rtl(design)
    role = "empty_stub" if empty_baseline else "reference"
    attempt = rtllm_agent.AttemptRecord(round=0, role=role, sim=sim, rtl=rtl)
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


# --------------------------------------------------------------------------- #
# external RTL (--external-rtl): score another model's pre-generated files
# --------------------------------------------------------------------------- #


def discover_external_trials(root: Path) -> "list[Path]":
    """The trial directories under ``root``, sorted; ``[root]`` when it holds the files itself.

    A "trial" is any immediate subdirectory containing at least one ``.v`` file. RTLLM ships
    its archived model output as ``_chatgpt4/t1 ... t5``, five independent generations of the
    same design set, which is exactly the shape ``--samples 5`` produces for the agent -- so
    each trial becomes one sample and pass@k is the same estimator over the same n.

    Sorted by name so sample indices are stable across runs (and so ``t10`` cannot swap
    places with ``t2`` between two invocations and silently re-label the artifacts).
    """

    root = Path(root)
    if not root.is_dir():
        raise SystemExit(f"--external-rtl directory not found: {root}")
    trials = sorted(
        (entry for entry in root.iterdir() if entry.is_dir() and any(entry.glob("*.v"))),
        key=lambda path: path.name,
    )
    if trials:
        return trials
    if any(root.glob("*.v")):
        return [root]
    raise SystemExit(
        f"--external-rtl {root} holds no .v files, and none of its subdirectories do either. "
        "Point it at a directory of <design>.v files, or at one holding per-trial "
        "subdirectories (t1, t2, ...) of them."
    )


def resolve_external_candidate(
    trial: Path, design_name: str
) -> "tuple[Path | None, str]":
    """Find ``design_name``'s candidate file in ``trial``: ``(path, how_it_was_resolved)``.

    Exact ``<design>.v`` first -- that is the documented layout and the only resolution that
    needs no explanation. Two fallbacks follow, and **both are reported** in report.json's
    ``resolved_by_fallback`` rather than applied silently, because each one is a judgement
    call about whose file this is:

    ``case``
        the same stem in a different case, on a case-sensitive filesystem.
    ``module``
        no filename match, but exactly one ``.v`` file in the trial *declares* ``module
        <design_name>``. RTLLM's own archive needs this: every ``_chatgpt35`` trial ships
        the ``calendar`` design as ``calender.v`` (upstream typo) with ``module calendar``
        inside. Scoring that as a miss would charge the model five failures for a filename.

    Ambiguity is never resolved: two files declaring the module leave the design missing.
    """

    exact = trial / f"{design_name}.v"
    if exact.is_file():
        return exact, "exact"

    candidates = sorted(path for path in trial.glob("*.v") if path.is_file())
    lowered = design_name.lower()
    by_case = [path for path in candidates if path.stem.lower() == lowered]
    if len(by_case) == 1:
        return by_case[0], "case"

    by_module = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(m.group("name") == design_name for m in rtllm_bench._ANY_MODULE_RE.finditer(text)):
            by_module.append(path)
    if len(by_module) == 1:
        return by_module[0], "module"
    return None, "missing"


def external_basis(
    trials: "Sequence[Path]", designs: "Sequence[rtllm_bench.RtllmDesign]"
) -> "list[rtllm_bench.RtllmDesign]":
    """The designs this external set actually attempted: present in at least one trial.

    A model that shipped 29 of the benchmark's 50 designs did not fail the other 21, it was
    never asked about them, and scoring 0/50 would be a statement about the archive rather
    than about the model. The basis size is printed in report.json and report.md so the
    denominator can never be read as "the whole benchmark" by accident.
    """

    return [
        design
        for design in designs
        if any(resolve_external_candidate(trial, design.name)[0] for trial in trials)
    ]


def _missing_candidate_sim(design_name: str, trial: Path) -> rtllm_bench.SimResult:
    return rtllm_bench.SimResult(
        design=design_name,
        syntax_pass=False,
        func_pass=False,
        func_pass_strict=False,
        timed_out=False,
        compile_log=(
            f"{MISSING_CANDIDATE_MARKER}\n"
            f"Looked for {trial / (design_name + '.v')} (and for a .v file declaring "
            f"'module {design_name}'); found neither. Recorded as a failed sample of this "
            "model, not dropped: dropping it would shrink the denominator and raise the rate."
        ),
        sim_log="",
        duration_s=0.0,
        failure_family=MISSING_CANDIDATE_FAMILY,
    )


def run_external_design(
    design: rtllm_bench.RtllmDesign,
    trials: "Sequence[Path]",
    config: rtllm_agent.RtllmAgentConfig,
    workdir: Path,
    *,
    gate_impact: bool = True,
) -> "dict[str, Any]":
    """Score one design's pre-generated RTL, one sample per trial.

    Each sample has exactly one round: these files were produced without a verifier in the
    loop, so there is nothing to repair and ``pass@1 round 0`` equals ``pass@1 with repair``
    by construction. That is the honest comparison point against the agent's round-0 number.

    When the illegal-system-task gate refuses a candidate and ``gate_impact`` is set, the
    same file is re-run with the gate disabled *purely to record what the gate cost*. That
    second verdict never touches ``syntax_pass``/``func_pass`` -- it is stored under
    ``gate_impact`` and reported as a delta, because a candidate that can print is a
    candidate that can print its own ``Pass``.
    """

    samples: "list[rtllm_agent.SampleResult]" = []
    missing: "list[str]" = []
    fallbacks: "list[dict[str, Any]]" = []
    impacts: "list[dict[str, Any]]" = []

    for index, trial in enumerate(trials):
        path, resolved_by = resolve_external_candidate(trial, design.name)
        if path is None:
            missing.append(trial.name)
            sim = _missing_candidate_sim(design.name, trial)
            rtl = ""
        else:
            if resolved_by != "exact":
                fallbacks.append(
                    {
                        "design": design.name,
                        "trial": trial.name,
                        "path": str(path),
                        "resolved_by": resolved_by,
                    }
                )
            rtl = path.read_text(encoding="utf-8", errors="replace")
            sim = rtllm_bench.evaluate_rtl(
                design,
                rtl,
                Path(workdir) / f"sample{index}",
                compile_timeout=config.compile_timeout,
                sim_timeout=config.sim_timeout,
                apply_shims=config.apply_shims,
            )
            if gate_impact and sim.failure_family == "illegal_system_task":
                ungated = rtllm_bench.evaluate_rtl(
                    design,
                    rtl,
                    Path(workdir) / f"sample{index}_ungated",
                    compile_timeout=config.compile_timeout,
                    sim_timeout=config.sim_timeout,
                    apply_shims=config.apply_shims,
                    enforce_illegal_task_gate=False,
                )
                impacts.append(
                    {
                        "design": design.name,
                        "trial": trial.name,
                        "sample": index,
                        "path": str(path),
                        "gated_verdict": "rejected_illegal_system_task",
                        "ungated_syntax_pass": bool(ungated.syntax_pass),
                        "ungated_func_pass": bool(ungated.func_pass),
                        "ungated_func_pass_strict": bool(ungated.func_pass_strict),
                        "ungated_failure_family": ungated.failure_family,
                    }
                )

        attempt = rtllm_agent.AttemptRecord(round=0, role="external_rtl", sim=sim, rtl=rtl)
        samples.append(
            rtllm_agent.SampleResult(
                design=design.name,
                sample=index,
                syntax_pass=sim.syntax_pass,
                func_pass=sim.func_pass,
                func_pass_strict=sim.func_pass_strict,
                rounds=[attempt],
                contract=None,
                final_rtl=rtl,
                evidence_policy="none",
            )
        )

    result = rtllm_agent.DesignResult(
        design=design.name,
        category=design.category,
        samples=samples,
        syntax_success=sum(1 for s in samples if s.syntax_pass),
        func_success=sum(1 for s in samples if s.func_pass),
    )
    row = result.to_dict()
    row["missing_candidate_trials"] = missing
    row["resolved_by_fallback"] = fallbacks
    row["gate_impact"] = impacts
    return row


def execute(
    designs: "Sequence[rtllm_bench.RtllmDesign]",
    worker: "Callable[[rtllm_bench.RtllmDesign], dict[str, Any]]",
    record: "Callable[[dict[str, Any]], None]",
    workers: int,
    stop: threading.Event,
) -> None:
    """Run ``worker`` over ``designs``, recording each row exactly once as it finishes.

    ``record`` is serialized here so callers append without their own locking. When ``stop``
    is set (SIGINT) nothing new is scheduled; in-flight designs wind down via the same event
    (``rtllm_agent.run_design`` checks it between samples and repair rounds) and whatever
    they return is still recorded.

    The pool is shut down explicitly rather than with a ``with`` block: ``__exit__`` calls
    ``shutdown(wait=True)``, so an abort raised out of this function would first block for
    the full remaining runtime of every in-flight design -- measured at the whole 20 s of a
    sleeping worker, and hours in a real sweep -- after the SIGINT handler has already
    restored ``SIG_DFL``. The user's second Ctrl-C then kills the process before any report
    is written, which is exactly what the first Ctrl-C promised not to do.
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

    pool = ThreadPoolExecutor(max_workers=workers)
    futures = [pool.submit(run_one, design) for design in designs]
    aborting = False
    try:
        for future in as_completed(futures):
            row = future.result()
            if row is None:
                continue
            with lock:
                record(row)
    except BaseException:
        # Don't drain the queue: cancel everything not yet started, and do not join what is
        # already running. Recorded rows are already on disk for --resume.
        aborting = True
        stop.set()
        raise
    finally:
        _shutdown(pool, wait=not aborting)


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


def _passed_first_round(sample: "dict[str, Any]") -> bool:
    """True when the sample's round 0 -- the single generation, before any repair -- passed.

    This is the RTLLM-comparable event. ``sample['func_pass']`` is true if ANY round passed,
    which after two repair rounds is a three-generation best-of, not a single shot.
    """

    round_index = sample.get("func_pass_round")
    if round_index is not None:
        return round_index == 0
    rounds = _rounds(sample)  # rows written before func_pass_round existed
    return bool(rounds) and bool((rounds[0].get("sim") or {}).get("func_pass"))


def summarize_row(row: "dict[str, Any]") -> "dict[str, Any]":
    """Flatten one ``DesignResult`` row into a report table row."""

    samples = _samples(row)
    n_samples = int(row.get("n_samples") or len(samples))
    syntax_success = int(row.get("syntax_success") or sum(1 for s in samples if s.get("syntax_pass")))
    func_success = int(row.get("func_success") or sum(1 for s in samples if s.get("func_pass")))
    strict_success = sum(1 for s in samples if s.get("func_pass_strict"))
    round0_success = sum(1 for s in samples if _passed_first_round(s))
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
    llm_error = next((s.get("llm_error") for s in samples if s.get("llm_error")), None)
    return {
        "design": name,
        "category": str(row.get("category") or ""),
        "n_samples": n_samples,
        "syntax_success": syntax_success,
        "func_success": func_success,
        "func_success_strict": strict_success,
        "func_success_round0": round0_success,
        "syntax_pass": syntax_success > 0,
        "func_pass": func_success > 0,
        "func_pass_strict": strict_success > 0,
        "func_pass_round0": round0_success > 0,
        "repair_rounds_used": rounds_used,
        "failure_family": families[0] if families else None,
        "failure_families": families,
        "shim_applied": shim_applied,
        "known_oracle_issue": rtllm_bench.KNOWN_ORACLE_ISSUES.get(name),
        "vacuous_oracle": rtllm_bench.VACUOUS_ORACLE_DESIGNS.get(name),
        "llm_error": llm_error,
        # A design nothing was ever generated for: the backend died, so a 0 here is a
        # measurement of the backend, not of the model.
        "backend_failed": bool(llm_error) and func_success == 0,
        "sim_seconds": round(duration, 3),
    }


def _totals(table: "Sequence[dict[str, Any]]", k: int) -> "dict[str, Any]":
    """Aggregate a table of ``summarize_row`` dicts.

    Two different pass@1 numbers are reported, because ``func_success`` counts a sample as a
    success if ANY of its rounds passed. At the default ``--max-repair-rounds 2`` that is up
    to three generations with verifier feedback between them:

    ``pass@1_with_repair``
        the agent's number -- generate, verify, repair, verify, repair, verify.
    ``pass@1_round0``
        the single-shot number, the one comparable to published RTLLM pass@1.

    ``pass@1`` remains as an alias of ``pass@1_with_repair`` so existing consumers keep
    working, but report.md prints both and neither is quotable without the other.
    """

    designs = len(table)
    samples = sum(row["n_samples"] for row in table)
    pass1 = [value for value in (pass_at_k(r["n_samples"], r["func_success"], 1) for r in table) if value is not None]
    passk = [value for value in (pass_at_k(r["n_samples"], r["func_success"], k) for r in table) if value is not None]
    round0 = [
        value
        for value in (pass_at_k(r["n_samples"], r.get("func_success_round0", 0), 1) for r in table)
        if value is not None
    ]
    syntax1 = [value for value in (pass_at_k(r["n_samples"], r["syntax_success"], 1) for r in table) if value is not None]
    with_repair = _mean(pass1)
    return {
        "designs": designs,
        "designs_syntax_success": sum(1 for row in table if row["syntax_pass"]),
        "designs_func_success": sum(1 for row in table if row["func_pass"]),
        "designs_func_success_strict": sum(1 for row in table if row["func_pass_strict"]),
        "designs_func_success_round0": sum(1 for row in table if row.get("func_pass_round0")),
        "designs_syntax_rate": _rate(sum(1 for row in table if row["syntax_pass"]), designs),
        "designs_func_rate": _rate(sum(1 for row in table if row["func_pass"]), designs),
        "designs_func_rate_strict": _rate(sum(1 for row in table if row["func_pass_strict"]), designs),
        "designs_func_rate_round0": _rate(sum(1 for row in table if row.get("func_pass_round0")), designs),
        "samples": samples,
        "samples_syntax_success": sum(row["syntax_success"] for row in table),
        "samples_func_success": sum(row["func_success"] for row in table),
        "samples_func_success_strict": sum(row["func_success_strict"] for row in table),
        "samples_func_success_round0": sum(row.get("func_success_round0", 0) for row in table),
        "samples_syntax_rate": _rate(sum(row["syntax_success"] for row in table), samples),
        "samples_func_rate": _rate(sum(row["func_success"] for row in table), samples),
        "k": k,
        "pass@1_with_repair": with_repair,
        "pass@1_round0": _mean(round0),
        "pass@1": with_repair,  # alias: same number, kept so existing consumers do not break
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
    external: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    """Aggregate finished ``DesignResult`` rows into the report.json payload.

    Both the raw/official numbers and the oracle-adjusted ones are always present, and the
    adjustment corrects in **both** directions:

    - ``rtllm_bench.KNOWN_ORACLE_ISSUES`` -- oracles no RTL can pass, not even the
      benchmark's own ``verified_*.v``. Dropping them raises the rate.
    - ``rtllm_bench.VACUOUS_ORACLE_DESIGNS`` -- oracles an EMPTY module passes. Keeping them
      also raises the rate, by handing every agent four free designs. An "adjusted" number
      that drops the first set and keeps the second is biased upward from both ends at once,
      which is what this harness used to print.
    - designs whose samples all died on an LLM backend error, which measure the backend
      rather than the model.

    ``adjusted_unpassable_only`` keeps the old one-directional basis for continuity. Neither
    adjusted view ever replaces ``totals``.
    """

    table = sorted((summarize_row(row) for row in rows), key=lambda r: (r["category"], r["design"]))
    unpassable_only = [row for row in table if not row["known_oracle_issue"]]
    sound = [
        row
        for row in unpassable_only
        if not row["vacuous_oracle"] and not row["backend_failed"]
    ]

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
    vacuous = sorted(name for name in selected_names if name in rtllm_bench.VACUOUS_ORACLE_DESIGNS)
    backend_failed = sorted(row["design"] for row in table if row["backend_failed"])
    declared_shims = sorted(
        name for name in getattr(rtllm_bench, "TESTBENCH_SHIMS", {}) if name in selected_names
    )
    excluded_from_sound = sorted(
        {row["design"] for row in table} - {row["design"] for row in sound}
    )
    payload_external = external_section(rows, external) if external is not None else None
    report = {
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
            "basis": (
                "designs whose oracle is sound in BOTH directions: not in "
                "rtllm_bench.KNOWN_ORACLE_ISSUES (unpassable), not in "
                "rtllm_bench.VACUOUS_ORACLE_DESIGNS (passed by an empty module), and not "
                "failed by an LLM backend outage"
            ),
            "excluded_designs": excluded_from_sound,
            "excluded_unpassable": affected,
            "excluded_vacuous": vacuous,
            "excluded_backend_failed": backend_failed,
            **_totals(sound, k),
        },
        "adjusted_unpassable_only": {
            "basis": (
                "designs not in rtllm_bench.KNOWN_ORACLE_ISSUES. One-directional: it drops "
                "the oracles that are too strict and keeps the ones that are vacuous, so it "
                "reads higher than 'adjusted'. Kept for continuity with earlier reports."
            ),
            "excluded_designs": affected,
            **_totals(unpassable_only, k),
        },
        "failure_families": families,
        "failure_families_by_design": families_by_design,
        "llm_error_designs": backend_failed,
        "oracle": {
            "known_issues": dict(rtllm_bench.KNOWN_ORACLE_ISSUES),
            "affected_selected_designs": affected,
            "sound_selected_designs": len(selected_names) - len(set(affected) | set(vacuous)),
            "vacuous_issues": dict(rtllm_bench.VACUOUS_ORACLE_DESIGNS),
            "vacuous_selected_designs": vacuous,
            "shimmed_designs_declared": declared_shims,
            "shimmed_designs_applied": sorted(row["design"] for row in table if row["shim_applied"]),
            "note": (
                "Designs in affected_selected_designs cannot be passed by any RTL -- the "
                "benchmark's own verified_*.v fails their testbench under this simulator. "
                "Designs in vacuous_selected_designs are passed by a module with no logic at "
                "all (X-optimistic checks), so every agent banks them for free. 'adjusted' "
                "drops both; 'adjusted_unpassable_only' drops only the first; 'totals' drops "
                "neither. Run --empty-baseline to re-measure the vacuous set on your machine."
            ),
        },
        "designs": table,
    }
    if payload_external is not None:
        report["external"] = payload_external
    return report


def external_section(
    rows: "Sequence[dict[str, Any]]", meta: "dict[str, Any]"
) -> "dict[str, Any]":
    """The ``external`` block of report.json: whose RTL this was and what was skipped.

    Aggregated from the rows rather than from the sweep's in-memory state, so a ``--resume``
    that finishes a half-done comparison reports the same numbers as an uninterrupted one.
    """

    missing: "dict[str, list[str]]" = {}
    fallbacks: "list[dict[str, Any]]" = []
    impacts: "list[dict[str, Any]]" = []
    for row in rows:
        trials = row.get("missing_candidate_trials")
        if isinstance(trials, list) and trials:
            missing[str(row.get("design"))] = [str(name) for name in trials]
        for entry in row.get("resolved_by_fallback") or []:
            if isinstance(entry, dict):
                fallbacks.append(entry)
        for entry in row.get("gate_impact") or []:
            if isinstance(entry, dict):
                impacts.append(entry)

    rescued = [entry for entry in impacts if entry.get("ungated_func_pass")]
    return {
        "label": meta.get("label"),
        "rtl_dir": meta.get("rtl_dir"),
        "trials": list(meta.get("trials") or []),
        "trial_count": len(meta.get("trials") or []),
        "basis_designs": meta.get("basis_designs"),
        "benchmark_designs": meta.get("benchmark_designs"),
        "basis_note": (
            "Only designs with a candidate file in at least one trial are in this model's "
            "basis. Designs the archive never attempted are out of the denominator entirely; "
            "a design attempted in SOME trials counts as a failed sample in the trials that "
            "are missing it (see missing_candidates)."
        ),
        "missing_candidates": dict(sorted(missing.items())),
        "missing_candidate_samples": sum(len(v) for v in missing.values()),
        "missing_candidate_designs": len(missing),
        "resolved_by_fallback": sorted(
            fallbacks, key=lambda e: (str(e.get("design")), str(e.get("trial")))
        ),
        "gate_impact": {
            "enabled": bool(meta.get("gate_impact_enabled")),
            "rejected_samples": len(impacts),
            "rejected_designs": len({str(entry.get("design")) for entry in impacts}),
            "would_pass_without_gate": len(rescued),
            "would_pass_without_gate_designs": sorted(
                {str(entry.get("design")) for entry in rescued}
            ),
            "samples": sorted(
                impacts, key=lambda e: (str(e.get("design")), str(e.get("trial")))
            ),
            "note": (
                "The illegal-system-task gate (rtllm_bench.find_illegal_system_tasks) is "
                "applied to external RTL exactly as it is to the agent's. It is part of the "
                "oracle, not a house rule: the benchmark scores by grepping the simulator's "
                "stdout for 'Pass', and the design under test shares that stream with the "
                "testbench, so a candidate containing $display can print its own verdict. "
                "These files were nonetheless produced without being told about the gate, so "
                "'would_pass_without_gate' states what it cost -- the largest number of "
                "designs this comparison could possibly be understating. That number is "
                "NOT a score: an ungated pass cannot be distinguished from a self-reported one."
            ),
        },
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
    external = report.get("external") or {}
    if mode == "reference":
        engine = "reference (the benchmark's own verified RTL; no model involved)"
    elif mode == "empty":
        engine = "empty stub (a port-only module with no logic; no model involved)"
    elif mode == "external":
        engine = (
            f"external RTL from `{external.get('label')}`, pre-generated and scored through "
            "this harness (no model was called by this run)"
        )
    else:
        engine = f"agent (backend={report.get('backend')}, model={report.get('model')})"
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

    # No report.md is quotable without the configuration that produced it: `samples` and
    # `max_repair_rounds` alone are the difference between a single-shot number and a
    # best-of-15 one.
    config = dict(report.get("agent_config") or {})
    if mode == "external":
        # The scoring knobs that are meaningless for a file that was never regenerated: no
        # planner ran, no repair round existed, no evidence was shown to anything.
        for key in ("plan", "max_repair_rounds", "evidence_policy", "llm_retries"):
            config.pop(key, None)
        gate = external.get("gate_impact") or {}
        config.update(
            {
                "external_label": external.get("label"),
                "external_rtl_dir": external.get("rtl_dir"),
                "external_trials": ", ".join(external.get("trials") or []) or "(none)",
                "external_trial_count": external.get("trial_count"),
                "external_basis_designs": (
                    f"{external.get('basis_designs')} of "
                    f"{external.get('benchmark_designs')} benchmark designs"
                ),
                "external_missing_candidate_samples": external.get("missing_candidate_samples"),
                "external_missing_candidate_designs": external.get("missing_candidate_designs"),
                "illegal_task_gate": "enforced (same as the agent run)",
                "illegal_task_gate_rejected_samples": gate.get("rejected_samples"),
                "illegal_task_gate_would_pass_disabled": gate.get("would_pass_without_gate"),
            }
        )
    if config:
        lines.append("## Configuration")
        lines.append("")
        lines.append("| setting | value |")
        lines.append("| --- | --- |")
        for key in sorted(config):
            lines.append(f"| {key} | `{config[key]}` |")
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
    lines.append(
        f"| func pass, round 0 only (designs) | {totals['designs_func_success_round0']}/{totals['designs']}"
        f" ({_pct(totals['designs_func_rate_round0'])}) | {adjusted['designs_func_success_round0']}"
        f"/{adjusted['designs']} ({_pct(adjusted['designs_func_rate_round0'])}) |"
    )
    rounds = (report.get("agent_config") or {}).get("max_repair_rounds")
    # A reference/empty run has exactly one round per design, so naming a repair budget it
    # never used would misdescribe the number.
    repair_note = f" (up to {rounds} repair rounds)" if rounds and mode == "llm" else ""
    lines.append(
        f"| pass@1, with repair{repair_note} | {_num(totals['pass@1_with_repair'])} | "
        f"{_num(adjusted['pass@1_with_repair'])} |"
    )
    lines.append(
        f"| pass@1, round 0 (single-shot, RTLLM-comparable) | {_num(totals['pass@1_round0'])} | "
        f"{_num(adjusted['pass@1_round0'])} |"
    )
    if k != 1:  # with one sample per design pass@k IS pass@1; printing it twice invites misreading
        scored = totals.get("pass@k_designs_scored")
        over = f" (over {scored}/{totals['designs']} designs)" if scored != totals["designs"] else ""
        lines.append(f"| pass@{k}, with repair{over} | {_num(totals['pass@k'])} | {_num(adjusted['pass@k'])} |")
    lines.append("")
    lines.append(
        "Official oracle = the benchmark's own rule (stdout contains `Pass`/`pass`). "
        "Strict additionally requires no failure banner, no timeout and no runaway output."
    )
    lines.append(
        "`pass@1, with repair` counts a sample as a success if ANY of its rounds passed, so it "
        "is the agent's score, not the base model's. Only `pass@1, round 0` is comparable to a "
        "published single-shot RTLLM pass@1."
    )
    if mode == "external":
        lines.append("")
        lines.append(
            "This run scored **pre-generated files**: every sample has exactly one round, so "
            "`pass@1, with repair` and `pass@1, round 0` are the same number by construction. "
            "The agent number to compare against is its **round 0** column, not its with-repair "
            "one -- these candidates had no verifier in the loop."
        )
    lines.append("")

    lines.append("## Designs")
    lines.append("")
    lines.append("| design | category | syntax | func | repair rounds | failure family |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in report["designs"]:
        note = ""
        if row["known_oracle_issue"]:
            note = " (broken oracle)"
        elif row.get("vacuous_oracle"):
            note = " (vacuous oracle)"
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
    if external:
        gate = external.get("gate_impact") or {}
        lines.append(
            f"**Basis.** `{external.get('label')}` shipped RTL for "
            f"{external.get('basis_designs')} of the {external.get('benchmark_designs')} "
            "benchmark designs discovered here. Only those are in the denominator; the rest "
            "were never attempted and are not counted as failures. Any rate from this report "
            "is a rate over that basis, not over the whole benchmark."
        )
        lines.append("")
        missing = external.get("missing_candidates") or {}
        if missing:
            lines.append(
                f"**Missing candidates.** {external.get('missing_candidate_samples')} "
                f"design-sample(s) across {external.get('missing_candidate_designs')} design(s) "
                "have no file in some trial. Each is scored as a FAILED sample of that trial "
                "(family `missing_candidate`), not dropped -- dropping would shrink the "
                "denominator and raise the rate:"
            )
            lines.append("")
            for name, trials in sorted(missing.items()):
                lines.append(f"- `{name}`: absent from {', '.join(trials)}")
            lines.append("")
        else:
            lines.append("- Every design in the basis has a candidate file in every trial.")
            lines.append("")
        fallbacks = external.get("resolved_by_fallback") or []
        if fallbacks:
            lines.append(
                "**Filename fallbacks.** These candidates were not found at `<design>.v` and "
                "were resolved another way. Listed rather than applied silently, because each "
                "is a judgement about whose file this is:"
            )
            lines.append("")
            for entry in fallbacks:
                lines.append(
                    f"- `{entry.get('design')}` / {entry.get('trial')}: "
                    f"`{Path(str(entry.get('path'))).name}` (matched by {entry.get('resolved_by')})"
                )
            lines.append("")
        lines.append(
            "**Illegal-system-task gate.** Applied identically to the agent's RTL and to this "
            f"RTL. It rejected {gate.get('rejected_samples')} design-sample(s) here; with the "
            f"gate disabled {gate.get('would_pass_without_gate')} of them would have reported a "
            "pass. That delta is the most this comparison could be understating "
            f"`{external.get('label')}`. It is not itself a score: the oracle greps the "
            "simulator's stdout, the design shares that stream with the testbench, so an "
            "ungated pass cannot be told apart from a self-printed one."
        )
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

    vacuous = oracle.get("vacuous_selected_designs") or []
    if vacuous:
        lines.append(
            "These selected designs have a **vacuous oracle**: a module with the right ports and "
            "no logic at all passes them, so every agent -- including one that emits an empty "
            "module -- banks them for free. They are counted in `totals` and excluded from "
            "`adjusted` (but NOT from `adjusted_unpassable_only`). Re-measure with "
            "`--empty-baseline`:"
        )
        lines.append("")
        for name in vacuous:
            lines.append(f"- `{name}`: {(oracle.get('vacuous_issues') or {}).get(name, 'passes an empty module')}")
        lines.append("")

    backend_failed = report.get("llm_error_designs") or []
    if backend_failed:
        lines.append(
            f"**{len(backend_failed)} design(s) failed because the LLM backend errored**, not "
            "because the RTL was wrong: "
            + ", ".join(f"`{name}`" for name in backend_failed)
            + ". They score 0 in `totals` and are excluded from `adjusted`. A rate that includes "
            "them measures the backend's uptime, not the model."
        )
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
            "unavailable at startup, 3 the sweep completed but at least one design scored 0 "
            "because the backend errored mid-run, 130 interrupted (a partial report is still "
            "written)."
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
    parser.add_argument(
        "--empty-baseline",
        action="store_true",
        help=(
            "Evaluate a port-only module with NO LOGIC instead of calling a model: the oracle "
            "floor. Any design it passes has a vacuous oracle and its score means nothing "
            "(no LLM is constructed)"
        ),
    )
    parser.add_argument(
        "--external-rtl",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Score pre-generated RTL from another model through this same pipeline instead of "
            "calling one: DIR/<design>.v, or DIR/<trial>/<design>.v where each trial is one "
            "sample (no LLM is constructed). Mutually exclusive with --reference/--empty-baseline"
        ),
    )
    parser.add_argument(
        "--label",
        default=None,
        metavar="NAME",
        help="Name of the model that produced --external-rtl, recorded in the report (default: DIR basename)",
    )
    parser.add_argument(
        "--no-gate-impact",
        action="store_true",
        help=(
            "Skip the second, gate-disabled run of every candidate the illegal-system-task gate "
            "refuses. That measurement is on by default for --external-rtl so the gate's cost is "
            "stated rather than assumed; it never changes a reported verdict"
        ),
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
    """Resolve ``--designs``/``--exclude``/``--limit`` into the list of designs to run.

    A ``--designs`` name that matches nothing is fatal. Silently dropping it turns one typo
    (or one renamed design in a scripted list) into a sweep over whatever survived, reported
    as ``1/1 designs func-pass (100.0%)`` with nothing anywhere saying the other 49 were
    never run. Matching in ``discover_designs`` is exact and case-sensitive, so "matched
    nothing" always means the caller asked for something that is not there.
    """

    discovered = {design.name for design in rtllm_bench.discover_designs(root)}
    designs = rtllm_bench.discover_designs(root, include=args.designs, exclude=args.exclude)

    unmatched = [name for name in dict.fromkeys(args.designs or ()) if name not in discovered]
    if unmatched:
        raise SystemExit(
            f"--designs named {len(unmatched)} design(s) that do not exist under {root}: "
            + ", ".join(unmatched)
            + "\nNames are matched exactly and case-sensitively. Available designs: "
            + (", ".join(sorted(discovered)) if discovered else "(none discovered)")
        )

    if not designs:
        raise SystemExit(
            f"no RTLLM designs found under {root}"
            + (f" matching --designs {' '.join(args.designs)}" if args.designs else "")
            + ". A design directory needs both design_description.txt and testbench.v."
        )

    # An --exclude that matches nothing removed nothing, so it cannot corrupt a score the way
    # an unmatched --designs can; it is still a typo the user should hear about.
    stale = [name for name in dict.fromkeys(args.exclude or ()) if name not in discovered]
    if stale:
        print(
            "warning: --exclude named design(s) that do not exist and excluded nothing: "
            + ", ".join(stale),
            file=sys.stderr,
            flush=True,
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

    if args.reference and args.empty_baseline:
        raise SystemExit(
            "--reference and --empty-baseline are two different measurements (the oracle's "
            "ceiling and its floor). Run them into separate --out-dirs."
        )
    if args.external_rtl is not None and (args.reference or args.empty_baseline):
        raise SystemExit(
            "--external-rtl scores another model's pre-generated RTL; --reference and "
            "--empty-baseline score the benchmark's own golden RTL and an empty module. They "
            "are three different measurements. Run them into separate --out-dirs."
        )
    if args.label is not None and args.external_rtl is None:
        raise SystemExit("--label names the model behind --external-rtl and needs it.")

    root = resolve_benchmark(args)
    designs = select_designs(root, args)
    external_mode = args.external_rtl is not None
    harness_mode = args.reference or args.empty_baseline or external_mode
    mode = (
        "external"
        if external_mode
        else ("reference" if args.reference else ("empty" if args.empty_baseline else "llm"))
    )
    config = make_agent_config(args)

    trials: "list[Path]" = []
    external_meta: "dict[str, Any] | None" = None
    if external_mode:
        rtl_dir = Path(args.external_rtl).expanduser()
        label = args.label or rtl_dir.name
        trials = discover_external_trials(rtl_dir)
        benchmark_designs = len(designs)
        designs = external_basis(trials, designs)
        if not designs:
            raise SystemExit(
                f"--external-rtl {rtl_dir} has no candidate file for any of the "
                f"{benchmark_designs} selected benchmark design(s). Nothing to score."
            )
        if args.samples != 1 and args.samples != len(trials):
            print(
                f"warning: --samples {args.samples} ignored for --external-rtl: the sample "
                f"count is the number of trial directories ({len(trials)}).",
                file=sys.stderr,
                flush=True,
            )
        # The report's Configuration block must describe what was actually scored.
        config.samples = len(trials)
        external_meta = {
            "label": label,
            "rtl_dir": str(rtl_dir),
            "trials": [trial.name for trial in trials],
            "basis_designs": len(designs),
            "benchmark_designs": benchmark_designs,
            "gate_impact_enabled": not args.no_gate_impact,
        }
        print(
            f"external RTL: label={label} trials={len(trials)} "
            f"({', '.join(t.name for t in trials)}) basis={len(designs)}/{benchmark_designs} designs",
            flush=True,
        )

    client = None
    backend = model = None
    if not harness_mode:
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
    run_config = run_config_fingerprint(
        config,
        root,
        backend=backend,
        model=model,
        extra=(
            {
                "external_rtl": external_meta["rtl_dir"],
                "external_label": external_meta["label"],
            }
            if external_meta
            else {}
        ),
    )
    rows: "list[dict[str, Any]]" = []
    pending = designs
    if args.resume:
        prior = load_prior_rows(results_path)
        check_resume_compatible(prior, mode, run_config, results_path)
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
            if external_mode:
                row = run_external_design(
                    design,
                    trials,
                    config,
                    workdir,
                    gate_impact=not args.no_gate_impact,
                )
            elif harness_mode:
                row = run_reference_design(
                    design, config, workdir, empty_baseline=args.empty_baseline
                )
            else:
                row = run_llm_design(
                    design, client, config, workdir, log if args.verbose else None, stop
                )
        except Exception as exc:  # noqa: BLE001 - one bad design must not kill the sweep
            # n_samples is the sample budget, not 0: a design that blew up is a failure with
            # c=0, and reporting n=0 would drop it out of the pass@k average instead.
            row = {
                "design": design.name,
                "category": design.category,
                "n_samples": (
                    len(trials) if external_mode else (1 if harness_mode else max(1, args.samples))
                ),
                "syntax_success": 0,
                "func_success": 0,
                "samples": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        row["mode"] = mode
        # Stamped per row so --resume can refuse to average rows scored under different
        # settings, and so a stray results.jsonl is self-describing.
        row["run_config"] = run_config
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
        # A reference/empty run has exactly one deterministic sample per design; a pass@5
        # over n=1 would be undefined for every row, so k is pinned to 1 there. An external
        # run has one sample per trial, which is exactly what k means.
        k=(
            len(trials)
            if external_mode
            else (1 if harness_mode else max(1, args.samples))
        ),
        backend=backend,
        model=model,
        agent_config=config.to_dict() if hasattr(config, "to_dict") else {},
        selected=selected_names,
        wall_clock_s=time.time() - started,
        interrupted=interrupted,
        resumed=resumed,
        external=external_meta,
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
        f"pass@1(with repair)={_num(totals['pass@1_with_repair'])}, "
        f"pass@1(round 0)={_num(totals['pass@1_round0'])}{at_k}",
        flush=True,
    )
    if mode == "empty":
        vacuous = sorted(row["design"] for row in report["designs"] if row["func_pass"])
        print(
            "empty-baseline: a design listed here passes with NO LOGIC, so its score is "
            "meaningless -- " + (", ".join(vacuous) if vacuous else "(none)"),
            flush=True,
        )
    if mode == "external":
        section = report.get("external") or {}
        gate = section.get("gate_impact") or {}
        missing = section.get("missing_candidates") or {}
        print(
            f"external[{section.get('label')}]: basis {section.get('basis_designs')}/"
            f"{section.get('benchmark_designs')} designs over {section.get('trial_count')} trials; "
            f"missing candidates {section.get('missing_candidate_samples')} sample(s) across "
            f"{section.get('missing_candidate_designs')} design(s)"
            + (" -- " + ", ".join(f"{n}({','.join(t)})" for n, t in sorted(missing.items())) if missing else "")
            + f"; illegal-task gate rejected {gate.get('rejected_samples')} sample(s), "
            f"{gate.get('would_pass_without_gate')} of which would pass with the gate disabled",
            flush=True,
        )
    backend_failed = report.get("llm_error_designs") or []
    if backend_failed:
        print(
            f"\nwarning: {len(backend_failed)} design(s) scored 0 because the LLM backend "
            f"errored, not because the RTL was wrong: {', '.join(backend_failed)}.\n"
            "Those are not model results. Rerun them with --resume once the backend is back.",
            file=sys.stderr,
            flush=True,
        )
    print(f"report: {out_dir / REPORT_MD}", flush=True)
    if interrupted:
        return 130
    return 3 if backend_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
