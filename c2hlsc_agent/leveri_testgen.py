from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .analyze import AnalysisResult, FunctionArg, strip_comments
from .config import AgentConfig


LEVERI_TESTBENCH_POLICY_ID = "hls_leveri_shift_left_v1"
LEVERI_REFERENCE_REPO = "https://github.com/cz-5f/HLS-LeVeri"

LEVERI_TESTBENCH_SYSTEM_PROMPT = """You are the shift_left_testbench_agent for AUTO RTL.

Reference framework:
- Follow the HLS-LeVeri shift-left verification style.
- Build paired golden-C and HLS-C testbenches from the same interface contract.
- Keep HLS-C generation separate; this agent owns only stimulus, traces, checks, and coverage/refinement hooks.

Core requirements:
- Preserve one synchronized stimulus schedule for both golden C and HLS-C.
- Emit trace artifacts with a header row and a role row that classify columns as inputs or outputs.
- Perform dual-tier consistency checking:
  1. static/structural alignment of headers, roles, stimulus columns, argument ordering, and cycle count
  2. dynamic behavioral checking of output columns across the golden and HLS traces
- Use deterministic directed and pseudo-random stimuli so failing rows are reproducible.
- Collect concrete coverage with gcov when available.
- Generate a relational KLEE driver that calls both golden C and HLS-C from cloned
  symbolic state, then checks return values and complete pointer post-state equality.
- Treat generated traces as evidence, not proof; host equivalence, CSim, CSynth, and CoSim still decide acceptance.
- Record structured metadata for the live HLS verification knowledge graph: function name, arguments, directions, lengths, generated files, relational scope, bounds, assumptions, and check types.
"""


@dataclass(frozen=True)
class LeVeriTestbenchContract:
    policy_id: str
    owner_agent: str
    reference_repo: str
    owns_hlsc_generation: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "owner_agent": self.owner_agent,
            "reference_repo": self.reference_repo,
            "owns_hlsc_generation": self.owns_hlsc_generation,
        }


@dataclass(frozen=True)
class LeVeriTestbenchBundle:
    golden_tb: str
    hls_tb: str
    compare_script: str
    gcov_script: str
    klee_driver: str
    klee_script: str
    manifest_json: str
    policy_id: str = LEVERI_TESTBENCH_POLICY_ID


def get_leveri_testbench_contract() -> LeVeriTestbenchContract:
    return LeVeriTestbenchContract(
        policy_id=LEVERI_TESTBENCH_POLICY_ID,
        owner_agent="shift_left_testbench_agent",
        reference_repo=LEVERI_REFERENCE_REPO,
    )


def _is_unsigned(c_type: str) -> bool:
    return "unsigned" in c_type or c_type.strip().startswith("uint") or "ap_uint" in c_type


def _storage_type(arg: FunctionArg) -> str:
    return " ".join(token for token in arg.c_type.split() if token not in {"const", "volatile"})


def _scalar_decl(arg: FunctionArg) -> str:
    if arg.scalar_range:
        lo, hi = arg.scalar_range
        return f"bounded_scalar<{arg.c_type}>(cycle, rng, {lo}LL, {hi}LL)"
    return f"random_value<{arg.c_type}>(rng)"


def _init_array(arg: FunctionArg) -> str:
    if arg.direction == "output":
        return f"""for (int i = 0; i < {arg.length}; ++i) {{
    {arg.name}[i] = output_sentinel<{_storage_type(arg)}>(cycle, i);
  }}"""
    unsigned = "true" if _is_unsigned(arg.c_type) else "false"
    return f"""for (int i = 0; i < {arg.length}; ++i) {{
    {arg.name}[i] = patterned_value<{_storage_type(arg)}>(cycle, i, rng, {unsigned});
  }}"""


def _call_args(args: list[FunctionArg]) -> str:
    return ", ".join(arg.name for arg in args)


def _header_and_roles(function_args: list[FunctionArg], return_type: str) -> tuple[list[str], list[str]]:
    headers = ["cycle"]
    roles = ["meta"]
    for arg in function_args:
        if arg.is_pointer_like:
            if arg.direction in {"input", "inout"}:
                for idx in range(arg.length or 0):
                    suffix = "_in" if arg.direction == "inout" else ""
                    headers.append(f"{arg.name}{suffix}[{idx}]")
                    roles.append("in")
            if arg.direction in {"output", "inout"}:
                for idx in range(arg.length or 0):
                    suffix = "_out" if arg.direction == "inout" else ""
                    headers.append(f"{arg.name}{suffix}[{idx}]")
                    roles.append("out")
        else:
            headers.append(arg.name)
            roles.append("in")
    if return_type != "void":
        headers.append("return")
        roles.append("out")
    return headers, roles


def _write_header_line(items: list[str]) -> str:
    return "  trace << " + json.dumps(",".join(items) + "\n") + ";"


def _array_declarations(arrays: list[FunctionArg]) -> list[str]:
    declarations: list[str] = []
    for arg in arrays:
        declarations.append(f"  {_storage_type(arg)} {arg.name}[{arg.length}] = {{}};")
    return declarations


def _scalar_declarations(scalars: list[FunctionArg]) -> list[str]:
    return [f"  {arg.c_type} {arg.name} = {_scalar_decl(arg)};" for arg in scalars]


def _array_initializers(arrays: list[FunctionArg]) -> list[str]:
    return [_init_array(arg) for arg in arrays]


def _write_value_line(expr: str) -> str:
    return f"  write_csv_value(trace, {expr});"


