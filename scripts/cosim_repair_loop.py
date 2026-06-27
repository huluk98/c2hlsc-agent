#!/usr/bin/env python3
"""Closed-loop HLS_NL cosim + Opus-4.8 repair.

For each selected record:
  1. Generate the Vitis project (dut.cpp + tb.cpp + tcl) and run the C/RTL
     co-simulation ladder (CSim -> CSynth -> CoSim) with a per-phase timeout
     (default 300s = 5 min). CoSim passing == the synthesized RTL is functionally
     equivalent to the HLS-C under the deterministic testbench.
  2. If a phase fails or times out, read the earliest failing Vitis log and ask
     Anthropic Opus 4.8 to regenerate the HLS-C from the original NL spec + the
     failing source + the error evidence.
  3. Rewrite dut.cpp with the repaired code and rerun the cosim ladder to re-check
     functional equivalence. Repeat up to --max-iterations.

Requires on the run host: vitis_hls (VITIS_HLS_BIN or on PATH), the `anthropic`
package, and ANTHROPIC_API_KEY. Repaired sources are written to
<out-dir>/repaired_corpus.jsonl; per-record outcomes to <out-dir>/results.jsonl.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# scripts/ is sys.path[0] when run as a file (sibling imports); add repo root for c2hlsc_agent.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generate_hls_nl_testbenches import (  # noqa: E402
    extract_function,
    load_records,
    record_id_for,
    record_source_file,
    write_design,
)
from run_hls_nl_vitis_batch import (  # noqa: E402
    render_cosim_tcl,
    render_csim_tcl,
    render_csynth_tcl,
    render_verilog_tcl,
    resolve_vitis_hls,
    run_design,
)
from c2hlsc_agent.llm import AnthropicLLMClient, DEFAULT_ANTHROPIC_MODEL, extract_code_blocks  # noqa: E402


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
    phase = result.get("failed_phase", "")
    log = design_dir / f"vitis_{phase}.log"
    if log.exists():
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-120:])
    return str(result.get("vitis_log_tail", ""))


def repair(client: AnthropicLLMClient, record: dict[str, Any], hls_cpp: str, stage: str, evidence: str) -> str | None:
    user = (
        f"Design spec:\n{record.get('HLS_instruction', '')}\n\n"
        f"Current implementation that FAILED Vitis '{stage}':\n```cpp\n{hls_cpp}\n```\n\n"
        f"Vitis {stage} error log (tail):\n{evidence}\n\n"
        "Return the corrected COMPLETE source in one ```cpp block."
    )
    resp = client.complete(REPAIR_SYSTEM, user, max_tokens=6000)
    return pick_code(resp, record.get("top_function", ""))


def select(records: list[dict[str, Any]], args: argparse.Namespace) -> list[tuple[int, dict[str, Any]]]:
    if args.only_failing:
        wanted: set[int] = set()
        for line in Path(args.only_failing).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if str(row.get("status")) not in ("pass",):
                wanted.add(int(row["record_id"]))
        return [(record_id_for(r, i), r) for i, r in enumerate(records) if record_id_for(r, i) in wanted]
    if args.record_id:
        wanted = set(args.record_id)
        return [(record_id_for(r, i), r) for i, r in enumerate(records) if record_id_for(r, i) in wanted]
    sel = records[args.offset:]
    if args.limit is not None:
        sel = sel[: args.limit]
    return [(record_id_for(r, args.offset + i), r) for i, r in enumerate(sel)]


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
    p.add_argument("--part", default="xczu7ev-ffvc1156-2-e")
    p.add_argument("--clock", default="10")
    p.add_argument("--model", default=DEFAULT_ANTHROPIC_MODEL)
    p.add_argument("--log-tail-lines", type=int, default=160)
    args = p.parse_args()

    vitis_hls = resolve_vitis_hls(None, generate_only=False)
    client = AnthropicLLMClient(model=args.model)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    results_path = args.out_dir / "results.jsonl"
    repaired_path = args.out_dir / "repaired_corpus.jsonl"
    rf = results_path.open("w", encoding="utf-8")
    cf = repaired_path.open("w", encoding="utf-8")

    targets = select(load_records(args.input), args)
    n_pass = n_fail = n_repaired = 0
    for record_id, record in targets:
        sig = extract_function(str(record.get("hls_cpp", "")))
        if sig is None:
            rf.write(json.dumps({"record_id": record_id, "status": "skipped", "reason": "unparseable"}) + "\n")
            continue
        hls_cpp = str(record.get("hls_cpp", ""))
        outcome = {"record_id": record_id, "top_function": sig.name, "iterations": [], "repaired": False}
        status = "fail"
        for attempt in range(args.max_iterations + 1):
            record["hls_cpp"] = hls_cpp
            row = write_project(args.out_dir, record, sig, record_id, args.part, args.clock)
            result = run_design(vitis_hls, row, args.timeout_seconds, run_full_cosim=True, log_tail_lines=args.log_tail_lines)
            status = result.get("status", "fail")
            outcome["iterations"].append({"attempt": attempt, "status": status, "failed_phase": result.get("failed_phase")})
            print(f"[{record_id}] {sig.name} attempt={attempt} -> {status}", flush=True)
            if status == "pass":
                break
            if attempt == args.max_iterations:
                break
            stage = result.get("failed_phase", "cosim")
            evidence = failing_evidence(Path(row["path"]), result)
            new_code = repair(client, record, hls_cpp, stage, evidence)
            if not new_code or new_code.strip() == hls_cpp.strip():
                outcome["iterations"][-1]["repair"] = "no_change"
                break
            hls_cpp = new_code
            outcome["repaired"] = True
            print(f"[{record_id}] repaired with {args.model} after '{stage}' failure; re-running cosim", flush=True)

        outcome["status"] = status
        if status == "pass":
            n_pass += 1
            if outcome["repaired"]:
                n_repaired += 1
        else:
            n_fail += 1
        record["hls_cpp"] = hls_cpp
        rf.write(json.dumps(outcome) + "\n")
        rf.flush()
        cf.write(json.dumps({"record_id": record_id, "top_function": sig.name, "hls_cpp": hls_cpp, "cosim_status": status}) + "\n")
        cf.flush()

    rf.close()
    cf.close()
    summary = {"targets": len(targets), "pass": n_pass, "fail": n_fail, "passed_after_repair": n_repaired,
               "results": str(results_path), "repaired_corpus": str(repaired_path)}
    print(json.dumps(summary, indent=2))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
