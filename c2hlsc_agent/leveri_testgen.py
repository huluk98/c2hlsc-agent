from __future__ import annotations

import json
from dataclasses import dataclass

from .analyze import AnalysisResult, FunctionArg, active_length_arg
from .config import AgentConfig
from .stimulus import (
    directed_index_decl,
    directed_schedule,
    directed_var,
    extra_guard,
    extra_vectors,
    render_extra_tables,
    render_helpers,
    total_iterations,
)


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
- Generate a KLEE symbolic driver for the golden C top when KLEE is available.
- Treat generated traces as evidence, not proof; host equivalence, CSim, CSynth, and CoSim still decide acceptance.
- Record enough metadata for a future HLS verification knowledge graph: function name, arguments, directions, lengths, generated files, and check types.
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


def _scalar_decl(arg: FunctionArg, config: AgentConfig) -> str:
    slot = directed_var(config, "cycle")
    if arg.scalar_range:
        lo, hi = arg.scalar_range
        base = f"bounded_scalar<{arg.c_type}>({slot}, rng, {lo}LL, {hi}LL)"
    else:
        base = f"random_value<{arg.c_type}>(rng)"
    guard = extra_guard(config, "cycle")
    if not guard:
        return base
    return f"({guard}) ? static_cast<{arg.c_type}>(c2hlsc_extra_{arg.name}[cycle]) : {base}"


def _init_array(arg: FunctionArg, config: AgentConfig) -> str:
    storage = _storage_type(arg)
    if arg.direction == "output":
        return f"""for (int i = 0; i < {arg.length}; ++i) {{
    {arg.name}[i] = output_sentinel<{storage}>(cycle, i);
  }}"""
    unsigned = "true" if _is_unsigned(arg.c_type) else "false"
    slot = directed_var(config, "cycle")
    patterned = f"""for (int i = 0; i < {arg.length}; ++i) {{
    {arg.name}[i] = patterned_value<{storage}>({slot}, i, rng, {unsigned});
  }}"""
    guard = extra_guard(config, "cycle")
    if not guard:
        return patterned
    return f"""if ({guard}) {{
    for (int i = 0; i < {arg.length}; ++i) {{
      {arg.name}[i] = static_cast<{storage}>(c2hlsc_extra_{arg.name}[cycle][i]);
    }}
  }} else {{
    {patterned}
  }}"""


def _call_args(args: list[FunctionArg]) -> str:
    return ", ".join(arg.name for arg in args)


def _columns(function_args: list[FunctionArg], return_type: str) -> list[dict[str, object]]:
    """The trace schema: one entry per CSV column, in order.

    Output columns of an array that has an *active length* scalar carry the column index
    of that scalar, so the comparator can clamp exactly the way the oracle testbench's
    ``clamp_count`` does. Without it a design that leaves the tail of a buffer untouched
    would be reported as a behavioural mismatch even though the tail is outside the
    declared contract.
    """

    scalars = [arg for arg in function_args if not arg.is_pointer_like]
    columns: list[dict[str, object]] = [{"name": "cycle", "role": "meta"}]
    scalar_column: dict[str, int] = {}

    # Scalars are emitted in argument order, interleaved with the arrays; resolve their
    # column indices in a first pass so an array column can point at one.
    index = 1
    for arg in function_args:
        if arg.is_pointer_like:
            if arg.direction in {"input", "inout"}:
                index += arg.length or 0
            if arg.direction in {"output", "inout"}:
                index += arg.length or 0
        else:
            scalar_column[arg.name] = index
            index += 1

    for arg in function_args:
        if arg.is_pointer_like:
            active = active_length_arg(arg, scalars)
            if arg.direction in {"input", "inout"}:
                suffix = "_in" if arg.direction == "inout" else ""
                for idx in range(arg.length or 0):
                    columns.append(
                        {"name": f"{arg.name}{suffix}[{idx}]", "role": "in", "arg": arg.name, "element": idx}
                    )
            if arg.direction in {"output", "inout"}:
                suffix = "_out" if arg.direction == "inout" else ""
                for idx in range(arg.length or 0):
                    column: dict[str, object] = {
                        "name": f"{arg.name}{suffix}[{idx}]",
                        "role": "out",
                        "arg": arg.name,
                        "element": idx,
                        "declared_length": arg.length,
                    }
                    if active is not None:
                        column["active_length_column"] = scalar_column[active.name]
                        column["active_length_arg"] = active.name
                    columns.append(column)
        else:
            columns.append({"name": arg.name, "role": "in", "arg": arg.name})
    if return_type != "void":
        columns.append({"name": "return", "role": "out", "arg": "return"})
    return columns


