"""Cross-reference dual-generation differential oracle over HLS_NL records.

Implements the paper-figure workflow ("C-to-HLS-C Cross-Reference Dual-Generation"):
two INDEPENDENT LLM generations from the same NL spec — no shared context, different
prompt framings — are normalized, compiled into isolated namespaces in SEPARATE
translation units, driven with byte-identical seeded stimulus, and compared output-
for-output. Verdicts:

- ``cross_verified``  both arms parse, compile, and agree on every driven vector
- ``divergent``       outputs differ (or the arms' signatures do not match)
- ``unavailable``     the host oracle cannot faithfully execute the design
                      (hls::stream / ap_* types, unsized shapes, compile failure)
- ``unparseable``     an arm produced no plausible definition of the top function

Isolation facts this module relies on:
- ``ClaudeCLIClient.complete()`` spawns a fresh ``claude -p`` subprocess per call with
  no session flags, so one client serving both arms shares nothing between them —
  isolation is structural, per-call.
- Each arm is its own translation unit (macros in 39% of corpus bodies CANNOT leak
  across arms; ``#define`` ignores namespace boundaries but not TU boundaries).
  ``extern "C"`` wrappers are stripped (recorded) — C linkage inside two namespaces
  would collide at link time.
- The dataset's reference ``hls_cpp`` is NEVER shown to either arm: arms see only
  NL-derived prompts. (It is also not used by the oracle — the arms check each other.)

Output protocol (crash-safe resume): ``results.jsonl`` is the ONLY append stream and
the commit marker; it embeds both arms' sources. ``cross_referenced_corpus.jsonl``
(schema-compatible with the accepted dataset, so the existing testbench/Vitis batch
flow consumes it unchanged) and ``needs_review.jsonl`` are regenerated wholesale from
results.jsonl at end of run. Rows with ``infra_error`` (LLM backend failure/timeout)
are excluded from the resume set so a rerun retries them. Concurrent shards must use
separate --out dirs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .equivalence import parse_mismatches
from .llm import (
    LLMClient,
    build_llm_client,
    extract_full_file,
    is_plausible_translation_unit,
    missing_llm_reason,
)
from .nl_records import (
    Arg,
    FunctionSig,
    extract_named_function,
    find_matching,
    load_records,
    record_design_title,
    record_id_for,
    record_source_file,
)
from .testgen import _LENGTH_NAMES, CPP_STIMULUS_HELPERS

RESULTS_FILENAME = "results.jsonl"
CORPUS_FILENAME = "cross_referenced_corpus.jsonl"
NEEDS_REVIEW_FILENAME = "needs_review.jsonl"
FRAMING_A = "verbatim_instruction"
FRAMING_B = "restructured_spec"
_WORK_DIRNAME = ".xref"  # sibling of best-of-N's .candidates, never shared
_DEFAULT_BUFFER = 16  # existing NL-testbench precedent for unsized pointer buffers
_MISMATCH_CAP = 10
_COMPILE_TIMEOUT = 120
_RUN_TIMEOUT = 60

XREF_B_SYSTEM = """You are an HLS C/C++ implementer. You receive a hardware design specification.
Implement it as ONE complete, self-contained, synthesizable C/C++ file.

Rules:
- Define exactly the externally visible function named in the task (local static
  helpers are fine).