def _write_row_lines(fn_args: list[FunctionArg], return_type: str) -> list[str]:
    lines = ["  trace << cycle;"]
    for arg in fn_args:
        if arg.is_pointer_like:
            if arg.direction in {"input", "inout"}:
                lines.append(f"  for (int i = 0; i < {arg.length}; ++i) {{")
                lines.append(_write_value_line(f"{arg.name}[i]"))
                lines.append("  }")
            if arg.direction in {"output", "inout"}:
                lines.append(f"  for (int i = 0; i < {arg.length}; ++i) {{")
                lines.append(_write_value_line(f"{arg.name}[i]"))
                lines.append("  }")
        else:
            lines.append(_write_value_line(arg.name))
    if return_type != "void":
        lines.append(_write_value_line("dut_return"))
    lines.append('  trace << "\\n";')
    return lines


def _common_helpers(seed: int) -> str:
    return f"""template <typename T>
T random_value(std::mt19937_64& rng) {{
  if (std::numeric_limits<T>::is_integer) {{
    return static_cast<T>(rng());
  }}
  // rng() is unsigned, so subtract in signed arithmetic to generate negative floats.
  return static_cast<T>(static_cast<long long>(rng() % 20001) - 10000) / static_cast<T>(100);
}}

template <typename T>
T bounded_scalar(int cycle, std::mt19937_64& rng, long long lo, long long hi) {{
  if (hi < lo) return static_cast<T>(lo);
  long long value = lo;
  if (cycle == 0) {{
    value = lo;
  }} else if (cycle == 1) {{
    value = hi;
  }} else if (cycle == 2) {{
    value = lo + ((hi - lo) / 2);
  }} else if (cycle == 3 && lo <= 1 && hi >= 1) {{
    value = 1;
  }} else {{
    const unsigned long long span = static_cast<unsigned long long>(hi - lo) + 1ULL;
    value = lo + static_cast<long long>(rng() % span);
  }}
  return static_cast<T>(value);
}}

template <typename T>
T patterned_value(int cycle, int element_idx, std::mt19937_64& rng, bool is_unsigned) {{
  if (cycle == 0) return static_cast<T>(0);
  if (cycle == 1) return static_cast<T>(~static_cast<unsigned long long>(0));
  if (cycle == 2 && std::numeric_limits<T>::is_integer) {{
    return is_unsigned ? std::numeric_limits<T>::max()
                       : (element_idx % 2 ? std::numeric_limits<T>::max() : std::numeric_limits<T>::min());
  }}
  if (cycle == 3) return static_cast<T>(element_idx % 2 ? 0xAAAAAAAAULL : 0x55555555ULL);
  return random_value<T>(rng);
}}

template <typename T>
T output_sentinel(int cycle, int element_idx) {{
  unsigned long long value = 0x9E3779B97F4A7C15ULL;
  value ^= static_cast<unsigned long long>(cycle + 1) * 0xBF58476D1CE4E5B9ULL;
  value ^= static_cast<unsigned long long>(element_idx + 1) * 0x94D049BB133111EBULL;
  return static_cast<T>(value);
}}

template <typename T>
void write_csv_value(std::ofstream& trace, const T& value) {{
  trace << "," << std::setprecision(17) << value;
}}

std::mt19937_64 make_trace_rng() {{
  return std::mt19937_64({seed}ULL);
}}
"""


def _render_trace_tb(
    analysis: AnalysisResult,
    config: AgentConfig,
    *,
    target_name: str,
    output_csv: str,
    include_block: str,
) -> str:
    fn = analysis.function
    arrays = [arg for arg in fn.args if arg.is_pointer_like]
    scalars = [arg for arg in fn.args if not arg.is_pointer_like]
    headers, roles = _header_and_roles(fn.args, fn.return_type)
    declarations = _array_declarations(arrays) + _scalar_declarations(scalars)
    initializers = _array_initializers(arrays)
    row_lines = _write_row_lines(fn.args, fn.return_type)
    return_prefix = f"{fn.return_type} dut_return = " if fn.return_type != "void" else ""

    return f"""// Generated by c2hlsc_agent using {LEVERI_TESTBENCH_POLICY_ID}.
// LeVeri-style paired trace testbench: emits one CSV trace for dual-tier checking.
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <random>

{include_block}

{_common_helpers(config.seed)}

int main() {{
  std::ofstream trace({json.dumps(output_csv)}, std::ofstream::out);
  if (!trace.is_open()) {{
    std::cerr << "failed to open {output_csv}\\n";
    return 1;
  }}
{_write_header_line(headers)}
{_write_header_line(roles)}

  std::mt19937_64 rng = make_trace_rng();
  for (int cycle = 0; cycle < {config.num_tests}; ++cycle) {{
{chr(10).join(declarations)}
{chr(10).join(initializers)}

  {return_prefix}{target_name}({_call_args(fn.args)});
{chr(10).join(row_lines)}
  }}

  trace.close();
  return 0;
}}
"""


