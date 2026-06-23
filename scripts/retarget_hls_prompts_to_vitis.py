#!/usr/bin/env python3
"""Retarget HLS_NL prompts from generic/Vivado-style HLS wording to Vitis HLS."""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any


GENERIC_HLS_PREAMBLE = (
    "You are an HLS designer. Your task is to write synthesizable HLS-C code for me "
    "based on the functional description provided below. The code must adhere to "
    "HLS-specific constraints, including fixed-width data types, interface protocols, "
    "and performance optimizations."
)

VITIS_HLS_PREAMBLE = (
    "You are a Vitis HLS code generation assistant. Generate synthesizable Vitis HLS "
    "C/C++ for AMD/Xilinx Vitis HLS based on the functional description provided below. "
    "Use Vitis HLS-compatible fixed-width data types, interface pragmas, streaming "
    "types, and optimization directives where appropriate. Return only the HLS C/C++ "
    "implementation."
)


def replace_vivado_terms(text: str) -> tuple[str, list[str]]:
    """Replace vendor-tool wording while preserving surrounding text."""

    edits: list[str] = []
    updated, vivado_hls_count = re.compile(r"\b[Vv]ivado\s+HLS\b").subn("Vitis HLS", text)
    if vivado_hls_count:
        edits.extend(["replaced_vivado_hls_with_vitis_hls"] * vivado_hls_count)

    updated, vivado_count = re.compile(r"\b[Vv]ivado\b").subn("Vitis", updated)
    if vivado_count:
        edits.extend(["replaced_vivado_with_vitis"] * vivado_count)

    return updated, edits


def retarget_instruction(instruction: str) -> tuple[str, list[str]]:
    """Return a Vitis HLS generation prompt and the mechanical edits applied."""

    updated = instruction.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    edits: list[str] = []

    if updated != instruction:
        edits.append("normalized_instruction_line_endings_or_bom")

    if GENERIC_HLS_PREAMBLE in updated:
        updated = updated.replace(GENERIC_HLS_PREAMBLE, VITIS_HLS_PREAMBLE, 1)
        edits.append("replaced_generic_hls_preamble_with_vitis_generation_preamble")
    elif "Vitis HLS" not in updated:
        updated = f"{VITIS_HLS_PREAMBLE} {updated}"
        edits.append("prepended_vitis_generation_preamble")

    updated, vendor_edits = replace_vivado_terms(updated)
    edits.extend(vendor_edits)

    return updated, edits


def load_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("Input JSON root must be a list")
    if not all(isinstance(record, dict) for record in data):
        raise SystemExit("Input JSON records must be objects")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Path to HLS_NL.json")
    parser.add_argument("--output", required=True, help="Path for Vitis-retargeted JSON")
    parser.add_argument("--report", required=True, help="Path for prompt retargeting report JSON")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    records = load_records(input_path)
    edit_counts = collections.Counter()
    changed_records = 0
    changed_code_records = 0
    samples: list[dict[str, Any]] = []

    for idx, record in enumerate(records):
        original = str(record.get("HLS_instruction", ""))
        updated, edits = retarget_instruction(original)
        if updated != original:
            changed_records += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "record_id": idx,
                        "file": record.get("file"),
                        "edits": edits,
                        "before": original[:500],
                        "after": updated[:500],
                    }
                )
        for edit in edits:
            edit_counts[f"instruction:{edit}"] += 1
        record["HLS_instruction"] = updated

        original_code = str(record.get("hls_cpp", ""))
        updated_code, code_edits = replace_vivado_terms(original_code)
        if updated_code != original_code:
            changed_code_records += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "record_id": idx,
                        "file": record.get("file"),
                        "edits": [f"hls_cpp:{edit}" for edit in code_edits],
                        "before": original_code[:500],
                        "after": updated_code[:500],
                    }
                )
        for edit in code_edits:
            edit_counts[f"hls_cpp:{edit}"] += 1
        record["hls_cpp"] = updated_code

    remaining_vivado_records = [
        {"record_id": idx, "file": record.get("file")}
        for idx, record in enumerate(records)
        if "vivado" in str(record.get("HLS_instruction", "")).lower()
    ]
    missing_vitis_records = [
        {"record_id": idx, "file": record.get("file")}
        for idx, record in enumerate(records)
        if "vitis hls" not in str(record.get("HLS_instruction", "")).lower()
    ]
    remaining_dataset_vivado_records = [
        {"record_id": idx, "file": record.get("file")}
        for idx, record in enumerate(records)
        if "vivado" in json.dumps(record, ensure_ascii=False).lower()
    ]

    output_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "input": str(input_path),
        "output": str(output_path),
        "count": len(records),
        "changed_instruction_records": changed_records,
        "changed_hls_cpp_records": changed_code_records,
        "edit_counts": dict(edit_counts),
        "remaining_instruction_vivado_records": remaining_vivado_records,
        "missing_instruction_vitis_hls_records": missing_vitis_records,
        "remaining_dataset_vivado_records": remaining_dataset_vivado_records,
        "samples": samples,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
