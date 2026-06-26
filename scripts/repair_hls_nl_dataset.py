#!/usr/bin/env python3
"""Repair and normalize HLS_NL-style prompt->HLSC datasets.

This script intentionally performs only auditable, mostly mechanical repairs by
default. It does not claim semantic correctness. Records that need semantic
regeneration or tool verification are quarantined with reasons.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


FUNCTION_RE = re.compile(
    r"(?:^|\n)\s*(?:template\s*<[^>]+>\s*)?"
    r"(?:void|int|unsigned|float|double|bool|ap_\w+<[^>]+>|u?int\d+_t|long|short|char)"
    r"(?:\s+[\w:<>,&*]+)*\s+([A-Za-z_]\w*)\s*\(",
    re.S,
)
PRAGMA_RE = re.compile(r"#pragma\s+HLS\s+([A-Za-z_]\w*)")
INCLUDE_RE = re.compile(r"^\s*#include\s*[<\"]([^>\"]+)[>\"]\s*$", re.M)
UNBOUNDED_LOOP_RE = re.compile(r"for\s*\(\s*;\s*;\s*\)|while\s*\(\s*(?:1|true)\s*\)", re.I)


@dataclass
class RepairResult:
    status: str
    code: str
    top_function: str | None
    repairs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    quarantine_reasons: list[str] = field(default_factory=list)
    code_features: dict[str, Any] = field(default_factory=dict)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_design_title(prompt: str) -> str | None:
    match = re.search(r"\*\*Design Task:\*\*\s*([^\n]+)", prompt)
    if match:
        return match.group(1).strip()
    match = re.search(r"Design Task:\s*([^\n]+)", prompt)
    return match.group(1).strip() if match else None


def strip_markdown_fence(code: str) -> tuple[str, list[str]]:
    repairs: list[str] = []
    stripped = code.strip()
    fence = re.match(r"^```(?:c|cpp|c\+\+)?\s*(.*?)\s*```$", stripped, re.S | re.I)
    if fence:
        repairs.append("removed_markdown_code_fence")
        return fence.group(1).strip() + "\n", repairs
    return code, repairs


def normalize_includes(code: str) -> tuple[str, list[str], list[str]]:
    repairs: list[str] = []
    warnings: list[str] = []
    lines = code.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    uses_hls_stream = "hls::stream" in code
    uses_ap_fixed = bool(re.search(r"\bap_[ui]?fixed\b", code))
    removed_unused_stream = False
    for line in lines:
        include = re.match(r"\s*#include\s*[<\"]([^>\"]+)[>\"]\s*", line)
        if include:
            header = include.group(1)
            if header == "hls_stream.h" and not uses_hls_stream:
                removed_unused_stream = True
                continue
            if header in seen:
                repairs.append(f"removed_duplicate_include:{header}")
                continue
            seen.add(header)
        out.append(line.rstrip())
    if removed_unused_stream:
        repairs.append("removed_unused_hls_stream_include")
    if uses_ap_fixed and "ap_fixed.h" not in seen:
        insert_at = 0
        while insert_at < len(out) and out[insert_at].startswith("#include"):
            insert_at += 1
        out.insert(insert_at, "#include <ap_fixed.h>")
        repairs.append("added_missing_ap_fixed_include")
    if "ap_uint<" in code or "ap_int<" in code:
        if "ap_int.h" not in seen and not any("#include <ap_int.h>" in line for line in out):
            out.insert(0, "#include <ap_int.h>")
            repairs.append("added_missing_ap_int_include")
    return "\n".join(out).strip() + "\n", repairs, warnings


def detect_features(code: str) -> dict[str, Any]:
    includes = INCLUDE_RE.findall(code)
    pragmas = PRAGMA_RE.findall(code)
    return {
        "line_count": len(code.splitlines()),
        "includes": includes,
        "hls_pragmas": pragmas,
        "uses_hls_stream": "hls::stream" in code,
        "has_static_state": bool(re.search(r"\bstatic\b", code)),
        "has_main": bool(re.search(r"\bint\s+main\s*\(", code)),
        "has_testbench_language": bool(re.search(r"testbench|test_vector|assert\s*\(", code, re.I)),
        "has_std_container_or_io": bool(re.search(r"\bstd::|using namespace std|#include\s*<(vector|map|queue|string|iostream)>", code)),
        "has_dynamic_allocation": bool(re.search(r"\bmalloc\s*\(|\bfree\s*\(|\bnew\s+|\bdelete\b", code)),
        "has_non_synth_io": bool(re.search(r"\bprintf\s*\(|std::cout|std::cerr|\bscanf\s*\(|\bfopen\s*\(", code)),
        "has_unbounded_loop": bool(UNBOUNDED_LOOP_RE.search(code)),
        "has_placeholder": bool(re.search(r"TODO|FIXME|your code|implementation here|placeholder|\.\.\.", code, re.I)),
        "brace_balance": code.count("{") - code.count("}"),
    }


def extract_top_function(code: str) -> str | None:
    match = FUNCTION_RE.search(code)
    return match.group(1) if match else None


def likely_semantic_warnings(prompt: str, code: str) -> list[str]:
    warnings: list[str] = []
    if re.search(r"Priority Encoder", prompt, re.I):
        if re.search(r"if\s*\(\s*I3\s*\).*?O1\s*=\s*1\s*;[^\n]*index\s+3", code, re.S | re.I):
            warnings.append("possible_priority_encoder_index_bug:I3_sets_O1_to_1_but_prompt_describes_index_3")
    if re.search(r"division by zero.*returning 0", prompt, re.I):
        if re.search(r"/\s*b\b", code) and not re.search(r"b\s*!=\s*0|b\s*>\s*0|\?\s*[^:]*:", code):
            warnings.append("possible_division_without_zero_guard")
    if re.search(r"rising edge|clock cycle", prompt, re.I) and re.search(r"\bbool\s+clock\b|\bap_uint<1>\s+clock\b", code):
        if re.search(r"if\s*\(\s*clock\s*\)", code):
            warnings.append("clock_modeled_as_level_sensitive_input_review_cycle_semantics")
    return warnings


def repair_record(record: dict[str, Any], index: int) -> RepairResult:
    if record.get("status") == "deleted" and "contains_unbounded_infinite_loop" in (record.get("quarantine_reasons") or []):
        features = dict(record.get("code_features") or {})
        features["has_unbounded_loop"] = True
        return RepairResult(
            "deleted",
            "",
            record.get("top_function"),
            [],
            [str(item) for item in record.get("warnings", [])],
            [str(item) for item in record.get("quarantine_reasons", [])],
            features,
        )
    original_code = str(record.get("hls_cpp", ""))
    code = original_code.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    repairs: list[str] = []
    warnings: list[str] = []
    quarantine: list[str] = []
    if code != original_code:
        repairs.append("normalized_line_endings_or_bom")
    code, fence_repairs = strip_markdown_fence(code)
    repairs.extend(fence_repairs)
    code, include_repairs, include_warnings = normalize_includes(code)
    repairs.extend(include_repairs)
    warnings.extend(include_warnings)
    features = detect_features(code)
    top = extract_top_function(code)
    if not top:
        quarantine.append("missing_parseable_top_function")
    if features["brace_balance"] != 0:
        quarantine.append("unbalanced_braces")
    if features["has_main"]:
        quarantine.append("contains_main_or_test_program")
    if features["has_testbench_language"]:
        quarantine.append("appears_to_be_testbench_not_hlsc_top")
    if features["has_std_container_or_io"]:
        quarantine.append("uses_stl_or_iostream_review_synthesizability")
    if features["has_dynamic_allocation"]:
        quarantine.append("uses_dynamic_allocation_or_delete")
    if features["has_non_synth_io"]:
        quarantine.append("uses_non_synthesizable_io")
    if features["has_unbounded_loop"]:
        quarantine.append("contains_unbounded_infinite_loop")
    if features["has_placeholder"]:
        quarantine.append("contains_placeholder_or_ellipsis")
    warnings.extend(likely_semantic_warnings(str(record.get("HLS_instruction", "")), code))
    status = "deleted" if features["has_unbounded_loop"] else ("quarantine" if quarantine else "accepted")
    return RepairResult(status, code, top, repairs, warnings, quarantine, features)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair HLS_NL prompt-to-HLSC dataset with auditable mechanical rules.")
    parser.add_argument("--input", required=True, help="Path to HLS_NL.json")
    parser.add_argument("--out-dir", required=True, help="Output directory for repaired JSONL and reports")
    parser.add_argument("--assume-prior-cosim-pass", action="store_true", help="Record user-provided prior cosim provenance in metadata")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("Input JSON root must be a list")

    accepted: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    counters = collections.Counter()
    warning_counts = collections.Counter()
    repair_counts = collections.Counter()
    quarantine_counts = collections.Counter()
    top_counts = collections.Counter()
    file_counts = collections.Counter(str(rec.get("file", "")) for rec in data)

    for idx, rec in enumerate(data):
        result = repair_record(rec, idx)
        for item in result.repairs:
            repair_counts[item] += 1
        for item in result.warnings:
            warning_counts[item] += 1
        for item in result.quarantine_reasons:
            quarantine_counts[item] += 1
        if result.top_function:
            top_counts[result.top_function] += 1
        duplicate_file = file_counts[str(rec.get("file", ""))] > 1
        if duplicate_file:
            result.warnings.append("duplicate_file_name")
            warning_counts["duplicate_file_name"] += 1
        row = {
            "record_id": idx,
            "original_file": rec.get("file"),
            "design_title": extract_design_title(str(rec.get("HLS_instruction", ""))),
            "HLS_instruction": rec.get("HLS_instruction", ""),
            "hls_cpp": "" if result.status == "deleted" else result.code,
            "top_function": result.top_function,
            "status": result.status,
            "repairs": result.repairs,
            "warnings": result.warnings,
            "quarantine_reasons": result.quarantine_reasons,
            "code_features": result.code_features,
            "source_sha256": {
                "instruction": sha256_text(str(rec.get("HLS_instruction", ""))),
                "hls_cpp_original": sha256_text(str(rec.get("hls_cpp", ""))),
                "hls_cpp_repaired": sha256_text(result.code),
                "hls_cpp_deleted_body": sha256_text(result.code) if result.status == "deleted" else None,
            },
            "verification_provenance": {
                "prior_cosim_pass_user_reported": bool(args.assume_prior_cosim_pass),
                "current_script_tool_verified": False,
            },
        }
        all_records.append(row)
        if result.status == "accepted":
            accepted.append(row)
        elif result.status == "deleted":
            deleted.append(row)
        else:
            quarantine.append(row)
        counters[result.status] += 1

    write_jsonl(out_dir / "hls_nl_repaired.all.jsonl", all_records)
    write_jsonl(out_dir / "hls_nl_repaired.accepted.jsonl", accepted)
    write_jsonl(out_dir / "hls_nl_repaired.quarantine.jsonl", quarantine)
    write_jsonl(out_dir / "hls_nl_repaired.deleted.jsonl", deleted)
    sft_records = [
        {
            "messages": [
                {
                    "role": "system",
                    "content": "You generate conservative synthesizable Vitis HLS C/C++ from a hardware-oriented functional specification. Preserve functional behavior before optimizing.",
                },
                {"role": "user", "content": row["HLS_instruction"]},
                {"role": "assistant", "content": row["hls_cpp"]},
            ],
            "metadata": {
                "source": row["original_file"],
                "top_function": row["top_function"],
                "design_title": row["design_title"],
                "verification_provenance": row["verification_provenance"],
            },
        }
        for row in accepted
    ]
    write_jsonl(out_dir / "hls_nl_sft.accepted.jsonl", sft_records)

    report = {
        "input": str(input_path),
        "count": len(data),
        "status_counts": dict(counters),
        "repair_counts": dict(repair_counts),
        "warning_counts": dict(warning_counts),
        "quarantine_reason_counts": dict(quarantine_counts),
        "top_function_top20": top_counts.most_common(20),
        "outputs": {
            "all": str(out_dir / "hls_nl_repaired.all.jsonl"),
            "accepted": str(out_dir / "hls_nl_repaired.accepted.jsonl"),
            "quarantine": str(out_dir / "hls_nl_repaired.quarantine.jsonl"),
            "deleted": str(out_dir / "hls_nl_repaired.deleted.jsonl"),
            "sft": str(out_dir / "hls_nl_sft.accepted.jsonl"),
        },
        "notes": [
            "Repairs are mechanical and do not prove functional correctness.",
            "Quarantined records should be regenerated or tool-verified before training.",
            "Deleted records keep code-free metadata and hashes only; their HLS-C bodies are intentionally omitted.",
            "Use accepted SFT records only as auxiliary HLSC-style data unless Vitis verification metadata is added.",
        ],
    }
    (out_dir / "repair_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_lines = [
        "# HLS_NL Repair Report",
        "",
        f"- Input: `{input_path}`",
        f"- Records: {len(data)}",
        f"- Accepted: {len(accepted)}",
        f"- Quarantined: {len(quarantine)}",
        f"- Deleted: {len(deleted)}",
        "",
        "## Repair Counts",
        "",
    ]
    md_lines.extend(f"- {k}: {v}" for k, v in repair_counts.most_common())
    md_lines.extend(["", "## Warning Counts", ""])
    md_lines.extend(f"- {k}: {v}" for k, v in warning_counts.most_common())
    md_lines.extend(["", "## Quarantine Reasons", ""])
    md_lines.extend(f"- {k}: {v}" for k, v in quarantine_counts.most_common())
    md_lines.extend(["", "## Top Functions", ""])
    md_lines.extend(f"- {k}: {v}" for k, v in top_counts.most_common(20))
    md_lines.extend(
        [
            "",
            "## Outputs",
            "",
            f"- All records: `{out_dir / 'hls_nl_repaired.all.jsonl'}`",
            f"- Accepted records: `{out_dir / 'hls_nl_repaired.accepted.jsonl'}`",
            f"- Quarantine records: `{out_dir / 'hls_nl_repaired.quarantine.jsonl'}`",
            f"- Deleted records: `{out_dir / 'hls_nl_repaired.deleted.jsonl'}`",
            f"- Accepted SFT chat records: `{out_dir / 'hls_nl_sft.accepted.jsonl'}`",
            "",
            "Current script verification: false. This pass preserves user-reported prior cosim provenance separately from current tool verification.",
        ]
    )
    (out_dir / "repair_report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
