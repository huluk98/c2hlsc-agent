#!/usr/bin/env python3
"""Closed-loop HLS_NL cosim + Opus-4.8 repair.

For each selected record:
  1. Generate the Vitis project (dut.cpp + tb.cpp + tcl) and run the C/RTL
     co-simulation ladder (CSim -> CSynth -> CoSim) with a per-phase timeout
     (default 300s = 5 min). CoSim passing == the synthesized RTL is functionally
     equivalent to the HLS-C under the deterministic testbench.
  2. If a phase fails or times out, read the earliest failing Vitis log and ask
     Opus 4.8 to regenerate the HLS-C from the original NL spec + the failing source
     + the error evidence.
  3. Rewrite dut.cpp with the repaired code and rerun the cosim ladder to re-check
     functional equivalence. Repeat up to --max-iterations.

Repair backend (--repairer):
  - claude-cli (default): use Claude Code via the `claude` CLI (subscription auth, no
    API key). Set --claude-cmd "ssh you@your-mac claude" to drive Claude Code on a
    remote Mac from the Vitis server.
  - anthropic: use the Anthropic API (needs the `anthropic` package + ANTHROPIC_API_KEY).

Requires on the run host: vitis_hls (VITIS_HLS_BIN or on PATH) and a reachable repair
backend. Repaired sources are written to <out-dir>/repaired_corpus.jsonl; per-record
outcomes to <out-dir>/results.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

# scripts/ is sys.path[0] when run as a file (sibling imports); add repo root for c2hlsc_agent.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generate_hls_nl_testbenches import (  # noqa: E402
    extract_function,
    load_records,
    record_id_for,
    write_design,
)
from run_hls_nl_vitis_batch import (  # noqa: E402
    MAX_VITIS_WORKERS,
    classify_failure,
    failure_fingerprint_for_result,
    render_cosim_tcl,
    render_csim_tcl,
    render_csynth_tcl,
    render_verilog_tcl,
    repair_trailing_newline,
    retry_at,
    retry_backoff_seconds,
    resolve_vitis_hls,
    run_design,
    source_fingerprint,
)
from c2hlsc_agent.evidence_context import distill_evidence  # noqa: E402
from c2hlsc_agent.llm import extract_code_blocks  # noqa: E402
from c2hlsc_agent.run_control import stable_fingerprint  # noqa: E402

Completer = Callable[[str, str], str]  # (system, user) -> raw model text
TERMINAL_STATUSES = {
    "pass",
    "fail",
    "failed",
    "blocked",
    "exhausted",
    "cancelled",
    "skipped",
}


def make_completer(args: argparse.Namespace) -> Completer:
    """Build the repair backend: Claude Code (the `claude` CLI) or the Anthropic API."""
    if args.repairer == "anthropic":
        # Billed API path. Requires the `anthropic` package and ANTHROPIC_API_KEY.
        from c2hlsc_agent.llm import AnthropicLLMClient
        client = AnthropicLLMClient(model=args.model)
        return lambda system, user: client.complete(system, user, max_tokens=6000)

    # Claude Code path (subscription auth, no API key). `claude -p` reads the prompt and
    # prints the answer. Set --claude-cmd "ssh you@mac claude" to drive Claude Code on a
    # remote Mac from the Vitis server.
    base = shlex.split(args.claude_cmd) + ["-p", "--model", args.claude_model]

    def complete(system: str, user: str) -> str:
        prompt = f"{system}\n\n{user}"
        proc = subprocess.run(base, input=prompt, text=True, capture_output=True, timeout=args.repair_timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed (rc={proc.returncode}): {proc.stderr[-800:]}")
        return proc.stdout

    return complete


REPAIR_SYSTEM = (
    "You are an expert AMD/Xilinx Vitis HLS engineer. You are given a natural-language "
    "design spec, a current HLS C/C++ implementation that FAILED a Vitis stage, and the "
    "Vitis error log. Return a corrected, fully synthesizable implementation that passes "
    "that stage and is functionally equivalent to the spec. Keep the EXACT same top-function "
    "name and a sensible synthesizable signature. Return ONLY the complete corrected source "
    "in a single ```cpp code block, nothing else. No dynamic memory, recursion, file I/O, or "
    "unbounded loops; bound every loop by a compile-time constant."
)


def pick_code(resp: str, top_name: str) -> str | None:
    blocks = extract_code_blocks(resp)
    candidates = [c for (lang, c) in blocks if lang.lower() in ("cpp", "c++", "c", "")] or [c for (_, c) in blocks]
    defines = re.compile(rf"\b{re.escape(top_name)}\s*\(")
    for c in candidates:
        if defines.search(c):
            return c.strip() + "\n"
    if candidates:
        return candidates[0].strip() + "\n"
    if defines.search(resp):
        return resp.strip() + "\n"
    return None


def write_project(out_dir: Path, record: dict[str, Any], sig, record_id: int, part: str, clock: str) -> Path:
    row = write_design(out_dir, record, sig, record_id, part, clock, "driver")
    design_dir = Path(row["path"])
    (design_dir / "run_verilog.tcl").write_text(render_verilog_tcl(sig, part, clock), encoding="utf-8")
    (design_dir / "run_csim.tcl").write_text(render_csim_tcl(sig, part, clock), encoding="utf-8")
    (design_dir / "run_csynth.tcl").write_text(render_csynth_tcl(), encoding="utf-8")
    (design_dir / "run_cosim.tcl").write_text(render_cosim_tcl(), encoding="utf-8")
    row["path"] = str(design_dir)
    return row


def failing_evidence(design_dir: Path, result: dict[str, Any]) -> str:
    """Distilled evidence from the earliest failing Vitis log.

    Shares the repair-context definition with the main agent loop
    (c2hlsc_agent.evidence_context): mismatch traces first, then an
    error-anchored log window, instead of a blind last-120-lines slice.
    """
    phase = str(result.get("failed_phase", ""))
    log = design_dir / f"vitis_{phase}.log"
    if log.exists():
        raw = log.read_text(encoding="utf-8", errors="replace")
    else:
        raw = str(result.get("vitis_log_tail", ""))
    return distill_evidence(raw, summary=str(result.get("error") or ""), phase=phase or None).text


def repair(complete: Completer, record: dict[str, Any], hls_cpp: str, stage: str, evidence: str) -> str | None:
    user = (
        f"Design spec:\n{record.get('HLS_instruction', '')}\n\n"
        f"Current implementation that FAILED Vitis '{stage}':\n```cpp\n{hls_cpp}\n```\n\n"
        f"Vitis {stage} error evidence (distilled, mismatches first):\n{evidence}\n\n"
        "Return the corrected COMPLETE source in one ```cpp block."
    )
    resp = complete(REPAIR_SYSTEM, user)
    return pick_code(resp, record.get("top_function", ""))


def load_result_rows(results_path: Path) -> list[dict[str, Any]]:
    """Valid rows from a previous (possibly interrupted) run, last per record_id."""
    if not results_path.exists():
        return []
    by_id: dict[int, dict[str, Any]] = {}
    with results_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            try:
                by_id[int(row["record_id"])] = row
            except (KeyError, TypeError, ValueError):
                continue
    return list(by_id.values())


def load_done_ids(results_path: Path) -> set[int]:
    """record_ids finished in a previous run.

    Retry checkpoints and legacy status=error rows do not count as done.
    """
    return {
        int(row["record_id"])
        for row in load_result_rows(results_path)
        if row.get("status") in TERMINAL_STATUSES
    }


def reconcile_corpus(corpus_path: Path) -> None:
    """Rewrite repaired_corpus.jsonl keeping the last valid row per record_id.

    A crash can leave a torn row, and a record retried after a crash between
    the corpus and results writes appends a second row for the same id — which
    the batch runner's duplicate-record_id guard would then reject.
    """
    if not corpus_path.exists():
        return
    by_id: dict[Any, str] = {}
    with corpus_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and "record_id" in row:
                by_id[row["record_id"]] = line
    tmp = corpus_path.with_name(f"{corpus_path.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(line + "\n" for line in by_id.values()), encoding="utf-8")
    os.replace(tmp, corpus_path)


def load_corpus_rows(corpus_path: Path) -> dict[int, dict[str, Any]]:
    if not corpus_path.exists():
        return {}
    by_id: dict[int, dict[str, Any]] = {}
    for line in corpus_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            record_id = int(row["record_id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if isinstance(row, dict):
            by_id[record_id] = row
    return by_id


def verification_state_fingerprint(
    source_hash: str,
    failure_hash: str | None,
) -> str:
    return stable_fingerprint(
        {"source_fingerprint": source_hash, "failure_fingerprint": failure_hash}
    )


def _exception_result(
    record: dict[str, Any],
    exc: Exception,
    source_hash: str,
    operation: str,
) -> dict[str, Any]:
    result = {
        "record_id": record.get("record_id"),
        "status": "error",
        "failed_phase": operation,
        "error": f"{type(exc).__name__}: {exc}",
        "source_fingerprint": source_hash,
    }
    result["failure_fingerprint"] = failure_fingerprint_for_result(result)
    return result


def _corpus_state(
    record_id: int,
    top_name: str,
    hls_cpp: str,
    status: str,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "top_function": top_name,
        "hls_cpp": hls_cpp,
        "cosim_status": status,
        "source_fingerprint": source_fingerprint(hls_cpp),
    }


def select(records: list[dict[str, Any]], args: argparse.Namespace) -> list[tuple[int, dict[str, Any]]]:
    if args.only_failing:
        wanted: set[int] = set()
        for line in Path(args.only_failing).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Torn final line from an interrupted incremental sweep.
                continue
            if str(row.get("status")) not in ("pass",):
                try:
                    wanted.add(int(row["record_id"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return [(record_id_for(r, i), r) for i, r in enumerate(records) if record_id_for(r, i) in wanted]
    if args.record_id:
        wanted = set(args.record_id)
        return [(record_id_for(r, i), r) for i, r in enumerate(records) if record_id_for(r, i) in wanted]
    sel = records[args.offset:]
    if args.limit is not None:
        sel = sel[: args.limit]
    return [(record_id_for(r, args.offset + i), r) for i, r in enumerate(sel)]


def _state_snapshot(
    state: dict[str, Any],
    status: str,
    reason: str = "",
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in state.items()
        if not key.startswith("_")
    }
    payload["status"] = status
    payload["reason"] = reason
    payload["hls_cpp"] = state["hls_cpp"]
    payload["source_fingerprint"] = source_fingerprint(state["hls_cpp"])
    payload.update(extra)
    return payload


def _persist_state(
    persist: Callable[..., None],
    state: dict[str, Any],
    status: str,
    reason: str = "",
    *,
    terminal: bool,
    **extra: Any,
) -> dict[str, Any]:
    payload = _state_snapshot(state, status, reason, **extra)
    corpus = _corpus_state(
        int(state["record_id"]),
        str(state["top_function"]),
        str(state["hls_cpp"]),
        status,
    )
    persist(payload, corpus, terminal=terminal)
    return payload


def _initial_record_state(
    record_id: int,
    top_name: str,
    hls_cpp: str,
    args: argparse.Namespace,
    prior: dict[str, Any] | None,
) -> dict[str, Any]:
    prior = prior or {}
    current = str(prior.get("hls_cpp") or hls_cpp)
    source_hash = source_fingerprint(current)
    seen_sources = list(prior.get("seen_source_fingerprints", []))
    if source_hash not in seen_sources:
        seen_sources.append(source_hash)
    return {
        "record_id": record_id,
        "top_function": top_name,
        "hls_cpp": current,
        "iterations": list(prior.get("iterations", [])),
        "repaired": bool(prior.get("repaired", False)),
        "repairs_used": int(prior.get("repairs_used", 0)),
        "retry_count": int(prior.get("retry_count", 0)),
        "max_iterations": args.max_iterations,
        "max_infra_retries": args.max_infra_retries,
        "seen_source_fingerprints": seen_sources,
        "seen_state_fingerprints": list(
            prior.get("seen_state_fingerprints", [])
        ),
        "_pending_operation": prior.get("pending_operation"),
        "_operation_retry_count": int(
            prior.get("operation_retry_count", 0)
        ),
        "_pending_state_fingerprint": prior.get("pending_state_fingerprint"),
    }


def _verify_current_source(
    state: dict[str, Any],
    record: dict[str, Any],
    args: argparse.Namespace,
    vitis_hls: str,
    persist: Callable[..., None],
    stop_event: threading.Event,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    operation_retries = (
        int(state.get("_operation_retry_count", 0))
        if state.get("_pending_operation") == "verify"
        else 0
    )
    while True:
        if stop_event.is_set():
            terminal = _persist_state(
                persist,
                state,
                "cancelled",
                "worker cancellation requested",
                terminal=True,
                dead_letter=False,
            )
            return None, terminal

        source_hash = source_fingerprint(state["hls_cpp"])
        record["record_id"] = state["record_id"]
        record["hls_cpp"] = state["hls_cpp"]
        try:
            row = write_project(
                args.out_dir,
                record,
                state["_signature"],
                state["record_id"],
                args.part,
                args.clock,
            )
            result = run_design(
                vitis_hls,
                row,
                args.timeout_seconds,
                run_full_cosim=True,
                log_tail_lines=args.log_tail_lines,
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            row = {"path": str(args.out_dir)}
            result = _exception_result(record, exc, source_hash, "vitis")

        result.setdefault("source_fingerprint", source_hash)
        result.setdefault(
            "failure_fingerprint",
            failure_fingerprint_for_result(result),
        )
        failure_class, retryable = classify_failure(result)
        entry = {
            "attempt": len(state["iterations"]),
            "status": result.get("status", "error"),
            "failed_phase": result.get("failed_phase"),
            "source_fingerprint": source_hash,
            "failure_fingerprint": result.get("failure_fingerprint"),
            "failure_class": failure_class,
            "retryable": retryable,
            "operation_retry_count": operation_retries,
        }
        state["iterations"].append(entry)

        if result.get("status") == "pass":
            state["_pending_operation"] = None
            state["_operation_retry_count"] = 0
            return result, None
        if not retryable:
            state["_pending_operation"] = None
            state["_operation_retry_count"] = 0
            return result, None

        state["retry_count"] += 1
        if operation_retries >= args.max_infra_retries:
            terminal = _persist_state(
                persist,
                state,
                "exhausted",
                "Vitis infrastructure retry budget exhausted",
                terminal=True,
                failure_fingerprint=result.get("failure_fingerprint"),
                failure_class=failure_class,
                retryable=False,
                dead_letter=True,
            )
            return result, terminal

        operation_retries += 1
        delay = retry_backoff_seconds(
            args.retry_backoff_seconds,
            operation_retries,
        )
        entry["backoff_seconds"] = delay
        state["_pending_operation"] = "verify"
        state["_operation_retry_count"] = operation_retries
        _persist_state(
            persist,
            state,
            "retry_pending",
            "retryable Vitis infrastructure failure",
            terminal=False,
            pending_operation="verify",
            operation_retry_count=operation_retries,
            backoff_seconds=delay,
            retry_at=retry_at(delay),
            failure_fingerprint=result.get("failure_fingerprint"),
            failure_class=failure_class,
            retryable=True,
            dead_letter=False,
        )
        if stop_event.wait(delay):
            continue


def _repair_failed_source(
    state: dict[str, Any],
    record: dict[str, Any],
    args: argparse.Namespace,
    complete: Completer,
    result: dict[str, Any],
    persist: Callable[..., None],
    repairer_label: str,
    stop_event: threading.Event,
    resume_pending_state: str | None = None,
    resume_retry_count: int = 0,
) -> dict[str, Any] | None:
    source_hash = source_fingerprint(state["hls_cpp"])
    failure_hash = result.get("failure_fingerprint")
    state_hash = verification_state_fingerprint(source_hash, failure_hash)
    seen_states = state["seen_state_fingerprints"]
    if state_hash in seen_states and state_hash != resume_pending_state:
        return _persist_state(
            persist,
            state,
            "exhausted",
            "the same source and failure state recurred",
            terminal=True,
            failure_fingerprint=failure_hash,
            repeated_state_fingerprint=state_hash,
            retryable=False,
            dead_letter=True,
        )
    if state_hash not in seen_states:
        seen_states.append(state_hash)

    if state["repairs_used"] >= args.max_iterations:
        return _persist_state(
            persist,
            state,
            "exhausted",
            "repair iteration budget exhausted",
            terminal=True,
            failure_fingerprint=failure_hash,
            retryable=False,
            dead_letter=True,
        )

    stage = str(result.get("failed_phase") or "cosim")
    design_dir = Path(str(result.get("path") or args.out_dir))
    evidence = failing_evidence(design_dir, result)
    operation_retries = resume_retry_count if state_hash == resume_pending_state else 0

    while True:
        if stop_event.is_set():
            return _persist_state(
                persist,
                state,
                "cancelled",
                "worker cancellation requested",
                terminal=True,
                failure_fingerprint=failure_hash,
                dead_letter=False,
            )
        try:
            new_code = repair(
                complete,
                record,
                state["hls_cpp"],
                stage,
                evidence,
            )
        except Exception as exc:  # noqa: BLE001 - classified and bounded below
            repair_result = _exception_result(
                record,
                exc,
                source_hash,
                "repair_backend",
            )
            failure_class, retryable = classify_failure(repair_result)
            state["retry_count"] += 1
            if not retryable:
                return _persist_state(
                    persist,
                    state,
                    "blocked",
                    "non-retryable repair backend failure",
                    terminal=True,
                    failure_fingerprint=repair_result.get("failure_fingerprint"),
                    failure_class=failure_class,
                    retryable=False,
                    dead_letter=True,
                )
            if operation_retries >= args.max_infra_retries:
                return _persist_state(
                    persist,
                    state,
                    "exhausted",
                    "repair backend retry budget exhausted",
                    terminal=True,
                    failure_fingerprint=repair_result.get("failure_fingerprint"),
                    failure_class=failure_class,
                    retryable=False,
                    dead_letter=True,
                )
            operation_retries += 1
            delay = retry_backoff_seconds(
                args.retry_backoff_seconds,
                operation_retries,
            )
            state["_pending_operation"] = "repair"
            state["_operation_retry_count"] = operation_retries
            state["_pending_state_fingerprint"] = state_hash
            _persist_state(
                persist,
                state,
                "retry_pending",
                "retryable repair backend failure",
                terminal=False,
                pending_operation="repair",
                pending_state_fingerprint=state_hash,
                operation_retry_count=operation_retries,
                backoff_seconds=delay,
                retry_at=retry_at(delay),
                failure_fingerprint=repair_result.get("failure_fingerprint"),
                failure_class=failure_class,
                retryable=True,
                dead_letter=False,
            )
            if stop_event.wait(delay):
                continue
            continue

        if not new_code or new_code.strip() == state["hls_cpp"].strip():
            return _persist_state(
                persist,
                state,
                "blocked",
                "repair produced no source change",
                terminal=True,
                failure_fingerprint=failure_hash,
                retryable=False,
                dead_letter=True,
            )

        new_hash = source_fingerprint(new_code)
        if new_hash in state["seen_source_fingerprints"]:
            return _persist_state(
                persist,
                state,
                "exhausted",
                "repair returned to a previously seen source state",
                terminal=True,
                failure_fingerprint=failure_hash,
                repeated_source_fingerprint=new_hash,
                retryable=False,
                dead_letter=True,
            )

        state["hls_cpp"] = new_code
        state["repaired"] = True
        state["repairs_used"] += 1
        state["seen_source_fingerprints"].append(new_hash)
        state["_pending_operation"] = "verify"
        state["_operation_retry_count"] = 0
        state["_pending_state_fingerprint"] = None
        _persist_state(
            persist,
            state,
            "running",
            "repaired source persisted before verification",
            terminal=False,
            pending_operation="verify",
            operation_retry_count=0,
            next_action="verify repaired source",
            failure_fingerprint=failure_hash,
            retryable=False,
            dead_letter=False,
        )
        print(
            f"[{state['record_id']}] repaired via {repairer_label} after '{stage}' failure; re-running cosim",
            flush=True,
        )
        return None


def run_record_loop(
    record_id: int,
    record: dict[str, Any],
    args: argparse.Namespace,
    vitis_hls: str,
    complete: Completer,
    persist: Callable[..., None],
    repairer_label: str,
    stop_event: threading.Event,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prior = prior or {}
    for key, current in (
        ("max_iterations", args.max_iterations),
        ("max_infra_retries", args.max_infra_retries),
    ):
        previous = prior.get(key)
        if previous is not None and int(previous) != int(current):
            raise RuntimeError(
                f"resume {key} mismatch: prior={previous}, current={current}"
            )

    hls_cpp = str(prior.get("hls_cpp") or record.get("hls_cpp", ""))
    sig = extract_function(hls_cpp)
    if sig is None:
        payload = {
            "record_id": record_id,
            "status": "skipped",
            "reason": "unparseable",
            "hls_cpp": hls_cpp,
            "source_fingerprint": source_fingerprint(hls_cpp),
            "dead_letter": True,
        }
        persist(payload, None, terminal=True)
        return payload

    state = _initial_record_state(
        record_id,
        sig.name,
        hls_cpp,
        args,
        prior,
    )
    state["_signature"] = sig
    record["top_function"] = sig.name
    while True:
        resume_pending_state = (
            state.get("_pending_state_fingerprint")
            if state.get("_pending_operation") == "repair"
            else None
        )
        resume_retry_count = (
            int(state.get("_operation_retry_count", 0))
            if resume_pending_state
            else 0
        )
        result, terminal = _verify_current_source(
            state,
            record,
            args,
            vitis_hls,
            persist,
            stop_event,
        )
        if terminal is not None:
            return terminal
        assert result is not None
        status = str(result.get("status", "fail"))
        print(
            f"[{record_id}] {sig.name} attempt={len(state['iterations']) - 1} -> {status}",
            flush=True,
        )
        if status == "pass":
            return _persist_state(
                persist,
                state,
                "pass",
                "all required Vitis phases passed",
                terminal=True,
                failure_fingerprint=None,
                retryable=False,
                dead_letter=False,
            )

        failure_class, retryable = classify_failure(result)
        if status == "error" and not retryable:
            return _persist_state(
                persist,
                state,
                "blocked",
                "non-retryable worker configuration failure",
                terminal=True,
                failure_fingerprint=result.get("failure_fingerprint"),
                failure_class=failure_class,
                retryable=False,
                dead_letter=True,
            )

        terminal = _repair_failed_source(
            state,
            record,
            args,
            complete,
            result,
            persist,
            repairer_label,
            stop_event,
            resume_pending_state=resume_pending_state,
            resume_retry_count=resume_retry_count,
        )
        if terminal is not None:
            return terminal


def main() -> int:
    p = argparse.ArgumentParser(description="HLS_NL cosim + Opus-4.8 repair loop.")
    p.add_argument("--input", required=True, type=Path, help="Corpus JSONL (record_id, top_function, hls_cpp, HLS_instruction)")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--timeout-seconds", type=int, default=300, help="Per-Vitis-phase timeout (default 300 = 5 min)")
    p.add_argument("--max-iterations", type=int, default=2, help="Repair+recosim attempts after the first cosim")
    p.add_argument("--record-id", type=int, action="append", help="Limit to specific record id(s); repeatable")
    p.add_argument("--only-failing", type=Path, help="A vitis_batch_results.jsonl; repair only its non-pass record_ids")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=1,
                   help="Concurrent records (default 1). Each worker runs its own Vitis ladder and repair call, "
                        "so one record's Claude repair overlaps another's cosim; ~2-6GB RAM per Vitis run.")
    p.add_argument(
        "--max-infra-retries",
        type=int,
        default=2,
        help="Retryable Vitis/model infrastructure failures per operation (default 2)",
    )
    p.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=5.0,
        help="Initial retry backoff; doubles to a 60-second cap (default 5)",
    )
    p.add_argument("--resume", action="store_true",
                   help="Skip record_ids already present in <out-dir>/results.jsonl and append instead of overwriting")
    p.add_argument("--part", default="xczu7ev-ffvc1156-2-e")
    p.add_argument("--clock", default="10")
    p.add_argument("--log-tail-lines", type=int, default=160)
    # Repair backend.
    p.add_argument("--repairer", choices=["claude-cli", "anthropic"], default="claude-cli",
                   help="claude-cli: repair via Claude Code (the `claude` CLI, subscription auth, no API key; default). "
                        "anthropic: repair via the Anthropic API (needs the `anthropic` package + ANTHROPIC_API_KEY).")
    p.add_argument("--claude-cmd", default="claude",
                   help="Base command for the claude-cli backend. Use e.g. \"ssh you@your-mac claude\" to drive "
                        "Claude Code on a remote Mac from the Vitis server.")
    p.add_argument("--claude-model", default="opus", help="Model passed to `claude --model` (default opus; pin with claude-opus-4-8)")
    p.add_argument("--repair-timeout", type=int, default=900, help="Timeout (s) for one Claude repair call")
    p.add_argument("--model", default="claude-opus-4-8", help="Model id for the anthropic backend")
    args = p.parse_args()
    if args.timeout_seconds < 1 or args.repair_timeout < 1:
        p.error("Vitis and repair timeouts must be at least 1 second")
    if args.max_iterations < 0 or args.max_infra_retries < 0:
        p.error("iteration and retry limits cannot be negative")
    if args.retry_backoff_seconds < 0:
        p.error("--retry-backoff-seconds cannot be negative")
    if args.log_tail_lines < 0:
        p.error("--log-tail-lines cannot be negative")
    if not 1 <= args.workers <= MAX_VITIS_WORKERS:
        p.error(f"--workers must be between 1 and {MAX_VITIS_WORKERS}")

    vitis_hls = resolve_vitis_hls(None, generate_only=False)
    complete = make_completer(args)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    results_path = args.out_dir / "results.jsonl"
    repaired_path = args.out_dir / "repaired_corpus.jsonl"
    prior_states: dict[int, dict[str, Any]] = {}
    repaired_rows: dict[int, dict[str, Any]] = {}

    targets = select(load_records(args.input), args)
    id_counts: dict[int, int] = {}
    for record_id, _ in targets:
        id_counts[record_id] = id_counts.get(record_id, 0) + 1
    duplicates = sorted(record_id for record_id, n in id_counts.items() if n > 1)
    if duplicates:
        raise SystemExit(f"duplicate record_id values in input: {duplicates[:10]}")
    skipped_done = 0
    counts = {"pass": 0, "fail": 0, "repaired": 0}
    if args.resume:
        repair_trailing_newline(results_path)
        reconcile_corpus(repaired_path)
        prior_states = {
            int(row["record_id"]): row
            for row in load_result_rows(results_path)
        }
        repaired_rows = load_corpus_rows(repaired_path)
        done_ids = load_done_ids(results_path)
        skipped_done = sum(1 for record_id, _ in targets if record_id in done_ids)
        targets = [(record_id, record) for record_id, record in targets if record_id not in done_ids]
        print(f"resume: {skipped_done} records already done, {len(targets)} to run", flush=True)
        # Seed counters from prior rows so the summary and exit code describe
        # the whole results file, not just this invocation's records.
        for row in load_result_rows(results_path):
            status = row.get("status")
            if status not in TERMINAL_STATUSES:
                continue
            if status == "pass":
                counts["pass"] += 1
                if row.get("repaired"):
                    counts["repaired"] += 1
            elif status not in ("skipped", "error"):
                counts["fail"] += 1
    mode = "a" if args.resume else "w"
    rf = results_path.open(mode, encoding="utf-8")
    cf = repaired_path.open(mode, encoding="utf-8")
    write_lock = threading.Lock()
    stop_event = threading.Event()

    repairer_label = (
        f"Claude Code ({args.claude_cmd} --model {args.claude_model})"
        if args.repairer == "claude-cli" else f"Anthropic API ({args.model})"
    )

    def emit(
        outcome_row: dict[str, Any],
        corpus_row: dict[str, Any] | None,
        *,
        terminal: bool = True,
    ) -> None:
        # Written and flushed per record so an interrupted run keeps everything
        # finished so far and --resume can continue from it. The corpus row goes
        # first: the results row is the --resume done-marker, so done-ness must
        # imply the repaired source is already on disk. (A crash between the two
        # leaves an extra corpus row; reconcile_corpus dedupes it on resume.)
        with write_lock:
            if corpus_row is not None:
                cf.write(json.dumps(corpus_row) + "\n")
                cf.flush()
            rf.write(json.dumps(outcome_row) + "\n")
            rf.flush()
            if not terminal:
                return
            status = outcome_row.get("status")
            if status == "pass":
                counts["pass"] += 1
                if outcome_row.get("repaired"):
                    counts["repaired"] += 1
            elif status != "skipped":
                counts["fail"] += 1

    def process_record(record_id: int, record: dict[str, Any]) -> None:
        working_record = dict(record)
        prior = prior_states.get(record_id)
        recovered = repaired_rows.get(record_id)
        if prior and prior.get("status") not in TERMINAL_STATUSES:
            latest_source = prior.get("hls_cpp")
            if latest_source:
                working_record["hls_cpp"] = latest_source
            elif recovered and recovered.get("hls_cpp"):
                working_record["hls_cpp"] = recovered["hls_cpp"]
        try:
            run_record_loop(
                record_id,
                working_record,
                args,
                vitis_hls,
                complete,
                emit,
                repairer_label,
                stop_event,
                prior=prior,
            )
        except Exception as exc:  # noqa: BLE001 - a flaky repair backend must not kill the sweep
            print(f"[{record_id}] error: {type(exc).__name__}: {exc}", flush=True)
            latest = {
                int(row["record_id"]): row
                for row in load_result_rows(results_path)
            }.get(record_id, prior or {})
            hls_cpp = str(
                latest.get("hls_cpp")
                or working_record.get("hls_cpp", "")
            )
            source_hash = source_fingerprint(hls_cpp)
            payload = {
                **latest,
                "record_id": record_id,
                "status": "blocked",
                "reason": "unexpected non-retryable worker error",
                "error": f"{type(exc).__name__}: {exc}",
                "hls_cpp": hls_cpp,
                "source_fingerprint": source_hash,
                "retryable": False,
                "dead_letter": True,
            }
            emit(
                payload,
                _corpus_state(
                    record_id,
                    str(payload.get("top_function", "")),
                    hls_cpp,
                    "blocked",
                ),
            )

    # Records are independent (each gets its own design dir keyed by record_id),
    # so with --workers > 1 one record's Claude repair call overlaps another
    # record's Vitis ladder instead of leaving the machine idle.
    if args.workers <= 1:
        for record_id, record in targets:
            process_record(record_id, record)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(process_record, record_id, record) for record_id, record in targets]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                # Ctrl-C etc.: cancel queued records; in-flight ones stop at
                # their next attempt boundary. Finished rows are on disk for
                # --resume.
                stop_event.set()
                for future in futures:
                    future.cancel()
                raise

    rf.close()
    cf.close()
    summary = {"targets": len(targets), "already_done": skipped_done, "workers": args.workers,
               "max_iterations": args.max_iterations,
               "max_infra_retries": args.max_infra_retries,
               "retry_backoff_seconds": args.retry_backoff_seconds,
               "pass": counts["pass"], "fail": counts["fail"], "passed_after_repair": counts["repaired"],
               "results": str(results_path), "repaired_corpus": str(repaired_path)}
    print(json.dumps(summary, indent=2))
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