def _header_and_roles(function_args: list[FunctionArg], return_type: str) -> tuple[list[str], list[str]]:
    columns = _columns(function_args, return_type)
    return [str(c["name"]) for c in columns], [str(c["role"]) for c in columns]


def _write_header_line(items: list[str]) -> str:
    return "  trace << " + json.dumps(",".join(items) + "\n") + ";"


def _array_declarations(arrays: list[FunctionArg]) -> list[str]:
    declarations: list[str] = []
    for arg in arrays:
        declarations.append(f"  {_storage_type(arg)} {arg.name}[{arg.length}] = {{}};")
    return declarations


def _scalar_declarations(scalars: list[FunctionArg], config: AgentConfig) -> list[str]:
    return [f"  {arg.c_type} {arg.name} = {_scalar_decl(arg, config)};" for arg in scalars]


def _array_initializers(arrays: list[FunctionArg], config: AgentConfig) -> list[str]:
    return [_init_array(arg, config) for arg in arrays]


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


def _trace_helpers(seed: int) -> str:
    """Trace-specific helpers. The stimulus templates come from :mod:`stimulus` so the
    oracle testbench and both trace testbenches share one schedule and one random stream."""

    return f"""template <typename T>
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
    declarations = _array_declarations(arrays) + _scalar_declarations(scalars, config)
    initializers = _array_initializers(arrays, config)
    row_lines = _write_row_lines(fn.args, fn.return_type)
    return_prefix = f"{fn.return_type} dut_return = " if fn.return_type != "void" else ""

    vectors = extra_vectors(config)
    stimulus_helpers = render_helpers(config, "cycle") + "\n" + _trace_helpers(config.seed)
    extra_tables = render_extra_tables(fn.args, vectors)
    directed_decl = directed_index_decl(config, "cycle")
    iterations = total_iterations(config)

    return f"""// Generated by c2hlsc_agent using {LEVERI_TESTBENCH_POLICY_ID}.
// LeVeri-style paired trace testbench: emits one CSV trace for dual-tier checking.
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <random>

{include_block}

{extra_tables}{stimulus_helpers}