def _compare_script() -> str:
    return """#!/usr/bin/env python3
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path


def read_trace(path: Path) -> tuple[list[str], list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 2:
        raise SystemExit(f"{path}: expected header row, role row, and trace data")
    return rows[0], rows[1], rows[2:]


def fail(message: str) -> None:
    print(f"HLS-LeVeri consistency check failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def values_match(golden: str, hls: str) -> bool:
    if golden == hls:
        return True
    try:
        gf = float(golden)
        hf = float(hls)
    except ValueError:
        return False
    if gf != gf and hf != hf:
        return True
    if gf == hf:
        return True
    if not math.isfinite(gf) or not math.isfinite(hf):
        return False
    diff = abs(gf - hf)
    scale = max(abs(gf), abs(hf), 1.0)
    return diff <= 1e-6 * scale


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: leveri_compare.py GOLDEN_TRACE.csv HLS_TRACE.csv", file=sys.stderr)
        return 2

    golden_header, golden_roles, golden_rows = read_trace(Path(argv[1]))
    hls_header, hls_roles, hls_rows = read_trace(Path(argv[2]))

    if golden_header != hls_header:
        fail("static header mismatch")
    if golden_roles != hls_roles:
        fail("static role-row mismatch")
    if len(golden_rows) != len(hls_rows):
        fail(f"cycle-count mismatch golden={len(golden_rows)} hls={len(hls_rows)}")

    input_columns = [idx for idx, role in enumerate(golden_roles) if role == "in"]
    output_columns = [idx for idx, role in enumerate(golden_roles) if role == "out"]

    for row_idx, (golden, hls) in enumerate(zip(golden_rows, hls_rows)):
        if len(golden) != len(golden_header) or len(hls) != len(hls_header):
            fail(f"row width mismatch at trace row {row_idx}")
        for col_idx in input_columns:
            if golden[col_idx] != hls[col_idx]:
                fail(
                    f"stimulus mismatch cycle={golden[0]} column={golden_header[col_idx]} "
                    f"golden={golden[col_idx]} hls={hls[col_idx]}"
                )
        for col_idx in output_columns:
            if not values_match(golden[col_idx], hls[col_idx]):
                fail(
                    f"behavior mismatch cycle={golden[0]} column={golden_header[col_idx]} "
                    f"expected={golden[col_idx]} actual={hls[col_idx]}"
                )

    print(
        "HLS-LeVeri consistency check passed: "
        f"{len(golden_rows)} cycles, {len(input_columns)} input columns, {len(output_columns)} output columns"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
"""


_KLEE_LENGTH_NAMES = {"n", "len", "length", "size", "count", "num", "limit", "samples", "elements"}


def _klee_scalar_bounds(arg: FunctionArg, pointer_args: list[FunctionArg]) -> tuple[int, int] | None:
    if arg.scalar_range is not None:
        return arg.scalar_range
    name = arg.name.lower()
    related_lengths = [
        pointer.length
        for pointer in pointer_args
        if pointer.length is not None
        and (
            name in _KLEE_LENGTH_NAMES
            or name in {
                f"{pointer.name.lower()}_n",
                f"n_{pointer.name.lower()}",
                f"{pointer.name.lower()}_len",
                f"{pointer.name.lower()}_length",
                f"{pointer.name.lower()}_size",
                f"{pointer.name.lower()}_count",
            }
            or name.startswith("num_")
            or name.endswith(("_len", "_length", "_size", "_count"))
        )
    ]
    return (0, min(related_lengths)) if related_lengths else None


def _static_storage_reasons(source: str, label: str) -> list[str]:
    """Conservatively find variable static storage while allowing static functions."""

    clean = strip_comments(source)
    reasons: list[str] = []
    for match in re.finditer(r"\bstatic\b", clean):
        tail = clean[match.start() :]
        semicolon = tail.find(";")
        brace = tail.find("{")
        delimiters = [index for index in (semicolon, brace) if index >= 0]
        if not delimiters:
            reasons.append(f"{label} contains unresolved static storage")
            continue
        end = min(delimiters)
        declaration = " ".join(tail[: end + 1].split())
        # `static int helper(int) {` and `static int helper(int);` are functions,
        # not mutable storage. An initializer (`static int x = helper();`) is state.
        if "(" in declaration and "=" not in declaration:
            if declaration.endswith("{") or re.search(r"\)\s*;$", declaration):
                continue
        reasons.append(f"{label} static storage is outside the single-invocation model: {declaration[:80]}")
    return reasons


def _file_scope_state_reasons(source: str, label: str) -> list[str]:
    clean = "\n".join(
        line for line in strip_comments(source).splitlines() if not line.lstrip().startswith("#")
    )
    reasons: list[str] = []
    depth = 0
    statement: list[str] = []
    for char in clean:
        if char == "{":
            depth += 1
            if depth == 1:
                statement = []
            continue
        if char == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                statement = []
            continue
        if depth:
            continue
        statement.append(char)
        if char != ";":
            continue
        text = " ".join("".join(statement).split())
        statement = []
        if (
            not text
            or "(" in text
            or re.match(r"^(?:typedef|using|struct|union|enum)\b", text)
            or re.search(r"\bconst\b", text)
        ):
            continue
        reasons.append(f"{label} mutable file-scope state is outside the relational model: {text[:80]}")
    return reasons