- No testbench, no main(), no I/O, no dynamic allocation.
- Output ONLY the file, in a single ```cpp fenced block — no analysis sections, no
  commentary.
"""

_BOILERPLATE_CUT = re.compile(r"(\*\*Design Task:\*\*|Design Task:)")


def build_framing_a(record: dict[str, Any]) -> tuple[str, str]:
    """Arm A: the dataset's HLS_instruction verbatim (it is a complete prompt)."""

    return "", str(record.get("HLS_instruction", ""))


def build_framing_b(record: dict[str, Any]) -> tuple[str, str]:
    """Arm B: restructured spec — boilerplate preamble cut, task restated, different
    output contract. Same semantics, different framing, zero shared context."""

    instruction = str(record.get("HLS_instruction", ""))
    top = str(record.get("top_function", ""))
    title = record_design_title(record) or "(untitled design)"
    match = _BOILERPLATE_CUT.search(instruction)
    body = instruction[match.start() :] if match else instruction
    user = f"""Specification title: {title}

Specification (implement from this alone):
\"\"\"
{body.strip()}
\"\"\"

Implement exactly one externally visible function named `{top}`.
Output ONLY the complete C/C++ file in a single ```cpp fenced block."""
    return XREF_B_SYSTEM, user


_EXTERN_C_BLOCK = re.compile(r'extern\s+"C"\s*\{')


def strip_extern_c(code: str) -> tuple[str, bool]:
    """Remove ``extern "C"`` linkage wrappers (block and single-declaration forms).

    C-linkage symbols ignore namespaces, so two arms both wrapping the top in
    ``extern "C"`` would collide at link time despite the namespace isolation.
    """

    stripped = False
    while True:
        match = _EXTERN_C_BLOCK.search(code)
        if not match:
            break
        open_idx = code.find("{", match.start())
        close_idx = find_matching(code, open_idx, "{", "}")
        if close_idx < 0:
            break
        code = code[: match.start()] + code[open_idx + 1 : close_idx] + code[close_idx + 1 :]
        stripped = True
    without_prefix = re.sub(r'extern\s+"C"\s+', "", code)
    if without_prefix != code:
        stripped = True
    return without_prefix, stripped


def normalize_signature(sig: FunctionSig) -> str:
    """Type-shape signature for cross-arm comparison: argument NAMES may differ
    between two correct implementations; types, dims, and return type may not."""

    parts = []
    for arg in sig.args:
        dims = "".join(re.findall(r"\[[^\]]*\]", arg.raw))
        parts.append(re.sub(r"\s+", " ", f"{arg.c_type}{dims}").strip())
    ret = re.sub(r"\s+", " ", sig.return_type).strip()
    return f"{ret}({', '.join(parts)})"


def _arg_dims(arg: Arg) -> list[int]:
    return [int(d) for d in re.findall(r"\[(\d+)\]", arg.raw)]


def _is_array_like(arg: Arg) -> bool:
    return "*" in arg.c_type or bool(re.search(r"\[[^\]]*\]", arg.raw))


def _is_unsigned_base(base: str) -> str:
    unsigned = "unsigned" in base or base.startswith("uint") or base == "bool" or base == "char"
    return "true" if unsigned else "false"


def executability_reason(sig: FunctionSig) -> str | None:
    """Host-oracle limits -> ``unavailable`` reason. hls/ap gates run BEFORE the
    integer-type check (``is_integer_type`` matches ap_int too)."""

    def type_reason(text: str) -> str | None:
        if "hls::stream" in text:
            return "hls_stream_arg"
        if "ap_fixed" in text or "ap_ufixed" in text:
            return "ap_fixed_arg"
        if "ap_int" in text or "ap_uint" in text:
            return "ap_int_arg"
        return None

    reason = type_reason(sig.return_type)
    if reason:
        return reason
    ret = re.sub(r"\s+", " ", sig.return_type).strip()
    if ret not in {"void"} and not re.search(
        r"\b(u?int\d+_t|int|unsigned|long|short|char|bool|float|double)\b", ret
    ):
        return "unknown_return_type"
    for arg in sig.args:
        reason = type_reason(arg.c_type)
        if reason:
            return reason
        if arg.c_type.count("*") > 1:
            return "nested_pointer_arg"
        if len(_arg_dims(arg)) > 1:
            return "multi_dim_array"
        base = arg.base_type
        if not re.search(r"\b(u?int\d+_t|int|unsigned|long|short|char|bool|float|double)\b", base):
            return "unknown_type_arg"
    return None


def _buffer_len(arg: Arg) -> int:
    dims = _arg_dims(arg)
    return dims[0] if dims else _DEFAULT_BUFFER


def _length_scalar(sig: FunctionSig) -> Arg | None:
    """The single length-like value scalar, if any (drives the A7 clamp)."""

    candidates = [
        arg
        for arg in sig.args
        if not _is_array_like(arg)
        and "&" not in arg.c_type
        and arg.name.lower() in _LENGTH_NAMES
    ]
    return candidates[0] if len(candidates) == 1 else None


def build_arm_tu(code: str, namespace: str) -> str:
    """One arm as its own translation unit: includes/pragmas lifted above the
    namespace (they cannot live inside one), everything else — including #define
    lines — confined inside it. Macros do not respect namespaces, but they DO
    respect translation-unit boundaries, which is why each arm gets its own TU."""

    lifted: list[str] = []
    body: list[str] = []
    for line in code.splitlines():
        if line.strip().startswith(("#include", "#pragma")):
            lifted.append(line)
        else:
            body.append(line)
    header = "\n".join(lifted)
    inner = "\n".join(body).strip("\n")
    return f"{header}\n\nnamespace {namespace} {{\n\n{inner}\n\n}}  // namespace {namespace}\n"


def build_harness(sig: FunctionSig, sig_b: FunctionSig, seed: int, n_vectors: int) -> str:
    """The shared-stimulus differential main(): identical inputs to both arms,
    sentinel-prefilled outputs, log-and-continue compares in the exact Mismatch
    grammar ``parse_mismatches`` reads."""

    length_arg = _length_scalar(sig)
    min_buffer = min((_buffer_len(a) for a in sig.args if _is_array_like(a)), default=_DEFAULT_BUFFER)

    decls: list[str] = []
    a_args: list[str] = []
    b_args: list[str] = []
    compares: list[str] = []

    for arg in sig.args:
        base = arg.base_type
        unsigned = _is_unsigned_base(base)
        if _is_array_like(arg):
            n = _buffer_len(arg)
            if arg.is_const or arg.direction == "input":
                decls.append(f"    {base} a_{arg.name}[{n}], b_{arg.name}[{n}];")
                decls.append(
                    f"    for (int i = 0; i < {n}; ++i) {{ {base} v = patterned_value<{base}>(test_idx, i, rng, {unsigned}); "
                    f"a_{arg.name}[i] = v; b_{arg.name}[i] = v; }}"
                )
            else:
                decls.append(f"    {base} a_{arg.name}[{n}], b_{arg.name}[{n}];")
                decls.append(
                    f"    for (int i = 0; i < {n}; ++i) {{ {base} s = output_sentinel<{base}>(test_idx, i); "
                    f"a_{arg.name}[i] = s; b_{arg.name}[i] = s; }}"
                )
                clamp = (
                    f"clamp_count(static_cast<long long>({length_arg.name}), {n})"
                    if length_arg is not None
                    else str(n)
                )
                compares.append(
                    f"""    {{
      const int compare_len = {clamp};
      for (int i = 0; i < compare_len; ++i) {{
        if (!values_equal(a_{arg.name}[i], b_{arg.name}[i])) {{
          ++mismatch_count;
          if (mismatch_count <= {_MISMATCH_CAP})
            std::cerr << "Mismatch test=" << test_idx << " arg={arg.name} index=" << i
                      << " expected=" << +a_{arg.name}[i] << " actual=" << +b_{arg.name}[i]
                      << " seed={seed}" << "\\n";
        }}
      }}
    }}"""
                )
            a_args.append(f"a_{arg.name}")
            b_args.append(f"b_{arg.name}")
        elif "&" in arg.c_type:
            decls.append(f"    {base} a_{arg.name}, b_{arg.name};")
            decls.append(
                f"    {{ {base} s = output_sentinel<{base}>(test_idx, 0); a_{arg.name} = s; b_{arg.name} = s; }}"
            )
            compares.append(
                f"""    if (!values_equal(a_{arg.name}, b_{arg.name})) {{
      ++mismatch_count;
      if (mismatch_count <= {_MISMATCH_CAP})
        std::cerr << "Mismatch test=" << test_idx << " arg={arg.name} index=0"
                  << " expected=" << +a_{arg.name} << " actual=" << +b_{arg.name}
                  << " seed={seed}" << "\\n";
    }}"""
            )
            a_args.append(f"a_{arg.name}")
            b_args.append(f"b_{arg.name}")
        else:
            if length_arg is not None and arg.name == length_arg.name:
                # A7: length-like scalars are clamped to the buffer capacity for BOTH
                # arms — without this, pattern test 1 drives ~0ULL over 16-element
                # buffers and OOB reads produce false divergence.
                decls.append(
                    f"    {arg.base_type} {arg.name} = static_cast<{arg.base_type}>("
                    f"bounded_scalar<long long>(test_idx, rng, 1, {min_buffer}));"
                )
            else:
                decls.append(
                    f"    {arg.base_type} {arg.name} = patterned_value<{arg.base_type}>(test_idx, 0, rng, {unsigned});"
                )
            a_args.append(arg.name)
            b_args.append(arg.name)

    ret = re.sub(r"\s+", " ", sig.return_type).strip()
    call_a = f"xref_a::{sig.name}({', '.join(a_args)})"
    call_b = f"xref_b::{sig.name}({', '.join(b_args)})"
    if ret == "void":
        calls = f"    {call_a};\n    {call_b};"
        return_compare = ""
    else:
        calls = f"    {ret} a_ret = {call_a};\n    {ret} b_ret = {call_b};"
        return_compare = f"""    if (!values_equal(a_ret, b_ret)) {{
      ++mismatch_count;
      if (mismatch_count <= {_MISMATCH_CAP})
        std::cerr << "Mismatch test=" << test_idx << " return expected=" << +a_ret
                  << " actual=" << +b_ret << " seed={seed}" << "\\n";
    }}"""

    proto_a = ", ".join(arg.raw for arg in sig.args)
    proto_b = ", ".join(arg.raw for arg in sig_b.args)
    newline = "\n"
    return f"""// Generated by c2hlsc_agent cross_reference. Differential harness (A vs B).
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <random>

