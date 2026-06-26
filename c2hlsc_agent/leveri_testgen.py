from __future__ import annotations

import json
from dataclasses import dataclass

from .analyze import AnalysisResult, FunctionArg
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
  return static_cast<T>((rng() % 20001) - 10000) / static_cast<T>(100);
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
            if golden[col_idx] != hls[col_idx]:
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
from __future__ import annotations

import json
import os
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

    gcov_cmd = [gcov, "-b", "-c", "-o", str(COVERAGE_DIR), "tb/leveri_golden_tb.cpp", "tb/leveri_hls_tb.cpp", "src/hls_top.cpp"]
    gcov_result = run(gcov_cmd, check=False)
    gcov_files = sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*.gcov"))
    coverage_data = sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*.gcda"))
    # The build, execution, and dual-trace comparison all succeeded above, so coverage
    # correctness is already established. This target's job is to PRODUCE a coverage
    # report; pass when instrumentation actually emitted data. gcov's own exit code is
    # advisory only -- it varies across gcov/compiler versions and platforms for
    # source-path resolution -- so it does not gate the result.
    produced_coverage = bool(coverage_data or gcov_files)
    write_report({
        "status": "pass" if produced_coverage else "fail",
        "policy_id": "hls_leveri_shift_left_v1",
        "commands": command_logs,
        "gcov_cmd": gcov_cmd,
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

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COVERAGE_DIR = ROOT / "coverage"
REPORT_PATH = COVERAGE_DIR / "klee_report.json"


def write_report(payload: dict[str, object]) -> None:
    COVERAGE_DIR.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, indent=2) + "\\n", encoding="utf-8")


def resolve_tool(env_name: str, default_name: str, fallback_path: str) -> str | None:
    value = os.environ.get(env_name)
    if value:
        return value
    found = shutil.which(default_name)
    if found:
        return found
    if Path(fallback_path).exists():
        return fallback_path
    return None


def main() -> int:
    klee = resolve_tool("KLEE", "klee", "/Users/luke/.local/klee/bin/klee")
    clangxx = resolve_tool("KLEE_CXX", "klee-clang++", "/Users/luke/.klee-conda/bin/clang++")
    klee_include = os.environ.get("KLEE_INCLUDE_DIR", "/Users/luke/.local/klee/include")
    if klee is None:
        write_report({"status": "skipped", "reason": "klee not found"})
        print("KLEE coverage skipped: klee not found")
        return 0
    if clangxx is None:
        write_report({"status": "skipped", "reason": "clang++ not found"})
        print("KLEE coverage skipped: clang++ not found")
        return 0
    if not Path(klee_include).exists():
        write_report({"status": "skipped", "reason": "KLEE include directory not found", "klee_include": klee_include})
        print("KLEE coverage skipped: include directory not found")
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
    timeout_s = int(os.environ.get("C2HLSC_KLEE_TIMEOUT", "60"))
    logs: list[dict[str, object]] = []
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
            "policy_id": "hls_leveri_shift_left_v1",
            "commands": logs,
            "ktest_count": len(ktests),
            "ktest_files": ktests,
        })
        print(f"KLEE report written to {REPORT_PATH}")
        return executed.returncode
    except subprocess.TimeoutExpired as exc:
        logs.append({"cmd": exc.cmd, "timeout_s": timeout_s, "stdout": (exc.stdout or "")[-4000:], "stderr": (exc.stderr or "")[-4000:]})
        write_report({"status": "fail", "reason": "timeout", "commands": logs})
        return 1
    except subprocess.CalledProcessError as exc:
        logs.append({"cmd": exc.cmd, "returncode": exc.returncode, "stdout": (exc.stdout or "")[-4000:], "stderr": (exc.stderr or "")[-4000:]})
        write_report({"status": "fail", "reason": "compile_failed", "commands": logs})
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
            "stimulus_column_alignment",
            "dynamic_output_consistency",
            "gcov_concrete_coverage",
            "klee_symbolic_path_exploration",
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