def _klee_unsupported_reasons(
    analysis: AnalysisResult, hlsc_source: str | None = None
) -> list[str]:
    fn = analysis.function
    reasons: list[str] = []
    observable_count = (1 if fn.return_type != "void" else 0) + sum(
        arg.length or 0 for arg in fn.args if arg.is_pointer_like
    )
    if observable_count == 0:
        reasons.append("no return value or pointer post-state is available to compare")
    if "*" in fn.return_type or "&" in fn.return_type:
        reasons.append("pointer/reference return values are not relationally comparable")
    type_texts = [("return", fn.return_type)] + [(arg.name, arg.c_type) for arg in fn.args]
    for name, c_type in type_texts:
        lowered = c_type.lower()
        if any(
            token in lowered
            for token in ("float", "double", "ap_int", "ap_uint", "hls::", "struct ", "union ", "class ")
        ):
            reasons.append(f"{name} uses a symbolic type outside the supported integral subset: {c_type}")
    for arg in fn.args:
        if arg.pointer_depth > 1:
            reasons.append(f"{arg.name} has pointer depth {arg.pointer_depth}; only one-dimensional buffers are supported")
        if arg.is_pointer_like and (arg.length is None or arg.length <= 0):
            reasons.append(f"{arg.name} has no positive finite buffer bound")
        if not arg.is_pointer_like and "&" in arg.raw:
            reasons.append(f"{arg.name} is a reference argument; scalar post-state cloning is unsupported")
        if len(arg.array_dims) > 1 or any(dim and not dim.strip().isdigit() for dim in arg.array_dims):
            reasons.append(f"{arg.name} uses a multidimensional or nonconstant array contract")
    if "..." in fn.signature:
        reasons.append("variadic signatures are unsupported")
    try:
        source = strip_comments(fn.source_path.read_text(encoding="utf-8"))
    except OSError:
        source = ""
    reasons.extend(_static_storage_reasons(source, "golden C"))
    reasons.extend(_file_scope_state_reasons(source, "golden C"))
    if hlsc_source is not None:
        reasons.extend(_static_storage_reasons(hlsc_source, "generated HLS-C"))
        reasons.extend(_file_scope_state_reasons(hlsc_source, "generated HLS-C"))
    return sorted(set(reasons))


def _klee_driver(analysis: AnalysisResult) -> str:
    fn = analysis.function
    pointer_args = [arg for arg in fn.args if arg.is_pointer_like]
    declarations: list[str] = []
    setup: list[str] = []
    golden_contract_checks: list[str] = []
    hlsc_contract_checks: list[str] = []
    comparisons: list[str] = []

    for arg in fn.args:
        if arg.is_pointer_like:
            storage_type = _storage_type(arg)
            declarations.extend(
                [
                    f"  {storage_type} seed_{arg.name}[{arg.length}] = {{}};",
                    f"  {storage_type} golden_{arg.name}[{arg.length}] = {{}};",
                    f"  {storage_type} hlsc_{arg.name}[{arg.length}] = {{}};",
                ]
            )
            setup.append(
                f'  klee_make_symbolic(seed_{arg.name}, sizeof(seed_{arg.name}), "{arg.name}_initial");'
            )
            setup.append(f"  for (int i = 0; i < {arg.length}; ++i) {{")
            setup.append(f"    golden_{arg.name}[i] = seed_{arg.name}[i];")
            setup.append(f"    hlsc_{arg.name}[i] = seed_{arg.name}[i];")
            setup.append("  }")
            comparisons.append(f"  for (int i = 0; i < {arg.length}; ++i) {{")
            comparisons.append(
                f'    c2hlsc_require_equal(golden_{arg.name}[i], hlsc_{arg.name}[i], '
                f'"C2HLSC_RELATIONAL_MISMATCH:{arg.name}");'
            )
            if arg.direction == "input":
                golden_contract_checks.extend(
                    [
                        f"  for (int i = 0; i < {arg.length}; ++i) {{",
                        f'    c2hlsc_require_unchanged(golden_{arg.name}[i], seed_{arg.name}[i], '
                        f'"C2HLSC_INPUT_CONTRACT_MUTATION:golden:{arg.name}");',
                        "  }",
                    ]
                )
                hlsc_contract_checks.extend(
                    [
                        f"  for (int i = 0; i < {arg.length}; ++i) {{",
                        f'    c2hlsc_require_unchanged(hlsc_{arg.name}[i], seed_{arg.name}[i], '
                        f'"C2HLSC_INPUT_CONTRACT_MUTATION:hlsc:{arg.name}");',
                        "  }",
                    ]
                )
            comparisons.append("  }")
        else:
            scalar_type = _storage_type(arg)
            declarations.extend(
                [
                    f"  {scalar_type} shared_{arg.name} = {{}};",
                    f"  {scalar_type} golden_{arg.name} = {{}};",
                    f"  {scalar_type} hlsc_{arg.name} = {{}};",
                ]
            )
            setup.append(
                f'  klee_make_symbolic(&shared_{arg.name}, sizeof(shared_{arg.name}), "{arg.name}");'
            )
            bounds = _klee_scalar_bounds(arg, pointer_args)
            if bounds is not None:
                lo, hi = bounds
                setup.append(f"  klee_assume(shared_{arg.name} >= static_cast<{scalar_type}>({lo}));")
                setup.append(f"  klee_assume(shared_{arg.name} <= static_cast<{scalar_type}>({hi}));")
            setup.append(f"  golden_{arg.name} = shared_{arg.name};")
            setup.append(f"  hlsc_{arg.name} = shared_{arg.name};")

    golden_args = ", ".join(
        f"golden_{arg.name}" for arg in fn.args
    )
    hlsc_args = ", ".join(
        f"hlsc_{arg.name}" for arg in fn.args
    )
    calls: list[str] = []
    if fn.return_type == "void":
        calls.extend(
            [
                f"  {fn.name}_ref({golden_args});",
                *golden_contract_checks,
                f"  {fn.name}({hlsc_args});",
                *hlsc_contract_checks,
            ]
        )
    else:
        calls.extend(
            [
                f"  {fn.return_type} golden_return = {fn.name}_ref({golden_args});",
                *golden_contract_checks,
                f"  {fn.return_type} hlsc_return = {fn.name}({hlsc_args});",
                *hlsc_contract_checks,
                '  c2hlsc_require_equal(golden_return, hlsc_return, "C2HLSC_RELATIONAL_MISMATCH:return");',
            ]
        )

    return f"""// Generated by c2hlsc_agent using {LEVERI_TESTBENCH_POLICY_ID}.
// Relational KLEE driver: golden C and HLS-C receive cloned symbolic initial state.
// Proof scope assumes distinct/non-aliasing pointer arguments and no mutable hidden state.
#include <cstddef>
#include <cstdint>
#include <klee/klee.h>

#include "../src/hls_top.hpp"

extern "C" {{
#define restrict __restrict__
#define {fn.name} {fn.name}_ref
#include "../input.c"
#undef {fn.name}
}}

[[noreturn]] static void c2hlsc_relational_mismatch(const char* observable) {{
  klee_report_error(__FILE__, __LINE__, observable, "c2hlsc_relational.err");
}}

[[noreturn]] static void c2hlsc_input_contract_violation(const char* observable) {{
  klee_report_error(__FILE__, __LINE__, observable, "c2hlsc_contract.err");
}}

template <typename GoldenT, typename HlscT>
static void c2hlsc_require_equal(const GoldenT& golden, const HlscT& hlsc, const char* observable) {{
  if (!(golden == hlsc)) c2hlsc_relational_mismatch(observable);
}}

template <typename ActualT, typename SeedT>
static void c2hlsc_require_unchanged(const ActualT& actual, const SeedT& seed, const char* observable) {{
  if (!(actual == seed)) c2hlsc_input_contract_violation(observable);
}}

int main() {{
{chr(10).join(declarations)}
{chr(10).join(setup)}
{chr(10).join(calls)}
{chr(10).join(comparisons)}
  return 0;
}}
"""