namespace xref_a {{ {sig.return_type} {sig.name}({proto_a}); }}
namespace xref_b {{ {sig_b.return_type} {sig_b.name}({proto_b}); }}

{CPP_STIMULUS_HELPERS}

int main() {{
  std::mt19937_64 rng({seed}ULL);
  long long mismatch_count = 0;
  for (int test_idx = 0; test_idx < {n_vectors}; ++test_idx) {{
{newline.join(decls)}

{calls}
{return_compare}
{newline.join(compares)}
  }}
  if (mismatch_count) {{
    std::cerr << "xref: " << mismatch_count << " mismatching value(s), seed={seed}\\n";
    return 1;
  }}
  std::cout << "xref: all {n_vectors} vectors agree, seed={seed}\\n";
  return 0;
}}
"""


@dataclass
class ArmOutcome:
    framing: str
    ok: bool = False
    infra_error: bool = False
    error: str | None = None
    instruction_sha256: str = ""
    model: str | None = None
    hls_cpp: str | None = None
    parse_ok: bool = False
    signature: str | None = None
    extern_c_stripped: bool = False
    sig: FunctionSig | None = None  # not serialized

    def to_dict(self) -> dict[str, Any]:
        return {
            "framing": self.framing,
            "ok": self.ok,
            "error": self.error,
            "instruction_sha256": self.instruction_sha256,
            "model": self.model,
            "parse_ok": self.parse_ok,
            "signature": self.signature,
            "extern_c_stripped": self.extern_c_stripped,
            "hls_cpp": self.hls_cpp,
        }


def run_arm(llm: LLMClient, framing: str, system: str, user: str, top: str) -> ArmOutcome:
    outcome = ArmOutcome(
        framing=framing,
        model=getattr(llm, "model", None),
        instruction_sha256=hashlib.sha256(f"{system}\n\n{user}".encode("utf-8")).hexdigest(),
    )
    try:
        response = llm.complete(system, user)
    except Exception as exc:  # backend down / CLI timeout: infra, not a verdict (A11)
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.infra_error = True
        return outcome
    outcome.ok = True
    code = extract_full_file(response, must_contain=f"{top}(")
    if not code or not is_plausible_translation_unit(code, top):
        outcome.error = "no_parseable_top_function"
        return outcome
    code, stripped = strip_extern_c(code)
    outcome.extern_c_stripped = stripped
    sig = extract_named_function(code, top)
    if sig is None:
        outcome.error = "top_function_not_found"
        return outcome
    outcome.hls_cpp = code
    outcome.parse_ok = True
    outcome.sig = sig
    outcome.signature = normalize_signature(sig)
    return outcome


def _compile_and_run(
    workdir: Path, a_tu: str, b_tu: str, harness: str
) -> tuple[str, str, str]:
    """Returns (status, detail, output). status: ok | compile_fail_a|b|harness |
    link_fail | run_timeout | run_fail. Per-TU compiles attribute failures to an arm."""

    workdir.mkdir(parents=True, exist_ok=True)
    cxx = os.environ.get("CXX", "g++")
    sources = {"a": a_tu, "b": b_tu, "harness": harness}
    objects: list[str] = []
    for tag, text in sources.items():
        src = workdir / f"{tag}.cpp"
        src.write_text(text, encoding="utf-8")
        obj = workdir / f"{tag}.o"
        try:
            proc = subprocess.run(
                [cxx, "-std=c++17", "-O1", "-c", str(src), "-o", str(obj)],
                capture_output=True,
                text=True,
                timeout=_COMPILE_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"compile_fail_{tag}", str(exc), ""
        if proc.returncode != 0:
            return f"compile_fail_{tag}", proc.stderr[-2000:], ""
        objects.append(str(obj))
    binary = workdir / "xref"
    try:
        proc = subprocess.run(
            [cxx, *objects, "-o", str(binary)], capture_output=True, text=True, timeout=_COMPILE_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "link_fail", str(exc), ""
    if proc.returncode != 0:
        return "link_fail", proc.stderr[-2000:], ""
    try:
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "run_timeout", f"harness exceeded {_RUN_TIMEOUT}s", ""
    output = (run.stdout or "") + (run.stderr or "")
    if run.returncode == 0:
        return "ok", "", output
    return "run_fail", f"exit {run.returncode}", output


def process_record(
    record: dict[str, Any],
    record_id: int,
    llm: LLMClient,
    seed: int,
    n_vectors: int,
    workroot: Path,
) -> dict[str, Any]:
    top = str(record.get("top_function", "")).strip()
    row: dict[str, Any] = {
        "record_id": record_id,
        "top_function": top,
        "design_title": record_design_title(record),
        "original_file": record_source_file(record),
        "stimulus": {"seed": seed, "n_vectors": n_vectors},
        "oracle": {"compiler": os.environ.get("CXX", "g++"), "namespaces": ["xref_a", "xref_b"]},
        "mismatches": [],
    }
    if not top:
        row.update(classification="unparseable", reason="missing_top_function")
        row["arm_a"] = row["arm_b"] = None
        return row

    system_a, user_a = build_framing_a(record)
    system_b, user_b = build_framing_b(record)
    arm_a = run_arm(llm, FRAMING_A, system_a, user_a, top)
    arm_b = (
        run_arm(llm, FRAMING_B, system_b, user_b, top)
        if not arm_a.infra_error
        else ArmOutcome(framing=FRAMING_B, error="skipped: arm A infra error")
    )
    row["arm_a"] = arm_a.to_dict()
    row["arm_b"] = arm_b.to_dict()

    if arm_a.infra_error or arm_b.infra_error:
        row.update(classification="unavailable", reason="llm_backend_error", infra_error=True)
        return row
    if not arm_a.parse_ok or not arm_b.parse_ok:
        row.update(
            classification="unparseable",
            reason=arm_a.error if not arm_a.parse_ok else arm_b.error,
        )
        return row
    assert arm_a.sig is not None and arm_b.sig is not None
    if arm_a.signature != arm_b.signature:
        row.update(classification="divergent", reason="signature_mismatch")
        return row
    reason = executability_reason(arm_a.sig)
    if reason:
        row.update(classification="unavailable", reason=reason)
        return row

    length_arg = _length_scalar(arm_a.sig)
    if length_arg is not None:
        row["stimulus"]["clamped_length_args"] = [length_arg.name]
    a_tu = build_arm_tu(arm_a.hls_cpp or "", "xref_a")
    b_tu = build_arm_tu(arm_b.hls_cpp or "", "xref_b")
    harness = build_harness(arm_a.sig, arm_b.sig, seed, n_vectors)
    status, detail, output = _compile_and_run(workroot / str(record_id), a_tu, b_tu, harness)
    if status == "ok":
        row.update(classification="cross_verified", reason="")
        return row
    if status in {"compile_fail_a", "compile_fail_b", "compile_fail_harness", "link_fail", "run_timeout"}:
        row.update(classification="unavailable", reason=status, detail=detail[-1000:])
        return row
    # run_fail: mismatch lines => divergent; anything else is a harness defect.
    mismatches = parse_mismatches(output)
    if mismatches:
        row["mismatches"] = [m.to_dict() for m in mismatches[:_MISMATCH_CAP]]
        row.update(classification="divergent", reason="output_mismatch")
        return row
    row.update(classification="unavailable", reason="harness_error", detail=(detail + "\n" + output)[-1000:])
    return row


def corpus_row(row: dict[str, Any], record: dict[str, Any], timestamp: str) -> dict[str, Any]:
    """cross_verified results as accepted-dataset-compatible corpus rows: arm A is
    canonical, provenance pins both arms' hashes and the stimulus."""

    arm_a = row.get("arm_a") or {}
    arm_b = row.get("arm_b") or {}
    return {
        "record_id": row["record_id"],
        "HLS_instruction": record.get("HLS_instruction", ""),
        "hls_cpp": arm_a.get("hls_cpp", ""),
        "canonical_arm": "a",
        "top_function": row["top_function"],
        "design_title": row.get("design_title"),
        "original_file": row.get("original_file"),
        "status": "cross_verified",
        "verification_provenance": {
            "cross_reference": {
                "arm_a_sha256": hashlib.sha256((arm_a.get("hls_cpp") or "").encode("utf-8")).hexdigest(),
                "arm_b_sha256": hashlib.sha256((arm_b.get("hls_cpp") or "").encode("utf-8")).hexdigest(),
                "arm_a_instruction_sha256": arm_a.get("instruction_sha256"),
                "arm_b_instruction_sha256": arm_b.get("instruction_sha256"),
                "seed": row.get("stimulus", {}).get("seed"),
                "n_vectors": row.get("stimulus", {}).get("n_vectors"),
                "classified": "cross_verified",
                "date": timestamp,
            }
        },
    }


