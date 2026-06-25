#!/usr/bin/env python3
"""Generate Vitis-oriented testbench scaffolds for HLS_NL JSON/JSONL records.

The generator is intentionally conservative. It emits semantic self-checking
testbenches only for recognized small patterns; otherwise it emits deterministic
driver/smoke testbenches that are useful for CSim/CoSim stimulus but are not a
functional-equivalence proof by themselves.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TYPE_PREFIX_RE = (
    r"(?:void|bool|int|unsigned\s+int|unsigned|long|short|char|float|double|"
    r"ap_u?int\s*<[^>]+>|ap_u?fixed\s*<[^>]+>|hls::stream\s*<[^>]+>|"
    r"u?int\d+_t)"
)


@dataclass
class Arg:
    raw: str
    c_type: str
    name: str

    @property
    def base_type(self) -> str:
        text = self.c_type
        text = re.sub(r"\bconst\b", "", text)
        text = text.replace("&", "").replace("*", "")
        return re.sub(r"\s+", " ", text).strip()

    @property
    def is_reference_or_pointer(self) -> bool:
        return "&" in self.c_type or "*" in self.c_type

    @property
    def is_const(self) -> bool:
        return bool(re.search(r"\bconst\b", self.c_type))

    @property
    def is_stream(self) -> bool:
        return "hls::stream" in self.c_type

    @property
    def direction(self) -> str:
        if self.is_stream:
            lower = self.name.lower()
            if lower.startswith(("in", "input")) or lower.endswith(("_in", "input")):
                return "input"
            if lower.startswith(("out", "output")) or lower.endswith(("_out", "output")):
                return "output"
            return "inout"
        if self.is_reference_or_pointer and not self.is_const:
            return "output"
        return "input"


@dataclass
class FunctionSig:
    return_type: str
    name: str
    args: list[Arg]
    signature: str


def split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    angle = paren = bracket = 0
    for idx, ch in enumerate(text):
        if ch == "<":
            angle += 1
        elif ch == ">" and angle:
            angle -= 1
        elif ch == "(":
            paren += 1
        elif ch == ")" and paren:
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]" and bracket:
            bracket -= 1
        elif ch == "," and angle == paren == bracket == 0:
            parts.append(text[start:idx].strip())
            start = idx + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def find_matching(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def parse_arg(raw: str) -> Arg | None:
    raw = raw.strip()
    if not raw or raw == "void":
        return None
    raw = raw.split("=", 1)[0].strip()
    raw = re.sub(r"\s+", " ", raw)
    match = re.match(r"(?P<prefix>.+?)(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\])*$", raw)
    if not match:
        return None
    c_type = match.group("prefix").strip()
    name = match.group("name").strip()
    return Arg(raw=raw, c_type=c_type, name=name)


def extract_function(code: str) -> FunctionSig | None:
    pattern = re.compile(
        rf"(?P<ret>{TYPE_PREFIX_RE}(?:\s*[*&]|\s+\w[\w:<>,\s*&]*)?)\s+"
        rf"(?P<name>[A-Za-z_]\w*)\s*\(",
        re.S,
    )
    for match in pattern.finditer(code):
        open_idx = code.find("(", match.end() - 1)
        close_idx = find_matching(code, open_idx, "(", ")")
        if close_idx < 0:
            continue
        after = code[close_idx + 1 :].lstrip()
        if not after.startswith("{"):
            continue
        args_text = code[open_idx + 1 : close_idx]
        args = [arg for part in split_top_level_commas(args_text) if (arg := parse_arg(part))]
        ret = re.sub(r"\s+", " ", match.group("ret")).strip()
        name = match.group("name")
        signature = f"{ret} {name}({', '.join(arg.raw for arg in args)})"
        return FunctionSig(ret, name, args, signature)
    return None


def extract_design_title(prompt: str) -> str | None:
    match = re.search(r"\*\*Design Task:\*\*\s*([^\n]+)", prompt)
    if match:
        return match.group(1).strip()
    match = re.search(r"Design Task:\s*([^\n]+)", prompt)
    return match.group(1).strip() if match else None


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load either HLS_NL.json or repaired HLS_NL JSONL records."""

    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if path.suffix.lower() == ".jsonl" or (stripped and not stripped.startswith("[")):
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise SystemExit(f"JSONL line {line_no} must be an object")
            records.append(row)
        return records

    data = json.loads(text)
    if not isinstance(data, list):
        raise SystemExit("HLS_NL JSON root must be a list")
    if not all(isinstance(record, dict) for record in data):
        raise SystemExit("HLS_NL records must be objects")
    return data