def _gcov_script() -> str:
    return """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COVERAGE_DIR = ROOT / "coverage"
REPORT_PATH = COVERAGE_DIR / "gcov_report.json"


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=check)


def write_report(payload: dict[str, object]) -> None:
    COVERAGE_DIR.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, indent=2) + "\\n", encoding="utf-8")


def tool(name: str) -> str | None:
    return shutil.which(name)


def main() -> int:
    cxx = os.environ.get("CXX", "g++")
    gcov = os.environ.get("GCOV", "gcov")
    if tool(cxx) is None or tool(gcov) is None:
        write_report({
            "status": "skipped",
            "reason": "CXX or gcov not found",
            "cxx": cxx,
            "gcov": gcov,
        })
        print("gcov coverage skipped: CXX or gcov not found")
        return 0

    COVERAGE_DIR.mkdir(exist_ok=True)
    for pattern in ("*.gcda", "*.gcno", "*.gcov"):
        for path in ROOT.rglob(pattern):
            path.unlink()

    flags = ["-std=c++17", "-Wall", "-Wextra", "-I", "src", "-O0", "--coverage"]
    extra = os.environ.get("C2HLSC_GCOV_CXXFLAGS", "").split()
    commands = [
        [cxx, *flags, *extra, "tb/leveri_golden_tb.cpp", "-o", "coverage/leveri_golden_tb"],
        [cxx, *flags, *extra, "tb/leveri_hls_tb.cpp", "src/hls_top.cpp", "-o", "coverage/leveri_hls_tb"],
        ["coverage/leveri_golden_tb"],
        ["coverage/leveri_hls_tb"],
        ["python3", "tb/leveri_compare.py", "leveri_golden_trace.csv", "leveri_hls_trace.csv"],
    ]
    command_logs: list[dict[str, object]] = []
    try:
        for cmd in commands:
            result = run(cmd)
            command_logs.append({
                "cmd": cmd,
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            })
    except subprocess.CalledProcessError as exc:
        command_logs.append({
            "cmd": exc.cmd,
            "returncode": exc.returncode,
            "stdout": exc.stdout[-4000:] if exc.stdout else "",
            "stderr": exc.stderr[-4000:] if exc.stderr else "",
        })
        write_report({"status": "fail", "stage": "build_or_run", "commands": command_logs})
        return exc.returncode or 1

    gcno_files = sorted(path.relative_to(ROOT) for path in COVERAGE_DIR.glob("*.gcno"))
    gcov_cmd = [gcov, "-b", "-c", "-o", str(COVERAGE_DIR), *map(str, gcno_files)]
    gcov_result = run(gcov_cmd, check=False)
    gcov_files = sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*.gcov"))
    coverage_data = sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*.gcda"))
    # Invoke gcov on the compiler-emitted note files instead of guessing their names
    # from source paths.  Compilers commonly prefix .gcno files with the output binary
    # name (for example ``leveri_hls_tb-hls_top.gcno``).  Raw .gcda data alone is not a
    # usable coverage report, so fail closed unless gcov successfully emits .gcov files.
    produced_coverage = gcov_result.returncode == 0 and bool(gcov_files)
    write_report({
        "status": "pass" if produced_coverage else "fail",
        "policy_id": "hls_leveri_shift_left_v1",
        "commands": command_logs,
        "gcov_cmd": gcov_cmd,
        "gcno_files": [str(path) for path in gcno_files],
        "gcov_returncode": gcov_result.returncode,
        "gcov_stdout": gcov_result.stdout[-8000:],
        "gcov_stderr": gcov_result.stderr[-8000:],
        "gcov_files": gcov_files,
        "coverage_data": coverage_data,
    })
    print(f"gcov coverage report written to {REPORT_PATH}")
    return 0 if produced_coverage else 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _klee_script() -> str:
    return """#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COVERAGE_DIR = ROOT / "coverage"