def _read_results(results_path: Path) -> list[dict[str, Any]]:
    """All parseable rows, LAST row per record_id winning (retries append)."""

    if not results_path.exists():
        return []
    by_id: dict[int, dict[str, Any]] = {}
    for line in results_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:  # torn final line from a killed writer
            continue
        if isinstance(row, dict) and "record_id" in row:
            by_id[int(row["record_id"])] = row
    return [by_id[key] for key in sorted(by_id)]


def done_record_ids(results_path: Path) -> set[int]:
    """Resume set: completed rows only — infra_error rows are retried (A11)."""

    return {
        int(row["record_id"]) for row in _read_results(results_path) if not row.get("infra_error")
    }


def regenerate_derived(out_dir: Path, records_by_id: dict[int, dict[str, Any]], timestamp: str) -> tuple[int, int]:
    """Idempotent wholesale rewrite of corpus + needs_review from results.jsonl."""

    rows = _read_results(out_dir / RESULTS_FILENAME)
    corpus_lines: list[str] = []
    review_lines: list[str] = []
    for row in rows:
        if row.get("classification") == "cross_verified":
            record = records_by_id.get(int(row["record_id"]), {})
            corpus_lines.append(json.dumps(corpus_row(row, record, timestamp), sort_keys=True))
        else:
            review_lines.append(json.dumps(row, sort_keys=True))
    (out_dir / CORPUS_FILENAME).write_text("\n".join(corpus_lines) + ("\n" if corpus_lines else ""), encoding="utf-8")
    (out_dir / NEEDS_REVIEW_FILENAME).write_text("\n".join(review_lines) + ("\n" if review_lines else ""), encoding="utf-8")
    return len(corpus_lines), len(review_lines)


