#!/usr/bin/env python3
"""Batch-generate HLS-C for every HLS_NL record by sending each record's prompt to a model.

Each record's ``HLS_instruction`` is already a complete generation prompt (natural-language
design spec -> "Return only the HLS C/C++ implementation"). This driver streams the whole
dataset through the pluggable LLM backend (local OpenAI-compatible server, OpenAI-compatible
cloud, or the Anthropic API) and APPENDS one result line per record to a single output JSONL.

The output schema carries ``hls_cpp`` + ``top_function`` + ``record_id`` so it is a drop-in
input for ``run_hls_nl_vitis_batch.py`` -- run cosim on it separately.

The run is resumable: rerunning skips ``record_id``s already present in the output file, so a
crashed or partial 10k run continues where it left off. Use ``--backend reference`` to emit
the dataset's own accepted ``hls_cpp`` (no model; for plumbing checks and as a baseline).

Examples
--------
Local model (Ollama), no cloud key, full dataset, appended to one file:

    python scripts/generate_hls_nl_llm.py \
      --input data/hls_nl/hls_nl_repaired.accepted.jsonl \
      --out build/hls_nl_generated.jsonl \
      --backend openai --base-url http://localhost:11434/v1 --model qwen2.5-coder \
      --workers 4

Then cosim separately:

    VITIS_HLS_BIN=/path/to/vitis_hls \
    python scripts/run_hls_nl_vitis_batch.py \
      --input build/hls_nl_generated.jsonl --out-dir build/hls_nl_cosim --run-full-cosim
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(REPO_ROOT))

from generate_hls_nl_testbenches import (  # noqa: E402  (sibling script)
    load_records,
    record_design_title,
    record_id_for,
)
from c2hlsc_agent.llm import (  # noqa: E402
    AnthropicLLMClient,
    OpenAICompatibleLLMClient,
    _env,
    extract_code_blocks,
)

DEFAULT_INPUT = REPO_ROOT / "data" / "hls_nl" / "hls_nl_repaired.accepted.jsonl"
SYSTEM_PROMPT = (
    "You are a Vitis HLS code generation assistant. Return ONLY the synthesizable Vitis "
    "HLS C/C++ implementation for the requested design, inside a single ```cpp code block."
)

_CODE_LANGS = {"", "c", "cc", "cpp", "c++", "cxx", "h", "hpp", "hxx"}


def code_from_response(text: str) -> str:
    """Extract the HLS-C implementation from a model response (fenced or raw)."""
    blocks = extract_code_blocks(text)
    code = [body for lang, body in blocks if lang in _CODE_LANGS] or [body for _l, body in blocks]
    if code:
        return max(code, key=len).strip()
    return (text or "").strip()


def build_client(args: argparse.Namespace):
    if args.backend in ("reference", "replay"):
        return None
    if args.backend == "anthropic":
        return AnthropicLLMClient(model=args.model or "claude-opus-4-8")
    # openai-compatible (local or cloud)
    base_url = args.base_url or _env("C2HLSC_LLM_BASE_URL", "OPENAI_BASE_URL") or "http://localhost:11434/v1"
    api_key = _env("C2HLSC_LLM_API_KEY", "OPENAI_API_KEY")
    return OpenAICompatibleLLMClient(base_url=base_url, model=args.model or "qwen2.5-coder", api_key=api_key)


def done_record_ids(out_path: Path) -> set[Any]:
    if not out_path.exists():
        return set()
    done: set[Any] = set()
    with out_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line).get("record_id"))
            except json.JSONDecodeError:
                continue
    return done


def generate_one(client, args: argparse.Namespace, record: dict, index: int) -> dict:
    record_id = record_id_for(record, index)
    top = record.get("top_function")
    title = record_design_title(record)
    instruction = str(record.get("HLS_instruction", ""))
    if args.backend == "reference":
        model_label = "reference(dataset)"
    elif args.backend == "replay":
        model_label = args.model or "claude-opus-4-8(in-task)"
    else:
        model_label = getattr(client, "model", "?")
    base = {
        "record_id": record_id,
        "top_function": top,
        "design_title": title,
        "original_file": record.get("original_file"),
        "backend": args.backend,
        "model": model_label,
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
    }
    try:
        if args.backend == "reference":  # re-emit the accepted hls_cpp (no model)
            hls_cpp = str(record.get("hls_cpp", "")).strip()
        elif args.backend == "replay":  # pre-recorded model responses keyed by record_id
            hls_cpp = code_from_response(args._replay.get(str(record_id), ""))
        else:
            response = client.complete(SYSTEM_PROMPT, instruction, max_tokens=args.max_tokens)
            hls_cpp = code_from_response(response)
        ok = bool(hls_cpp)
        return {**base, "ok": ok, "hls_cpp": hls_cpp, **({} if ok else {"error": "empty generation"})}
    except Exception as exc:  # noqa: BLE001 - record failures rather than abort the batch
        return {**base, "ok": False, "hls_cpp": "", "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="HLS_NL JSON/JSONL dataset")
    parser.add_argument("--out", type=Path, required=True, help="appended output JSONL (one generation per line)")
    parser.add_argument("--backend", choices=["openai", "anthropic", "reference", "replay"], default="openai")
    parser.add_argument("--base-url", help="base URL for --backend openai (e.g. http://localhost:11434/v1)")
    parser.add_argument("--model", help="model id (default per backend)")
    parser.add_argument("--replay-file", type=Path, help="JSON map record_id -> model response, for --backend replay")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--limit", type=int, help="max records to process this run")
    parser.add_argument("--offset", type=int, default=0, help="starting record offset")
    parser.add_argument("--workers", type=int, default=1, help="concurrent generations")
    parser.add_argument("--no-resume", action="store_true", help="do not skip record_ids already in --out")
    args = parser.parse_args()

    args._replay = {}
    if args.backend == "replay":
        if not args.replay_file:
            parser.error("--replay-file is required for --backend replay")
        args._replay = {str(k): v for k, v in json.loads(args.replay_file.read_text(encoding="utf-8")).items()}

    records = load_records(args.input)[args.offset:]
    if args.limit is not None:
        records = records[: args.limit]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    skip = set() if args.no_resume else done_record_ids(args.out)
    pending = [(args.offset + i, r) for i, r in enumerate(records) if record_id_for(r, args.offset + i) not in skip]
    if args.backend == "replay":
        # Only generate records we actually authored a response for; ignore id gaps/extra.
        pending = [(i, r) for (i, r) in pending if str(record_id_for(r, i)) in args._replay]

    client = build_client(args)
    if args.backend == "reference":
        model_label = "reference(dataset)"
    elif args.backend == "replay":
        model_label = args.model or "claude(replay)"
    else:
        model_label = getattr(client, "model", "?")
    print(
        f"[generate_hls_nl_llm] input={args.input} records={len(records)} "
        f"already_done={len(skip)} to_generate={len(pending)} backend={args.backend} "
        f"model={model_label} -> {args.out}"
    )

    write_lock = threading.Lock()
    counts = {"ok": 0, "fail": 0}
    started = time.monotonic()

    def emit(row: dict) -> None:
        with write_lock, args.out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        counts["ok" if row.get("ok") else "fail"] += 1
        total = counts["ok"] + counts["fail"]
        if total % 25 == 0 or total == len(pending):
            rate = total / max(1e-9, time.monotonic() - started)
            print(f"  {total}/{len(pending)} ok={counts['ok']} fail={counts['fail']} ({rate:.1f}/s)")

    if args.workers <= 1:
        for index, record in pending:
            emit(generate_one(client, args, record, index))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(generate_one, client, args, record, index) for index, record in pending]
            for fut in as_completed(futures):
                emit(fut.result())

    print(f"[generate_hls_nl_llm] done: ok={counts['ok']} fail={counts['fail']} appended to {args.out}")
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