def record_source_file(record: dict[str, Any]) -> Any:
    return record.get("file") or record.get("original_file") or record.get("source")


def record_design_title(record: dict[str, Any]) -> str | None:
    return record.get("design_title") or extract_design_title(str(record.get("HLS_instruction", "")))


def record_id_for(record: dict[str, Any], fallback: int) -> int:
    try:
        return int(record.get("record_id", fallback))
    except (TypeError, ValueError):
        return fallback


def identifier(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_").lower()
    return text or "record"


def macro_lines(code: str) -> list[str]:
    lines: list[str] = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("#define "):
            lines.append(stripped)
    return lines


def cpp_string(text: str) -> str:
    return json.dumps(text)


def is_integer_type(c_type: str) -> bool:
    base = re.sub(r"\s+", " ", c_type)
    return bool(
        "ap_uint" in base
        or "ap_int" in base
        or re.search(r"\b(u?int\d+_t|int|unsigned|long|short|char|bool)\b", base)
    )


def value_expr(arg: Arg, salt: int) -> str:
    base = arg.base_type
    name = arg.name.lower()
    if name in {"reset_n", "rst_n", "aresetn"}:
        return f"static_cast<{base}>(1)"
    if name in {"reset", "rst", "areset"}:
        return f"static_cast<{base}>(0)"
    if base == "bool":
        return f"((test_idx + {salt}) % 2) != 0"
    if "float" in base or "double" in base or "ap_fixed" in base:
        return f"static_cast<{base}>(((test_idx + {salt}) % 17) - 8)"
    return f"static_cast<{base}>(pattern_value(test_idx, {salt}))"


def print_expr(expr: str) -> str:
    return expr


def declare_arg(arg: Arg, idx: int) -> str:
    base = arg.base_type
    if arg.is_stream:
        lines = [f"    {base} {arg.name};"]
        if arg.direction in {"input", "inout"}:
            lines.append(f"    for (int s = 0; s < 4; ++s) {arg.name}.write(pattern_value(test_idx, {idx} + s));")
        return "\n".join(lines)
    if "*" in arg.c_type:
        if arg.direction == "input":
            return (
                f"    {base} {arg.name}[16] = {{}};\n"
                f"    for (int i = 0; i < 16; ++i) {arg.name}[i] = static_cast<{base}>(pattern_value(test_idx, {idx} + i));"
            )
        return (
            f"    {base} {arg.name}[16] = {{}};\n"
            f"    for (int i = 0; i < 16; ++i) {arg.name}[i] = static_cast<{base}>(0xA5A5A5A5ULL ^ i);"
        )
    if arg.direction == "output":
        return f"    {base} {arg.name} = static_cast<{base}>(0xA5A5A5A5ULL);"
    return f"    {base} {arg.name} = {value_expr(arg, idx)};"


def call_arg(arg: Arg) -> str:
    return arg.name


def find_arg(args: list[Arg], names: set[str]) -> Arg | None:
    lowered = {name.lower() for name in names}
    for arg in args:
        if arg.name.lower() in lowered:
            return arg
    return None


def input_args(sig: FunctionSig) -> list[Arg]:
    return [arg for arg in sig.args if arg.direction == "input" and not arg.is_stream and "*" not in arg.c_type]


def output_args(sig: FunctionSig) -> list[Arg]:
    return [arg for arg in sig.args if arg.direction == "output" and not arg.is_stream and "*" not in arg.c_type]


def mismatch_line(label: str, expected: str, actual: str) -> str:
    return (
        f'      std::cerr << "Mismatch test=" << test_idx << " field={label} expected=" '
        f"<< {print_expr(expected)} << \" actual=\" << {print_expr(actual)} << \"\\n\";\n"
        "      return 1;"
    )


def semantic_checks(sig: FunctionSig) -> tuple[str, str]:
    name = sig.name.lower()
    ins = input_args(sig)
    outs = output_args(sig)
    out_names = {arg.name.lower(): arg for arg in outs}
    in_names = {arg.name.lower(): arg for arg in ins}
    checks: list[str] = []

    a = find_arg(ins, {"a", "A", "in1", "x"})
    b = find_arg(ins, {"b", "B", "in2", "y"})
    cin = find_arg(ins, {"cin", "carry_in", "c_in"})

    if "simple_calculator" in name or name == "calculator":
        if a and b:
            for label, expr in (
                ("add", f"{a.name} + {b.name}"),
                ("sub", f"{a.name} - {b.name}"),
                ("mul", f"{a.name} * {b.name}"),
            ):
                out = out_names.get(label)
                if out:
                    checks.append(f"    if ({out.name} != static_cast<{out.base_type}>({expr})) {{\n{mismatch_line(label, f'static_cast<{out.base_type}>({expr})', out.name)}\n    }}")
            div_out = out_names.get("div")
            if div_out:
                expected = f"(({b.name} == 0) ? static_cast<{div_out.base_type}>(0) : static_cast<{div_out.base_type}>({a.name} / {b.name}))"
                checks.append(f"    if ({div_out.name} != {expected}) {{\n{mismatch_line('div', expected, div_out.name)}\n    }}")

    if ("comparator" in name or "compare" in name) and a and b:
        mapping = {
            "gt": f"({a.name} > {b.name})",
            "greater": f"({a.name} > {b.name})",
            "eq": f"({a.name} == {b.name})",
            "equal": f"({a.name} == {b.name})",
            "lt": f"({a.name} < {b.name})",
            "less": f"({a.name} < {b.name})",
        }
        for out_name, expr in mapping.items():
            out = out_names.get(out_name)
            if out:
                expected = f"static_cast<{out.base_type}>({expr})"
                checks.append(f"    if ({out.name} != {expected}) {{\n{mismatch_line(out.name, expected, out.name)}\n    }}")

    if ("full_adder" in name or name == "adder") and a and b and cin:
        sum_out = find_arg(outs, {"sum", "s"})
        cout = find_arg(outs, {"cout", "carry_out", "c_out"})
        total = f"({print_expr(a.name)} + {print_expr(b.name)} + {print_expr(cin.name)})"
        if sum_out:
            expected = f"static_cast<{sum_out.base_type}>({total} & 1)"
            checks.append(f"    if ({sum_out.name} != {expected}) {{\n{mismatch_line(sum_out.name, expected, sum_out.name)}\n    }}")
        if cout:
            expected = f"static_cast<{cout.base_type}>(({total} >> 1) & 1)"
            checks.append(f"    if ({cout.name} != {expected}) {{\n{mismatch_line(cout.name, expected, cout.name)}\n    }}")

    if ("adder_subtractor" in name or "adder_sub" in name) and a and b:
        mode = find_arg(ins, {"mode", "sub", "subtract"})
        out = find_arg(outs, {"result", "out", "sum", "diff"})
        if mode and out:
            expected = f"static_cast<{out.base_type}>({mode.name} ? ({a.name} - {b.name}) : ({a.name} + {b.name}))"
            checks.append(f"    if ({out.name} != {expected}) {{\n{mismatch_line(out.name, expected, out.name)}\n    }}")

    if ("subtractor" in name or name.startswith("sub")) and a and b:
        out = find_arg(outs, {"result", "out", "diff", "difference"})
        if out:
            expected = f"static_cast<{out.base_type}>({a.name} - {b.name})"
            checks.append(f"    if ({out.name} != {expected}) {{\n{mismatch_line(out.name, expected, out.name)}\n    }}")

    if ("multiplier" in name or name.startswith("multi") or "multiply" in name) and a and b:
        out = find_arg(outs, {"product", "result", "out", "p"})
        if out:
            expected = f"static_cast<{out.base_type}>({a.name} * {b.name})"
            checks.append(f"    if ({out.name} != {expected}) {{\n{mismatch_line(out.name, expected, out.name)}\n    }}")

    if ("max" in name) and len(ins) >= 2:
        out = find_arg(outs, {"max", "max_val", "out", "result"})
        if out:
            expr = ins[0].name
            for arg in ins[1:]:
                expr = f"(({expr} > {arg.name}) ? {expr} : {arg.name})"
            expected = f"static_cast<{out.base_type}>({expr})"
            checks.append(f"    if ({out.name} != {expected}) {{\n{mismatch_line(out.name, expected, out.name)}\n    }}")

    if ("gray" in name) and ins and outs:
        expected = f"static_cast<{outs[0].base_type}>({ins[0].name} ^ ({ins[0].name} >> 1))"
        checks.append(f"    if ({outs[0].name} != {expected}) {{\n{mismatch_line(outs[0].name, expected, outs[0].name)}\n    }}")

    if ("mux" in name) and outs:
        sel = find_arg(ins, {"sel", "select", "ctrl", "control"})
        data_inputs = [arg for arg in ins if arg is not sel]
        if sel and len(data_inputs) >= 2:
            expected_var = f"expected_{outs[0].name}"
            lines = [f"    {outs[0].base_type} {expected_var} = static_cast<{outs[0].base_type}>({data_inputs[0].name});"]
            for idx, arg in enumerate(data_inputs[1:], start=1):
                lines.append(f"    if ({print_expr(sel.name)} == {idx}) {expected_var} = static_cast<{outs[0].base_type}>({arg.name});")
            lines.append(f"    if ({outs[0].name} != {expected_var}) {{\n{mismatch_line(outs[0].name, expected_var, outs[0].name)}\n    }}")
            checks.extend(lines)

    if checks:
        return "semantic", "\n".join(checks)
    if any(arg.is_stream for arg in sig.args) or re.search(r"(counter|fifo|shift_register|flip_flop|edge|ram|memory)", name):
        return "property", "    // Property/driver testbench: stimulus is deterministic; task-specific assertions should be added after contract audit."
    return "smoke", "    // Smoke/driver testbench: no semantic oracle was inferred for this task."


def render_testbench(record: dict[str, Any], sig: FunctionSig, record_id: int) -> tuple[str, str]:
    oracle_kind, checks = semantic_checks(sig)
    declarations = "\n".join(declare_arg(arg, idx + 1) for idx, arg in enumerate(sig.args))
    ret_decl = ""
    ret_assign = ""
    if sig.return_type != "void":
        ret_decl = f"    {sig.return_type} ret = {{}};"
        ret_assign = "ret = "
    title = record_design_title(record) or sig.name
    defines = "\n".join(macro_lines(str(record.get("hls_cpp", ""))))
    if defines:
        defines += "\n"
    return (
        oracle_kind,
        f"""// Generated from HLS_NL.json by generate_hls_nl_testbenches.py.
// Record: {record_id}
// Source file: {record_source_file(record)}
// Design title: {title}
// Oracle kind: {oracle_kind}
#include <ap_int.h>
#include <ap_fixed.h>
#include <hls_stream.h>
#include <cstdint>
#include <iostream>

{defines}{sig.signature};

unsigned long long pattern_value(int test_idx, int salt) {{
  if (test_idx == 0) return 0ULL;
  if (test_idx == 1) return ~0ULL;
  if (test_idx == 2) return 0xAAAAAAAAULL ^ static_cast<unsigned long long>(salt);
  if (test_idx == 3) return 0x55555555ULL ^ static_cast<unsigned long long>(salt);
  return static_cast<unsigned long long>(test_idx * 1103515245ULL + salt * 12345ULL);
}}

int main() {{
  constexpr int kTests = 64;
  for (int test_idx = 0; test_idx < kTests; ++test_idx) {{
{declarations}
{ret_decl}
    {ret_assign}{sig.name}({', '.join(call_arg(arg) for arg in sig.args)});
{checks}
  }}
  std::cout << "hls_nl_tb: all " << kTests << " tests completed for {sig.name} ({oracle_kind})\\n";
  return 0;
}}
"""
    )


def render_tcl(sig: FunctionSig, part: str, clock: str) -> str:
    return f"""# Generated from HLS_NL.json.
open_project -reset hls_nl_project
set_top {sig.name}
add_files dut.cpp
add_files -tb tb.cpp
open_solution "solution1" -flow_target vivado
set_part {{{part}}}
create_clock -period {clock} -name default
csim_design
csynth_design
cosim_design -rtl verilog
exit
"""


def write_design(out_dir: Path, record: dict[str, Any], sig: FunctionSig, record_id: int, part: str, clock: str) -> dict[str, Any]:
    source_file = record_source_file(record)
    stem = identifier(Path(str(source_file or f"record_{record_id}")).stem)
    design_dir = out_dir / f"{record_id:05d}_{stem}_{sig.name}"
    design_dir.mkdir(parents=True, exist_ok=True)
    code = str(record.get("hls_cpp", "")).replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    oracle_kind, tb = render_testbench(record, sig, record_id)
    (design_dir / "dut.cpp").write_text(code, encoding="utf-8")
    (design_dir / "tb.cpp").write_text(tb, encoding="utf-8")
    (design_dir / "run_hls.tcl").write_text(render_tcl(sig, part, clock), encoding="utf-8")
    (design_dir / "instruction.txt").write_text(str(record.get("HLS_instruction", "")), encoding="utf-8")
    return {
        "record_id": record_id,
        "source_file": source_file,
        "design_title": record_design_title(record),
        "top": sig.name,
        "signature": sig.signature,
        "oracle_kind": oracle_kind,
        "path": str(design_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate HLS_NL Vitis testbench scaffolds.")
    parser.add_argument("--input", required=True, type=Path, help="Path to HLS_NL JSON or JSONL")
    parser.add_argument("--out-dir", required=True, type=Path, help="Output corpus directory")
    parser.add_argument("--limit", type=int, help="Maximum records to process")
    parser.add_argument("--offset", type=int, default=0, help="Starting record offset")
    parser.add_argument("--part", default="xc7z020clg484-1")
    parser.add_argument("--clock", default="10")
    args = parser.parse_args()

    data = load_records(args.input)
    records = data[args.offset :]
    if args.limit is not None:
        records = records[: args.limit]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for local_idx, record in enumerate(records):
        record_id = record_id_for(record, args.offset + local_idx)
        sig = extract_function(str(record.get("hls_cpp", "")))
        if sig is None:
            skipped.append({"record_id": record_id, "source_file": record_source_file(record), "reason": "no_parseable_function_definition"})
            continue
        row = write_design(args.out_dir, record, sig, record_id, args.part, args.clock)
        manifest.append(row)
        counts[row["oracle_kind"]] = counts.get(row["oracle_kind"], 0) + 1

    summary = {
        "input": str(args.input),
        "out_dir": str(args.out_dir),
        "processed": len(records),
        "generated": len(manifest),
        "skipped": len(skipped),
        "oracle_counts": counts,
    }
    (args.out_dir / "manifest.json").write_text(json.dumps({"summary": summary, "designs": manifest, "skipped": skipped}, indent=2), encoding="utf-8")
    md_lines = [
        "# HLS_NL Testbench Generation Report",
        "",
        f"- Input: `{args.input}`",
        f"- Output: `{args.out_dir}`",
        f"- Processed: {len(records)}",
        f"- Generated: {len(manifest)}",
        f"- Skipped: {len(skipped)}",
        f"- Oracle counts: {counts}",
        "",
        "| Record | Top | Oracle | Source |",
        "| --- | --- | --- | --- |",
    ]
    for row in manifest[:200]:
        md_lines.append(f"| {row['record_id']} | `{row['top']}` | {row['oracle_kind']} | `{row['source_file']}` |")
    if len(manifest) > 200:
        md_lines.append(f"| ... | ... | ... | {len(manifest) - 200} more in manifest.json |")
    (args.out_dir / "README.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