int main() {{
  std::ofstream trace({json.dumps(output_csv)}, std::ofstream::out);
  if (!trace.is_open()) {{
    std::cerr << "failed to open {output_csv}\\n";
    return 1;
  }}
{_write_header_line(headers)}
{_write_header_line(roles)}

  std::mt19937_64 rng = make_trace_rng();
  for (int cycle = 0; cycle < {iterations}; ++cycle) {{
{directed_decl}
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
    return r"""#!/usr/bin/env python3
'''HLS-LeVeri dual-tier consistency check.

Tier 1 -- STATIC structural alignment between the two generated testbenches:
  * trace schema: header row, role row, cycle count
  * input stimulus: every 'in' column must be identical on both sides
  * control flow: the harnesses must have the same CFG shape
  * data dependency: the harnesses must have the same def-use structure

Tier 2 -- DYNAMIC behavioural consistency: every 'out' column must agree, clamped to the
declared active length when the contract has one.

The static tier is what separates a TESTBENCH bug from a DESIGN bug. Both harnesses come
from one template, so it passes by construction today; it exists so that the day someone
(or a model) augments one side's stimulus and not the other, the divergence is reported
as a harness defect instead of being silently blamed on the design.
'''
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tb" / "leveri_manifest.json"

CONTROL_KEYWORDS = (
    "if", "else", "for", "while", "do", "switch", "case",
    "default", "return", "break", "continue", "goto",
)

ASSIGNMENT = re.compile(
    r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:=(?!=)|\+=|-=|\*=|/=|%=|\|=|&=|\^=)\s*([^;]*);"
)


def read_trace(path: Path) -> tuple[list[str], list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 2:
        raise SystemExit(f"{path}: expected header row, role row, and trace data")
    return rows[0], rows[1], rows[2:]


def fail(tier: str, message: str) -> None:
    print(f"HLS-LeVeri {tier} check failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# Tier 1: static structural alignment
# --------------------------------------------------------------------------- #


def normalize_source(text: str, top: str) -> str:
    '''Strip everything the two harnesses are SUPPOSED to differ in.

    By design the golden side includes the original C with the top macro-renamed and the
    HLS side includes the generated header, and each writes its own CSV. Those are
    contract differences, not structural ones, so preprocessor lines, string literals and
    the extern "C" include block are removed and the _ref suffix is folded away before
    anything is compared.
    '''
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    text = re.sub(r'extern\s+""\s*\{[^{}]*\}', " ", text)
    if top:
        text = re.sub(r"\b" + re.escape(top) + r"_ref\b", top, text)
    return text


def cfg_signature(text: str) -> list[str]:
    '''Control-flow shape: every control keyword tagged with its brace depth.'''
    signature: list[str] = []
    depth = 0
    for token in re.finditer(r"[{}]|[A-Za-z_]\w*", text):
        value = token.group(0)
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
        elif value in CONTROL_KEYWORDS:
            signature.append(f"{value}@{depth}")
    return signature


def ddg_signature(text: str) -> list[str]:
    '''Def-use structure: each assignment as target <- the sorted set of names read.'''
    pairs = set()
    for match in ASSIGNMENT.finditer(text):
        target = match.group(1)
        reads = sorted(set(re.findall(r"[A-Za-z_]\w*", match.group(2))))
        pairs.add(target + "<-" + ",".join(reads))
    return sorted(pairs)


def first_divergence(left: list[str], right: list[str]) -> str:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return f"position {index}: golden={a!r} hls={b!r}"
    if len(left) != len(right):
        shared = min(len(left), len(right))
        longer, name = (left, "golden") if len(left) > len(right) else (right, "hls")
        return f"{name} has {abs(len(left) - len(right))} extra entr(ies), first extra {longer[shared]!r}"
    return "unknown"


def static_structural_check(manifest: dict) -> dict:
    top = str(manifest.get("top", ""))
    golden_path = ROOT / "tb" / "leveri_golden_tb.cpp"
    hls_path = ROOT / "tb" / "leveri_hls_tb.cpp"
    if not golden_path.exists() or not hls_path.exists():
        print("static structural check skipped: testbench sources not found", file=sys.stderr)
        return {"cfg": None, "ddg": None}

    golden = normalize_source(golden_path.read_text(encoding="utf-8"), top)
    hls = normalize_source(hls_path.read_text(encoding="utf-8"), top)

    golden_cfg, hls_cfg = cfg_signature(golden), cfg_signature(hls)
    if golden_cfg != hls_cfg:
        fail(
            "static control-flow",
            "paired testbenches have different CFG shapes; " + first_divergence(golden_cfg, hls_cfg),
        )

    golden_ddg, hls_ddg = ddg_signature(golden), ddg_signature(hls)
    if golden_ddg != hls_ddg:
        fail(
            "static data-dependency",
            "paired testbenches have different def-use structures; " + first_divergence(golden_ddg, hls_ddg),
        )

    return {"cfg": len(golden_cfg), "ddg": len(golden_ddg)}


# --------------------------------------------------------------------------- #
# Tier 2: dynamic behavioural consistency
# --------------------------------------------------------------------------- #


def values_match(golden: str, hls: str) -> bool:
    if golden == hls:
        return True
    try:
        gf = float(golden)
        hf = float(hls)
    except ValueError:
        return False
    diff = abs(gf - hf)
    scale = max(abs(gf), abs(hf), 1.0)
    return diff <= 1e-6 * scale


def clamp(value: str, limit: int) -> int:
    try:
        number = int(float(value))
    except ValueError:
        return limit
    if number < 0:
        return 0
    return min(number, limit)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: leveri_compare.py GOLDEN_TRACE.csv HLS_TRACE.csv", file=sys.stderr)
        return 2

    manifest = load_manifest()
    columns = manifest.get("columns") or []
    static_stats = static_structural_check(manifest)

    golden_header, golden_roles, golden_rows = read_trace(Path(argv[1]))
    hls_header, hls_roles, hls_rows = read_trace(Path(argv[2]))

    if golden_header != hls_header:
        fail("static schema", "header mismatch")
    if golden_roles != hls_roles:
        fail("static schema", "role-row mismatch")
    if len(golden_rows) != len(hls_rows):
        fail("static schema", f"cycle-count mismatch golden={len(golden_rows)} hls={len(hls_rows)}")

    input_columns = [idx for idx, role in enumerate(golden_roles) if role == "in"]
    output_columns = [idx for idx, role in enumerate(golden_roles) if role == "out"]
    clamped = 0

    for row_idx, (golden, hls) in enumerate(zip(golden_rows, hls_rows)):
        if len(golden) != len(golden_header) or len(hls) != len(hls_header):
            fail("static schema", f"row width mismatch at trace row {row_idx}")
        for col_idx in input_columns:
            if golden[col_idx] != hls[col_idx]:
                fail(
                    "static stimulus",
                    f"stimulus mismatch cycle={golden[0]} column={golden_header[col_idx]} "
                    f"golden={golden[col_idx]} hls={hls[col_idx]}",
                )
        for col_idx in output_columns:
            meta = columns[col_idx] if col_idx < len(columns) else {}
            active_col = meta.get("active_length_column")
            element = meta.get("element")
            if isinstance(active_col, int) and isinstance(element, int) and active_col < len(golden):
                limit = clamp(golden[active_col], int(meta.get("declared_length") or 0))
                if element >= limit:
                    clamped += 1
                    continue  # outside the declared active length: not part of the contract
            if not values_match(golden[col_idx], hls[col_idx]):
                fail(
                    "dynamic behaviour",
                    f"behaviour mismatch cycle={golden[0]} column={golden_header[col_idx]} "
                    f"expected={golden[col_idx]} actual={hls[col_idx]}",
                )

    notes = []
    if static_stats.get("cfg") is not None:
        notes.append(f"CFG {static_stats['cfg']} node(s), def-use {static_stats['ddg']} edge(s)")
    if clamped:
        notes.append(f"{clamped} element comparison(s) clamped to the active length")
    suffix = (", " + ", ".join(notes)) if notes else ""
    print(
        "HLS-LeVeri dual-tier consistency check passed: "
        f"{len(golden_rows)} cycles, {len(input_columns)} input columns, "
        f"{len(output_columns)} output columns" + suffix
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
"""


def _klee_driver(analysis: AnalysisResult) -> str:
    fn = analysis.function
    declarations: list[str] = []
    setup: list[str] = []
    for arg in fn.args:
        if arg.is_pointer_like:
            declarations.append(f"  {_storage_type(arg)} {arg.name}[{arg.length}] = {{}};")
            if arg.direction in {"input", "inout"}:
                setup.append(f'  klee_make_symbolic({arg.name}, sizeof({arg.name}), "{arg.name}");')
            else:
                setup.append(f"  for (int i = 0; i < {arg.length}; ++i) {arg.name}[i] = static_cast<{_storage_type(arg)}>(0);")
        else:
            scalar_type = _storage_type(arg)
            declarations.append(f"  {scalar_type} {arg.name} = 0;")
            setup.append(f'  klee_make_symbolic(&{arg.name}, sizeof({arg.name}), "{arg.name}");')
            if arg.scalar_range:
                lo, hi = arg.scalar_range
                setup.append(f"  klee_assume({arg.name} >= static_cast<{scalar_type}>({lo}));")
                setup.append(f"  klee_assume({arg.name} <= static_cast<{scalar_type}>({hi}));")
    return_prefix = f"{fn.return_type} dut_return = " if fn.return_type != "void" else ""
    if fn.return_type != "void":
        setup.append("  (void)dut_return;")

    return f"""// Generated by c2hlsc_agent using {LEVERI_TESTBENCH_POLICY_ID}.
// KLEE symbolic driver for the golden C top function.
#include <cstdint>
#include <klee/klee.h>

extern "C" {{
#define {fn.name} {fn.name}_ref
#include "../input.c"
#undef {fn.name}
}}

int main() {{
{chr(10).join(declarations)}
{chr(10).join(setup[:-1] if fn.return_type != "void" else setup)}
  {return_prefix}{fn.name}_ref({_call_args(fn.args)});
{setup[-1] if fn.return_type != "void" else ""}
  return 0;
}}
"""


def _gcov_script() -> str:
    return """#!/usr/bin/env python3
\"\"\"Concrete structural coverage for the shift-left tier.

Builds both trace testbenches with --coverage, runs them, runs the dual-tier comparison,
then invokes gcov and PARSES the result: line and branch coverage over the measured
targets (the golden C and the generated HLS-C, never the testbenches themselves), plus
the exact uncovered lines and branches. Those uncovered sites are what the refinement
loop in c2hlsc_agent/coverage_refine.py steers KLEE toward, so producing a number is the
whole point of this target -- an unparsed .gcov file cannot close a loop.
\"\"\"
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COVERAGE_DIR = ROOT / "coverage"
REPORT_PATH = COVERAGE_DIR / "gcov_report.json"

# Coverage is measured over the specification and the design, not the harness that
# drives them: a testbench is 100% covered by construction and would mask the real number.
MEASURED_SUFFIXES = ("input.c", "src/hls_top.cpp", "hls_top.cpp")


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=check)


def write_report(payload: dict[str, object]) -> None:
    COVERAGE_DIR.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, indent=2) + "\\n", encoding="utf-8")


def tool(name: str) -> str | None:
    return shutil.which(name)


def is_measured(source: str) -> bool:
    normalized = source.replace("\\\\", "/")
    return any(normalized.endswith(suffix) for suffix in MEASURED_SUFFIXES)


def parse_gcov(path: Path) -> dict[str, object] | None:
    \"\"\"Parse one .gcov file into per-file line/branch counts and uncovered sites.

    gcov line format is 'count:lineno:source'. A count of '-' marks a non-executable
    line, '#####' and '=====' mark executable-but-never-executed. With -b, branch rows
    follow the line they belong to, so the current line number attributes them.
    \"\"\"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    source = None
    lines_total = lines_hit = 0
    branches_total = branches_hit = 0
    uncovered_lines: list[int] = []
    uncovered_branches: list[dict[str, int]] = []
    current_line = 0

    for raw in text.splitlines():
        if raw.startswith("branch "):
            branches_total += 1
            match = re.match(r"branch\\s+(\\d+)", raw)
            index = int(match.group(1)) if match else 0
            # gcov -c prints branch COUNTS ("taken 0"), plain gcov prints PERCENTAGES
            # ("taken 0%"). Both mean the same thing at zero; match either, or the
            # never-taken branch silently counts as covered and the number is a lie.
            taken = re.search(r"taken\\s+(\\d+)", raw)
            never = "never executed" in raw
            if never or (taken is not None and int(taken.group(1)) == 0):
                uncovered_branches.append({"line": current_line, "branch": index})
            else:
                branches_hit += 1
            continue
        parts = raw.split(":", 2)
        if len(parts) < 3:
            continue
        count = parts[0].strip()
        number = parts[1].strip()
        if number == "0":
            if parts[2].startswith("Source:"):
                source = parts[2][len("Source:"):].strip()
            continue
        try:
            current_line = int(number)
        except ValueError:
            continue
        if count == "-":
            continue  # not executable
        lines_total += 1
        if count in {"#####", "====="}:
            uncovered_lines.append(current_line)
        else:
            lines_hit += 1

    if source is None:
        return None
    return {
        "source": source,
        "gcov_file": str(path.relative_to(ROOT)),
        "lines_total": lines_total,
        "lines_hit": lines_hit,
        "branches_total": branches_total,
        "branches_hit": branches_hit,
        "uncovered_lines": uncovered_lines,
        "uncovered_branches": uncovered_branches,
    }


def percent(hit: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(100.0 * hit / total, 2)


def main() -> int:
    cxx = os.environ.get("CXX", "g++")
    gcov = os.environ.get("GCOV", "gcov")
    if tool(cxx) is None or tool(gcov) is None:
        write_report({
            "status": "skipped",
            "reason": "CXX or gcov not found",
            "cxx": cxx,
            "gcov": gcov,
            "remedy": "c2hlsc-agent doctor --install",
        })
        print("gcov coverage skipped: CXX or gcov not found -- run `c2hlsc-agent doctor --install` to install it")
        return 0

    COVERAGE_DIR.mkdir(exist_ok=True)
    for pattern in ("*.gcda", "*.gcno", "*.gcov"):
        for path in ROOT.rglob(pattern):
            path.unlink()

    flags = ["-std=c++17", "-Wall", "-Wextra", "-I", "src", "-O0", "--coverage"]
    extra = os.environ.get("C2HLSC_GCOV_CXXFLAGS", "").split()
    # Compile to objects under coverage/ first, then link. A one-step multi-source build
    # makes gcc name the notes files <output>-<source>.gcno, which gcov then cannot find
    # for src/hls_top.cpp -- the design would silently drop out of the coverage number.
    commands = [
        [cxx, *flags, *extra, "-c", "tb/leveri_golden_tb.cpp", "-o", "coverage/leveri_golden_tb.o"],
        [cxx, *flags, *extra, "coverage/leveri_golden_tb.o", "-o", "coverage/leveri_golden_tb"],
        [cxx, *flags, *extra, "-c", "tb/leveri_hls_tb.cpp", "-o", "coverage/leveri_hls_tb.o"],
        [cxx, *flags, *extra, "-c", "src/hls_top.cpp", "-o", "coverage/hls_top.o"],
        [cxx, *flags, *extra, "coverage/leveri_hls_tb.o", "coverage/hls_top.o", "-o", "coverage/leveri_hls_tb"],
        ["coverage/leveri_golden_tb"],
        ["coverage/leveri_hls_tb"],
        [sys.executable, "tb/leveri_compare.py", "leveri_golden_trace.csv", "leveri_hls_trace.csv"],
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

    gcov_cmd = [gcov, "-b", "-c", "-o", str(COVERAGE_DIR), "tb/leveri_golden_tb.cpp", "tb/leveri_hls_tb.cpp", "src/hls_top.cpp"]
    gcov_result = run(gcov_cmd, check=False)
    gcov_files = sorted(ROOT.rglob("*.gcov"))
    coverage_data = sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*.gcda"))

    # Keep the report to project sources; standard-library headers pulled in by the
    # harness are noise and are never part of the coverage claim.
    parsed = [
        entry
        for entry in (parse_gcov(path) for path in gcov_files)
        if entry and not str(entry["source"]).startswith("/")
    ]
    measured = [entry for entry in parsed if is_measured(str(entry["source"]))]
    lines_total = sum(int(entry["lines_total"]) for entry in measured)
    lines_hit = sum(int(entry["lines_hit"]) for entry in measured)
    branches_total = sum(int(entry["branches_total"]) for entry in measured)
    branches_hit = sum(int(entry["branches_hit"]) for entry in measured)
    uncovered_lines = [
        {"file": entry["source"], "line": line}
        for entry in measured
        for line in entry["uncovered_lines"]
    ]
    uncovered_branches = [
        {"file": entry["source"], "line": item["line"], "branch": item["branch"]}
        for entry in measured
        for item in entry["uncovered_branches"]
    ]
    line_coverage = percent(lines_hit, lines_total)
    branch_coverage = percent(branches_hit, branches_total)

    # The build, execution, and dual-tier comparison all succeeded above, so coverage
    # correctness is already established. This target's job is to PRODUCE a coverage
    # report; pass when instrumentation actually emitted data. gcov's own exit code is
    # advisory only -- it varies across gcov/compiler versions and platforms for
    # source-path resolution -- so it does not gate the result.
    produced_coverage = bool(coverage_data or gcov_files)
    status = "pass" if produced_coverage else "fail"

    # An explicit target turns coverage into a gate; without one, coverage is evidence.
    raw_min = os.environ.get("C2HLSC_MIN_COVERAGE", "").strip()
    min_coverage = None
    if raw_min:
        try:
            min_coverage = float(raw_min)
        except ValueError:
            min_coverage = None
    # Gate on the WEAKER of line and branch coverage. Line coverage alone is easy to
    # saturate while a whole branch stays unreached, which is exactly the case the
    # refinement loop exists to find.
    observed = [value for value in (line_coverage, branch_coverage) if value is not None]
    gate_value = min(observed) if observed else None
    below_target = (
        status == "pass"
        and min_coverage is not None
        and gate_value is not None
        and gate_value < min_coverage
    )
    if below_target:
        status = "fail"

    write_report({
        "status": status,
        "policy_id": "hls_leveri_shift_left_v1",
        "line_coverage": line_coverage,
        "branch_coverage": branch_coverage,
        "lines_total": lines_total,
        "lines_hit": lines_hit,
        "branches_total": branches_total,
        "branches_hit": branches_hit,
        "min_coverage": min_coverage,
        "gate_coverage": gate_value,
        "below_target": below_target,
        "measured_files": [str(entry["source"]) for entry in measured],
        "uncovered_lines": uncovered_lines,
        "uncovered_branches": uncovered_branches,
        "files": parsed,
        "commands": command_logs,
        "gcov_cmd": gcov_cmd,
        "gcov_returncode": gcov_result.returncode,
        "gcov_stdout": gcov_result.stdout[-8000:],
        "gcov_stderr": gcov_result.stderr[-8000:],
        "gcov_files": [str(path.relative_to(ROOT)) for path in gcov_files],
        "coverage_data": coverage_data,
    })
    summary = "lines n/a" if line_coverage is None else f"lines {line_coverage:.2f}%"
    if branch_coverage is not None:
        summary += f", branches {branch_coverage:.2f}%"
    if below_target:
        summary += f" (below C2HLSC_MIN_COVERAGE={min_coverage})"
    print(f"gcov coverage: {summary}; report written to {REPORT_PATH}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _klee_script() -> str:
    return r"""#!/usr/bin/env python3
'''Symbolic exploration of the golden C top.

Runs KLEE natively when it is installed. Where it is not -- macOS has no Homebrew
formula and Ubuntu does not package it either -- this falls back to the official
klee/klee container automatically, so `make klee-coverage` and the refinement loop work
on a Mac without anything being installed into the host PATH.

  C2HLSC_KLEE_DOCKER=0   never use the container, skip instead
  C2HLSC_KLEE_DOCKER=1   use the container even if a native klee exists
  C2HLSC_KLEE_IMAGE=...  override the image (default klee/klee:latest)
'''
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COVERAGE_DIR = ROOT / "coverage"
REPORT_PATH = COVERAGE_DIR / "klee_report.json"
DEFAULT_IMAGE = "klee/klee:latest"
DOCTOR = "c2hlsc-agent doctor --install"


def write_report(payload: dict) -> None:
    COVERAGE_DIR.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


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


def docker_available() -> tuple[bool, str]:
    '''The CLI existing is not enough -- a Mac with Docker Desktop closed has the binary
    but no daemon, which would otherwise fail with an unhelpful socket error.'''
    if shutil.which("docker") is None:
        return False, "docker not installed"
    try:
        probe = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker not usable: {exc}"
    if probe.returncode != 0:
        return False, "docker daemon is not running (start Docker Desktop)"
    return True, ""


CONTAINER_SCRIPT = r'''
set -e
INC=""
for c in /home/klee/klee_src/include /usr/local/include /usr/include; do
  if [ -f "$c/klee/klee.h" ]; then INC="$c"; break; fi
done
if [ -z "$INC" ]; then echo "klee headers not found inside the image" >&2; exit 3; fi
CXX=clang++
command -v clang++ >/dev/null 2>&1 || CXX="clang -x c++"
mkdir -p coverage
$CXX -std=c++17 -I . -I "$INC" -emit-llvm -c -g -O0 tb/klee_driver.cpp -o coverage/klee_driver.bc
klee --output-dir=coverage/klee-out coverage/klee_driver.bc
'''


def run_in_docker(timeout_s: int) -> int:
    image = os.environ.get("C2HLSC_KLEE_IMAGE", DEFAULT_IMAGE)
    klee_out = COVERAGE_DIR / "klee-out"
    if klee_out.exists():
        shutil.rmtree(klee_out)
    COVERAGE_DIR.mkdir(exist_ok=True)

    command = ["docker", "run", "--rm", "-v", f"{ROOT}:/work", "-w", "/work"]
    if platform.system() == "Linux":
        # Bind mounts on Linux keep the container's uid, so without this every artifact
        # KLEE writes would land root-owned in the user's project.
        command += ["-u", f"{os.getuid()}:{os.getgid()}"]
    command += [image, "bash", "-c", CONTAINER_SCRIPT]

    logs: list[dict] = []
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        write_report({
            "status": "fail",
            "mode": "docker",
            "image": image,
            "reason": "timeout",
            "timeout_s": timeout_s,
            "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
        })
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        write_report({"status": "fail", "mode": "docker", "image": image, "reason": str(exc)})
        return 1

    logs.append({
        "cmd": command[:-1] + ["<script>"],
        "returncode": result.returncode,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-8000:],
    })
    ktests = sorted(str(path.relative_to(ROOT)) for path in klee_out.glob("*.ktest")) if klee_out.exists() else []
    status = "pass" if result.returncode == 0 else "fail"
    write_report({
        "status": status,
        "mode": "docker",
        "image": image,
        "policy_id": "hls_leveri_shift_left_v1",
        "commands": logs,
        "ktest_count": len(ktests),
        "ktest_files": ktests,
    })
    print(f"KLEE ({image}) report written to {REPORT_PATH}")
    return result.returncode


def main() -> int:
    forced = os.environ.get("C2HLSC_KLEE_DOCKER", "").strip()
    timeout_s = int(os.environ.get("C2HLSC_KLEE_TIMEOUT", "60"))

    klee = resolve_tool("KLEE", "klee")
    clangxx = resolve_tool("KLEE_CXX", "klee-clang++", "clang++")
    klee_include = os.environ.get("KLEE_INCLUDE_DIR") or default_klee_include(klee)
    native_ready = bool(klee and clangxx and klee_include and Path(klee_include).exists())

    if forced == "1" or (not native_ready and forced != "0"):
        ok, reason = docker_available()
        if ok:
            return run_in_docker(timeout_s)
        if forced == "1":
            write_report({"status": "skipped", "mode": "docker", "reason": reason, "remedy": DOCTOR})
            print(f"KLEE coverage skipped: {reason}")
            return 0
        if not native_ready:
            missing = "klee not found" if not klee else ("clang++ not found" if not clangxx else "KLEE include directory not found")
            write_report({
                "status": "skipped",
                "mode": "none",
                "reason": missing,
                "docker_reason": reason,
                "remedy": DOCTOR,
            })
            print(f"KLEE coverage skipped: {missing}; container fallback unavailable ({reason}) -- {DOCTOR}")
            return 0

    COVERAGE_DIR.mkdir(exist_ok=True)
    bitcode = COVERAGE_DIR / "klee_driver.bc"
    klee_out = COVERAGE_DIR / "klee-out"
    if klee_out.exists():
        shutil.rmtree(klee_out)

    compile_cmd = [
        clangxx,
        "-std=c++17",
        "-I",
        ".",
        "-I",
        klee_include,
        "-emit-llvm",
        "-c",
        "-g",
        "-O0",
        "tb/klee_driver.cpp",
        "-o",
        str(bitcode),
    ]
    logs: list[dict] = []
    try:
        compiled = subprocess.run(compile_cmd, cwd=ROOT, text=True, capture_output=True, check=True)
        logs.append({"cmd": compile_cmd, "returncode": compiled.returncode, "stdout": compiled.stdout[-4000:], "stderr": compiled.stderr[-4000:]})
        klee_cmd = [klee, f"--output-dir={klee_out}", str(bitcode)]
        executed = subprocess.run(klee_cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout_s, check=False)
        logs.append({"cmd": klee_cmd, "returncode": executed.returncode, "stdout": executed.stdout[-8000:], "stderr": executed.stderr[-8000:]})
        ktests = sorted(str(path.relative_to(ROOT)) for path in klee_out.glob("*.ktest"))
        status = "pass" if executed.returncode == 0 else "fail"
        write_report({
            "status": status,
            "mode": "native",
            "policy_id": "hls_leveri_shift_left_v1",
            "commands": logs,
            "ktest_count": len(ktests),
            "ktest_files": ktests,
        })
        print(f"KLEE report written to {REPORT_PATH}")
        return executed.returncode
    except subprocess.TimeoutExpired as exc:
        logs.append({"cmd": exc.cmd, "timeout_s": timeout_s, "stdout": (exc.stdout or "")[-4000:], "stderr": (exc.stderr or "")[-4000:]})
        write_report({"status": "fail", "mode": "native", "reason": "timeout", "commands": logs})
        return 1
    except subprocess.CalledProcessError as exc:
        logs.append({"cmd": exc.cmd, "returncode": exc.returncode, "stdout": (exc.stdout or "")[-4000:], "stderr": (exc.stderr or "")[-4000:]})
        write_report({"status": "fail", "mode": "native", "reason": "compile_failed", "commands": logs})
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _manifest(analysis: AnalysisResult, config: AgentConfig) -> str:
    fn = analysis.function
    payload = {
        "policy_id": LEVERI_TESTBENCH_POLICY_ID,
        "reference_repo": LEVERI_REFERENCE_REPO,
        "top": fn.name,
        "num_tests": config.num_tests,
        "seed": config.seed,
        "checks": [
            "static_header_alignment",
            "static_role_alignment",
            "static_cycle_count_alignment",
            "stimulus_column_alignment",
            "static_control_flow_alignment",
            "static_data_dependency_alignment",
            "dynamic_output_consistency",
            "gcov_concrete_coverage",
            "klee_symbolic_path_exploration",
            "coverage_driven_refinement",
        ],
        "directed_tests": directed_schedule(config),
        "extra_vectors": [vector.to_dict() for vector in extra_vectors(config)],
        "columns": _columns(fn.args, fn.return_type),
        "testbench_sources": ["tb/leveri_golden_tb.cpp", "tb/leveri_hls_tb.cpp"],
        "traces": ["leveri_golden_trace.csv", "leveri_hls_trace.csv"],
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
            },
        },
        "refinement": {
            "script": "c2hlsc_agent/coverage_refine.py",
            "command": "c2hlsc-agent refine --project .",
            "make_target": "refine-coverage",
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


def generate_leveri_testbenches(analysis: AnalysisResult, config: AgentConfig) -> LeVeriTestbenchBundle:
    fn = analysis.function
    golden_include = f"""extern "C" {{
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
        manifest_json=_manifest(analysis, config),
    )