REPORT_PATH = COVERAGE_DIR / "klee_report.json"
MANIFEST_PATH = ROOT / "tb" / "leveri_manifest.json"
SCHEMA = "c2hlsc-klee-report-v1"
SCOPE = "golden_hlsc_relational"
PROVENANCE_FILES = (
    "input.c",
    "src/hls_top.hpp",
    "src/hls_top.cpp",
    "tb/klee_driver.cpp",
    "tb/leveri_manifest.json",
)


def provenance() -> dict[str, object]:
    hashes = {
        relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        for relative in PROVENANCE_FILES
        if (ROOT / relative).is_file()
    }
    top = None
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and isinstance(manifest.get("top"), str):
            top = manifest["top"]
    except (OSError, ValueError, TypeError):
        pass
    return {"top": top, "artifact_sha256": hashes}


def write_report(payload: dict[str, object]) -> None:
    COVERAGE_DIR.mkdir(exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps({"schema": SCHEMA, "scope": SCOPE, **payload, **provenance()}, indent=2) + "\\n",
        encoding="utf-8",
    )


def load_contract() -> dict[str, object]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("manifest must contain a JSON object")
    coverage_hooks = manifest.get("coverage_hooks")
    if not isinstance(coverage_hooks, dict):
        raise ValueError("manifest has no coverage hook object")
    contract = coverage_hooks.get("klee", {})
    assumptions = contract.get("assumptions") if isinstance(contract, dict) else None
    if (
        not isinstance(contract, dict)
        or contract.get("schema") != SCHEMA
        or contract.get("scope") != SCOPE
        or contract.get("invocations") != 1
        or not isinstance(contract.get("observable_count"), int)
        or (
            contract.get("observable_count", 0) <= 0
            and not contract.get("unsupported_reasons")
        )
        or not isinstance(assumptions, dict)
        or assumptions.get("pointer_alias_model") != "distinct_pointer_arguments"
        or assumptions.get("hidden_state_model") != "no_mutable_hidden_state"
        or assumptions.get("comparison") != "return_and_complete_pointer_post_state"
    ):
        raise ValueError("manifest has no relational KLEE contract")
    return contract


def resolve_tool(env_name: str, *candidate_names: str) -> str | None:
    # Honor an explicit override, otherwise search PATH for any candidate name.
    # No machine-specific fallback path so this runs on any machine.
    value = os.environ.get(env_name)
    if value:
        return value
    for name in candidate_names:
        found = shutil.which(name)
        if found:
            return found
    return None


def strip_source_comments(source: str) -> str:
    source = re.sub(r"/\\*.*?\\*/", "", source, flags=re.S)
    return re.sub(r"//.*", "", source)


def current_hlsc_hidden_state() -> list[str]:
    path = ROOT / "src" / "hls_top.cpp"
    source = strip_source_comments(path.read_text(encoding="utf-8"))
    reasons: list[str] = []
    for match in re.finditer(r"\\bstatic\\b", source):
        tail = source[match.start():]
        semicolon = tail.find(";")
        brace = tail.find("{")
        delimiters = [index for index in (semicolon, brace) if index >= 0]
        if not delimiters:
            reasons.append("generated HLS-C contains unresolved static storage")
            continue
        end = min(delimiters)
        declaration = " ".join(tail[:end + 1].split())
        if "(" in declaration and "=" not in declaration:
            if declaration.endswith("{") or re.search(r"\\)\\s*;$", declaration):
                continue
        reasons.append(f"generated HLS-C static storage: {declaration[:80]}")

    top_level = "\\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    depth = 0
    statement: list[str] = []
    for char in top_level:
        if char == "{":
            depth += 1
            if depth == 1:
                statement = []
            continue
        if char == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                statement = []
            continue
        if depth:
            continue
        statement.append(char)
        if char != ";":
            continue
        text = " ".join("".join(statement).split())
        statement = []
        if (
            not text
            or "(" in text
            or re.match(r"^(?:typedef|using|struct|union|enum)\\b", text)
            or re.search(r"\\bconst\\b", text)
        ):
            continue
        reasons.append(f"generated HLS-C mutable file-scope state: {text[:80]}")
    return sorted(set(reasons))