def run_cross_reference(args: Any) -> int:
    from .config import load_config, merge_cli_config

    config = merge_cli_config(load_config(None), args)
    config.use_llm = True  # inherently LLM-driven, mirror the nl_only convention
    llm = build_llm_client(config)
    if llm is None:
        raise SystemExit(f"cross-reference requires an LLM: {missing_llm_reason(config)}")

    records_path = Path(args.records).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / RESULTS_FILENAME
    seed = int(args.seed) if getattr(args, "seed", None) is not None else config.seed
    n_vectors = int(getattr(args, "num_vectors", None) or 16)

    records = load_records(records_path)
    offset = int(getattr(args, "offset", None) or 0)
    limit = getattr(args, "limit", None)
    sliced = records[offset : offset + int(limit)] if limit is not None else records[offset:]

    only_id = getattr(args, "record_id", None)
    done = done_record_ids(results_path)
    records_by_id: dict[int, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    infra = 0
    processed = 0
    for local_idx, record in enumerate(sliced):
        record_id = record_id_for(record, offset + local_idx)  # A14: offset-stable fallback
        records_by_id[record_id] = record
        if only_id is not None and record_id != int(only_id):
            continue
        if record_id in done:
            continue
        row = process_record(record, record_id, llm, seed, n_vectors, out_dir / _WORK_DIRNAME)
        with results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        processed += 1
        counts[row.get("classification", "?")] = counts.get(row.get("classification", "?"), 0) + 1
        if row.get("infra_error"):
            infra += 1
        print(
            f"[{record_id}] {row.get('classification')}"
            + (f" ({row.get('reason')})" if row.get("reason") else ""),
            flush=True,
        )

    # Full-file mapping so resumed runs regenerate rows from earlier shards too.
    for idx, record in enumerate(records):
        records_by_id.setdefault(record_id_for(record, idx), record)
    timestamp = time.strftime("%Y-%m-%d")
    corpus_count, review_count = regenerate_derived(out_dir, records_by_id, timestamp)
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nothing new"
    print(f"cross-reference: {processed} record(s) processed ({summary})")
    print(f"  {CORPUS_FILENAME}: {corpus_count} row(s); {NEEDS_REVIEW_FILENAME}: {review_count} row(s)")
    print(f"  results: {results_path}")
    if infra:
        print(f"  {infra} record(s) hit LLM backend errors and will be retried on rerun.", file=sys.stderr)
        return 1
    return 0