def default_klee_include(klee_path: str | None) -> str | None:
    # KLEE's headers (klee/klee.h) live under the install prefix that also holds
    # bin/klee, so derive the include dir from the resolved binary instead of a
    # hard-coded path. Falls back to None when it cannot be located.
    candidates = []
    if klee_path:
        prefix = Path(klee_path).resolve().parent.parent
        candidates.append(prefix / "include")
    for candidate in candidates:
        if (candidate / "klee" / "klee.h").exists():
            return str(candidate)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def main() -> int:
    try:
        contract = load_contract()
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        write_report({
            "status": "blocked",
            "outcome": "invalid_contract",
            "failure_kind": "manifest_invalid",
            "reason": str(exc),
        })
        return 1

    unsupported = contract.get("unsupported_reasons") or []
    if unsupported:
        write_report({
            "status": "blocked",
            "outcome": "unsupported_contract",
            "failure_kind": "unsupported_contract",
            "reason": "; ".join(str(reason) for reason in unsupported),
            "assumptions": contract.get("assumptions", {}),
        })
        print("KLEE relational check blocked: unsupported contract")
        return 1

    try:
        current_hidden_state = current_hlsc_hidden_state()
    except OSError as exc:
        write_report({
            "status": "blocked",
            "outcome": "invalid_candidate",
            "failure_kind": "candidate_preflight_failed",
            "reason": str(exc),
        })
        return 1
    if current_hidden_state:
        write_report({
            "status": "blocked",
            "outcome": "unsupported_contract",
            "failure_kind": "current_candidate_hidden_state",
            "reason": "; ".join(current_hidden_state),
            "assumptions": contract.get("assumptions", {}),
        })
        print("KLEE relational check blocked: current HLS-C has hidden state")
        return 1

    klee = resolve_tool("KLEE", "klee")
    clangxx = resolve_tool("KLEE_CXX", "klee-clang++", "clang++")
    llvm_link = resolve_tool("KLEE_LLVM_LINK", "llvm-link")
    klee_include = os.environ.get("KLEE_INCLUDE_DIR") or default_klee_include(klee)
    if klee is None:
        write_report({"status": "skipped", "outcome": "unavailable", "failure_kind": "tool_unavailable", "reason": "klee not found"})
        print("KLEE relational check skipped: klee not found")
        return 0
    if clangxx is None:
        write_report({"status": "skipped", "outcome": "unavailable", "failure_kind": "tool_unavailable", "reason": "clang++ not found"})
        print("KLEE relational check skipped: clang++ not found")
        return 0
    if llvm_link is None:
        write_report({"status": "skipped", "outcome": "unavailable", "failure_kind": "tool_unavailable", "reason": "llvm-link not found"})
        print("KLEE relational check skipped: llvm-link not found")
        return 0
    if klee_include is None or not Path(klee_include).exists():
        write_report({"status": "skipped", "outcome": "unavailable", "failure_kind": "tool_unavailable", "reason": "KLEE include directory not found"})
        print("KLEE relational check skipped: include directory not found")
        return 0

    COVERAGE_DIR.mkdir(exist_ok=True)
    driver_bitcode = COVERAGE_DIR / "klee_driver.bc"
    hlsc_bitcode = COVERAGE_DIR / "klee_hlsc.bc"
    relational_bitcode = COVERAGE_DIR / "klee_relational.bc"
    klee_out = COVERAGE_DIR / "klee-out"
    if klee_out.exists():
        shutil.rmtree(klee_out)

    common_flags = [
        clangxx,
        "-std=c++17",
        "-Wno-unknown-pragmas",
        "-I",
        ".",
        "-I",
        "src",
        "-I",
        klee_include,
        "-emit-llvm",
        "-c",
        "-g",
        "-O0",
    ]
    compile_driver_cmd = [*common_flags, "tb/klee_driver.cpp", "-o", str(driver_bitcode)]
    compile_hlsc_cmd = [*common_flags, "src/hls_top.cpp", "-o", str(hlsc_bitcode)]
    link_cmd = [llvm_link, str(driver_bitcode), str(hlsc_bitcode), "-o", str(relational_bitcode)]
    timeout_s = int(os.environ.get("C2HLSC_KLEE_TIMEOUT", "60"))
    logs: list[dict[str, object]] = []
    try:
        for command in (compile_driver_cmd, compile_hlsc_cmd, link_cmd):
            completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
            logs.append({"cmd": command, "returncode": completed.returncode, "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]})
        klee_cmd = [klee, f"--output-dir={klee_out}", str(relational_bitcode)]
        executed = subprocess.run(klee_cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout_s, check=False)
        logs.append({"cmd": klee_cmd, "returncode": executed.returncode, "stdout": executed.stdout[-8000:], "stderr": executed.stderr[-8000:]})
        ktests = sorted(str(path.relative_to(ROOT)) for path in klee_out.glob("*.ktest"))
        error_paths = sorted(klee_out.glob("*.err"))
        error_files = [str(path.relative_to(ROOT)) for path in error_paths]
        relational_errors = [path for path in error_paths if path.name.endswith(".c2hlsc_relational.err")]
        contract_errors = [path for path in error_paths if path.name.endswith(".c2hlsc_contract.err")]
        other_errors = [path for path in error_paths if path not in relational_errors and path not in contract_errors]
        counterexamples: list[dict[str, str]] = []
        for path in relational_errors:
            observable = "C2HLSC_RELATIONAL_MISMATCH:unknown"
            marker = "C2HLSC_RELATIONAL_MISMATCH:"
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if marker in line:
                    observable = marker + line.split(marker, 1)[1].split()[0]
                    break
            counterexamples.append({"error_file": str(path.relative_to(ROOT)), "observable": observable})
        path_matches = re.findall(r"completed paths\\s*=\\s*(\\d+)", executed.stdout + "\\n" + executed.stderr, flags=re.IGNORECASE)
        completed_paths = int(path_matches[-1]) if path_matches else 0
        if relational_errors:
            status = "fail"
            outcome = "counterexample"
            failure_kind = "relational_counterexample"
            reason = "KLEE found a golden-C versus HLS-C relational mismatch"
        elif contract_errors:
            status = "blocked"
            outcome = "contract_violation"
            failure_kind = "input_contract_violation"
            reason = "KLEE found a mutation of an input-only buffer; audit the contract and candidate"
        elif other_errors:
            status = "blocked"
            outcome = "execution_error"
            failure_kind = "symbolic_execution_error"
            reason = "KLEE emitted non-relational runtime errors"
        elif executed.returncode != 0:
            status = "blocked"
            outcome = "incomplete"
            failure_kind = "execution_incomplete"
            reason = f"KLEE exited {executed.returncode} before clean completion"
        elif completed_paths <= 0 or not ktests:
            status = "blocked"
            outcome = "incomplete"
            failure_kind = "non_vacuity_missing"
            reason = "KLEE produced no non-vacuous completed-path evidence"
        else:
            status = "pass"
            outcome = "no_counterexample"
            failure_kind = None
            reason = "bounded relational exploration completed without a counterexample"
        write_report({
            "status": status,
            "outcome": outcome,
            "failure_kind": failure_kind,
            "reason": reason,
            "policy_id": "hls_leveri_shift_left_v1",
            "invocations": 1,
            "observable_count": contract.get("observable_count"),
            "assumptions": contract.get("assumptions", {}),
            "bounded_lengths": contract.get("bounded_lengths", {}),
            "scalar_ranges": contract.get("scalar_ranges", {}),
            "commands": logs,
            "completed_paths": completed_paths,
            "generated_tests": len(ktests),
            "ktest_count": len(ktests),
            "ktest_files": ktests,
            "error_files": error_files,
            "counterexamples": counterexamples,
            "counterexample_names": sorted({item["observable"] for item in counterexamples}),
            "timed_out": False,
        })
        print(f"KLEE report written to {REPORT_PATH}")
        return 0 if status == "pass" else 1
    except subprocess.TimeoutExpired as exc:
        logs.append({"cmd": exc.cmd, "timeout_s": timeout_s, "stdout": (exc.stdout or "")[-4000:], "stderr": (exc.stderr or "")[-4000:]})
        write_report({"status": "blocked", "outcome": "incomplete", "failure_kind": "timeout", "reason": "timeout", "completed_paths": 0, "generated_tests": 0, "error_files": [], "counterexamples": [], "counterexample_names": [], "timed_out": True, "commands": logs})
        return 1
    except subprocess.CalledProcessError as exc:
        logs.append({"cmd": exc.cmd, "returncode": exc.returncode, "stdout": (exc.stdout or "")[-4000:], "stderr": (exc.stderr or "")[-4000:]})
        write_report({"status": "blocked", "outcome": "incomplete", "failure_kind": "compile_or_link_failed", "reason": "compile_or_link_failed", "completed_paths": 0, "generated_tests": 0, "error_files": [], "counterexamples": [], "counterexample_names": [], "timed_out": False, "commands": logs})
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _manifest(
    analysis: AnalysisResult, config: AgentConfig, hlsc_source: str | None = None
) -> str:
    fn = analysis.function
    pointer_args = [arg for arg in fn.args if arg.is_pointer_like]
    scalar_ranges = {
        arg.name: list(bounds)
        for arg in fn.args
        if not arg.is_pointer_like and (bounds := _klee_scalar_bounds(arg, pointer_args)) is not None
    }
    bounded_lengths = {
        arg.name: arg.length for arg in pointer_args if arg.length is not None
    }
    observable_count = (1 if fn.return_type != "void" else 0) + sum(
        arg.length or 0 for arg in pointer_args
    )
    payload = {
        "policy_id": LEVERI_TESTBENCH_POLICY_ID,
        "reference_repo": LEVERI_REFERENCE_REPO,
        "top": fn.name,
        "num_tests": config.num_tests,
        "seed": config.seed,
        "checks": [
            "static_header_alignment",
            "static_role_alignment",
            "stimulus_column_alignment",
            "dynamic_output_consistency",
            "gcov_concrete_coverage",
            "klee_golden_hlsc_relational_check",
        ],
        "coverage_hooks": {
            "gcov": {
                "script": "tb/run_gcov.py",
                "report": "coverage/gcov_report.json",
                "make_target": "gcov-coverage",
            },
            "klee": {
                "driver": "tb/klee_driver.cpp",
                "script": "tb/run_klee.py",
                "report": "coverage/klee_report.json",
                "make_target": "klee-coverage",
                "schema": "c2hlsc-klee-report-v1",
                "scope": "golden_hlsc_relational",
                "invocations": 1,
                "observable_count": observable_count,
                "bounded_lengths": bounded_lengths,
                "scalar_ranges": scalar_ranges,
                "assumptions": {
                    "pointer_alias_model": "distinct_pointer_arguments",
                    "hidden_state_model": "no_mutable_hidden_state",
                    "comparison": "return_and_complete_pointer_post_state",
                },
                "unsupported_reasons": _klee_unsupported_reasons(analysis, hlsc_source),
            },
        },
        "generated_files": [
            "tb/leveri_golden_tb.cpp",
            "tb/leveri_hls_tb.cpp",
            "tb/leveri_compare.py",
            "tb/run_gcov.py",
            "tb/klee_driver.cpp",
            "tb/run_klee.py",
            "tb/leveri_manifest.json",
        ],
        "arguments": [
            {
                "name": arg.name,
                "type": arg.c_type,
                "direction": arg.direction,
                "length": arg.length,
                "is_pointer_like": arg.is_pointer_like,
            }
            for arg in fn.args
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


def generate_leveri_testbenches(
    analysis: AnalysisResult,
    config: AgentConfig,
    hlsc_source: str | None = None,
) -> LeVeriTestbenchBundle:
    fn = analysis.function
    golden_include = f"""extern "C" {{
#define restrict __restrict__
#define {fn.name} {fn.name}_ref
#include "../input.c"
#undef {fn.name}
}}"""
    hls_include = '#include "../src/hls_top.hpp"'
    return LeVeriTestbenchBundle(
        golden_tb=_render_trace_tb(
            analysis,
            config,
            target_name=f"{fn.name}_ref",
            output_csv="leveri_golden_trace.csv",
            include_block=golden_include,
        ),
        hls_tb=_render_trace_tb(
            analysis,
            config,
            target_name=fn.name,
            output_csv="leveri_hls_trace.csv",
            include_block=hls_include,
        ),
        compare_script=_compare_script(),
        gcov_script=_gcov_script(),
        klee_driver=_klee_driver(analysis),
        klee_script=_klee_script(),
        manifest_json=_manifest(analysis, config, hlsc_source),
    )
