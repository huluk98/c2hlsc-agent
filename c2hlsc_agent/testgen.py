from __future__ import annotations

from .analyze import AnalysisResult, FunctionArg, active_length_arg, looks_like_length_name
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




def _is_unsigned(c_type: str) -> bool:
    return "unsigned" in c_type or c_type.strip().startswith("uint") or "ap_uint" in c_type


def _scalar_decl(arg: FunctionArg, config: AgentConfig) -> str:
    slot = directed_var(config, "test_idx")
    if arg.scalar_range:
        lo, hi = arg.scalar_range
        base = f"bounded_scalar<{arg.c_type}>({slot}, rng, {lo}LL, {hi}LL)"
    else:
        base = f"random_value<{arg.c_type}>(rng)"
    guard = extra_guard(config, "test_idx")
    if not guard:
        return base
    return (
        f"({guard}) ? static_cast<{arg.c_type}>(c2hlsc_extra_{arg.name}[test_idx]) : {base}"
    )


def _storage_type(arg: FunctionArg) -> str:
    return " ".join(token for token in arg.c_type.split() if token not in {"const", "volatile"})


def _value_print(expr: str) -> str:
    """Integer rendering, for contexts that genuinely need one (``clamp_count``)."""

    return f"static_cast<long long>({expr})"


def _show_value(expr: str) -> str:
    """Diagnostic rendering that preserves the value's own type.

    Mismatch text used to cast every value to ``long long``, which destroys exactly the
    evidence the diagnostic exists to carry: on a floating-point design a 1.5-vs-1.7
    disagreement both print as ``1``, and a NaN casts to INT64_MIN, so a real NaN
    divergence renders as ``expected=-9223372036854775808 actual=-9223372036854775808``
    -- identical values that nonetheless failed. The repair agent reads these.
    """

    return f"c2hlsc_show({expr})"


def _init_array(arg: FunctionArg, config: AgentConfig) -> str:
    storage = _storage_type(arg)
    if arg.direction == "output":
        # Outputs are never driven from a refinement vector: the sentinel is what makes an
        # unwritten element visible, and it must stay unique per test index.
        return f"""for (int i = 0; i < {arg.length}; ++i) {{
      auto v = output_sentinel<{storage}>(test_idx, i);
      ref_{arg.name}[i] = v;
      hls_{arg.name}[i] = v;
    }}"""
    unsigned = "true" if _is_unsigned(arg.c_type) else "false"
    slot = directed_var(config, "test_idx")
    patterned = f"""for (int i = 0; i < {arg.length}; ++i) {{
      auto v = patterned_value<{storage}>({slot}, i, rng, {unsigned});
      ref_{arg.name}[i] = v;
      hls_{arg.name}[i] = v;
    }}"""
    guard = extra_guard(config, "test_idx")
    if not guard:
        return patterned
    return f"""if ({guard}) {{
      for (int i = 0; i < {arg.length}; ++i) {{
        auto v = static_cast<{storage}>(c2hlsc_extra_{arg.name}[test_idx][i]);
        ref_{arg.name}[i] = v;
        hls_{arg.name}[i] = v;
      }}
    }} else {{
      {patterned}
    }}"""


def multi_dim_cast(arg: FunctionArg) -> str:
    """The pointer-to-array type a multi-dimensional parameter decays to, or ``""``.

    The testbench keeps one flat buffer per array argument, which is the right shape for
    the stimulus and comparison loops and is layout-identical to the declared array. But
    a parameter written `int a[N][60]` is `int (*)[60]`, not `int *`, so passing the flat
    buffer straight through is a type error. The cast reinterprets the same bytes with
    the shape the callee declares.
    """

    if len(arg.array_dims) < 2:
        return ""
    trailing = arg.array_dims[1:]
    if not all(dim.strip() for dim in trailing):
        return ""
    suffix = "".join(f"[{dim.strip()}]" for dim in trailing)
    base = arg.c_type.replace("const", "").strip()
    return f"{base} (*){suffix}"


def _call_args(prefix: str, args: list[FunctionArg]) -> str:
    values: list[str] = []
    for arg in args:
        if arg.is_pointer_like:
            name = f"{prefix}_{arg.name}"
            cast = multi_dim_cast(arg)
            values.append(f"reinterpret_cast<{cast}>({name})" if cast else name)
        else:
            values.append(arg.name)
    return ", ".join(values)


_looks_like_length_name = looks_like_length_name


_active_length_arg = active_length_arg


def _scalar_log_expr(scalars: list[FunctionArg]) -> str:
    return "".join(f' << " {arg.name}=" << {_show_value(arg.name)}' for arg in scalars)


def _array_trace_lines(current: FunctionArg, arrays: list[FunctionArg]) -> str:
    lines: list[str] = []
    for arg in arrays:
        if arg.name == current.name:
            continue
        lines.append(
            f"""        if (i < {arg.length}) {{
          std::cerr << " {arg.name}[i]=" << {_show_value(f'ref_{arg.name}[i]')};
        }}"""
        )
    return "\n".join(lines)


def _contract_comment(fn_args: list[FunctionArg], return_type: str, arrays: list[FunctionArg], scalars: list[FunctionArg]) -> str:
    observable = return_type != "void" or any(arg.direction in {"output", "inout"} for arg in arrays)
    lines = [
        "// Testbench contract:",
        "// - golden oracle: macro-renamed original C top function",
        "// - generated HLS top: called with cloned inputs from the same stimulus",
    ]
    if not observable:
        lines.append("// - WARNING: no return value or output/inout argument is available to compare")
    for arg in fn_args:
        if arg.is_pointer_like:
            compare = "not compared"
            if arg.direction in {"output", "inout"}:
                active_len = _active_length_arg(arg, scalars)
                if active_len:
                    compare = f"compare first clamp({active_len.name}, {arg.length}) elements"
                else:
                    compare = f"compare all {arg.length} elements"
            lines.append(f"// - {arg.name}: direction={arg.direction} length={arg.length} {compare}")
        elif arg.scalar_range:
            lo, hi = arg.scalar_range
            lines.append(f"// - {arg.name}: scalar range=[{lo}, {hi}] with directed boundary tests")
        else:
            lines.append(f"// - {arg.name}: scalar random stimulus")
    return "\n".join(lines)


def generate_testbench(analysis: AnalysisResult, config: AgentConfig) -> str:
    fn = analysis.function
    arrays = [arg for arg in fn.args if arg.is_pointer_like]
    scalars = [arg for arg in fn.args if not arg.is_pointer_like]
    contract_comment = _contract_comment(fn.args, fn.return_type, arrays, scalars)
    declarations: list[str] = []
    initializers: list[str] = []
    # static: a real HLS kernel's arrays are megabytes -- knn's searchSpace alone is
    # 8 MB -- and these are declared inside the per-test loop, so leaving them on the
    # stack overflows it and the testbench segfaults before it can compare anything.
    # Every element is written by the initialisation loops on each iteration, so BSS
    # storage carries no state between tests.
    for arg in arrays:
        storage_type = _storage_type(arg)
        declarations.append(f"    static {storage_type} ref_{arg.name}[{arg.length}] = {{}};")
        declarations.append(f"    static {storage_type} hls_{arg.name}[{arg.length}] = {{}};")
        initializers.append("    " + _init_array(arg, config).replace("\n", "\n    "))
    for arg in scalars:
        declarations.append(f"    {arg.c_type} {arg.name} = {_scalar_decl(arg, config)};")

    return_compare = ""
    return_capture_ref = ""
    return_capture_hls = ""
    scalar_context = _scalar_log_expr(scalars)
    if fn.return_type != "void":
        return_capture_ref = f"{fn.return_type} ref_ret = "
        return_capture_hls = f"{fn.return_type} hls_ret = "
        return_compare = f"""
    ++c2hlsc_comparisons;
    if (!values_equal(ref_ret, hls_ret)) {{
      std::cerr << "Mismatch test=" << test_idx << " return expected="
                << {_show_value('ref_ret')} << " actual=" << {_show_value('hls_ret')}
                << " seed={config.seed}"{scalar_context} << "\\n";
      return 1;
    }}"""

    comparisons: list[str] = []
    compare_declarations: list[str] = []
    for arg in arrays:
        if arg.direction in {"output", "inout"}:
            active_len = _active_length_arg(arg, scalars)
            compare_var = f"compare_len_{arg.name}"
            if active_len:
                compare_declarations.append(
                    f"    const int {compare_var} = clamp_count({_value_print(active_len.name)}, {arg.length});"
                )
            else:
                compare_declarations.append(f"    const int {compare_var} = {arg.length};")
            trace_lines = _array_trace_lines(arg, arrays)
            if trace_lines:
                trace_lines = "\n" + trace_lines
            comparisons.append(f"""    for (int i = 0; i < {compare_var}; ++i) {{
      ++c2hlsc_comparisons;
      if (!values_equal(ref_{arg.name}[i], hls_{arg.name}[i])) {{
        std::cerr << "Mismatch test=" << test_idx << " arg={arg.name} index=" << i
                  << " expected=" << {_show_value(f'ref_{arg.name}[i]')}
                  << " actual=" << {_show_value(f'hls_{arg.name}[i]')}
                  << " seed={config.seed}"
                  << " compare_len=" << {compare_var}{scalar_context};{trace_lines}
        std::cerr << "\\n";
        return 1;
      }}
    }}""")

    vectors = extra_vectors(config)
    schedule = directed_schedule(config)
    stimulus_helpers = render_helpers(config, "test_idx")
    extra_tables = render_extra_tables(fn.args, vectors)
    directed_decl = directed_index_decl(config, "test_idx")
    iterations = total_iterations(config)
    directed_names = ", ".join(schedule) or "none"
    extra_note = f"; +{len(vectors)} refinement vector(s)" if vectors else ""
    observable = fn.return_type != "void" or any(
        arg.direction in {"output", "inout"} for arg in arrays
    )
    # Only a top with something observable is required to have compared something. The
    # unobservable case is already refused earlier, by the `nothing-to-compare` analysis
    # error, so this guard covers the run-time half: declared outputs that were never
    # reached because every active length clamped to zero.
    vacuity_guard = (
        """  if (c2hlsc_comparisons == 0) {
    std::cerr << "c2hlsc_agent: FAIL compared 0 values across all tests; the oracle "
                 "examined nothing, so this run is not evidence of equivalence "
                 "(check that the active-length argument is ever non-zero)\\n";
    return 1;
  }"""
        if observable
        else "  // no observable outputs: the analysis gate already refused this top"
    )

    return f"""// Generated by c2hlsc_agent. This file is testbench-only code.
{contract_comment}
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <random>
#include <sstream>
#include <string>

extern "C" {{
#define restrict __restrict__
// The golden C may carry its own main() -- benchmark sources usually do. The
// testbench defines main, so rename the original's out of the way rather than
// colliding with it. CHStone's own flow does the same thing with -Dmain=...
#define main c2hlsc_golden_main
#define {fn.name} {fn.name}_ref
#include "../input.c"
#undef main
#undef {fn.name}
}}

#include "../src/hls_top.hpp"

{extra_tables}{stimulus_helpers}
template <typename T>
std::string c2hlsc_show(T value) {{
  std::ostringstream out;
  if (std::numeric_limits<T>::is_integer) {{
    out << static_cast<long long>(value);
  }} else {{
    out << std::setprecision(17) << static_cast<long double>(value);
  }}
  return out.str();
}}

template <typename T>
bool values_equal(T a, T b) {{
  if (std::numeric_limits<T>::is_integer) {{
    return a == b;
  }}
  long double da = static_cast<long double>(a);
  long double db = static_cast<long double>(b);
  // Identical non-finite values must agree before the tolerance is applied. For two
  // infinities the relative test computes inf - inf = NaN, and NaN <= anything is false,
  // so values_equal(inf, inf) used to report a mismatch between identical values. This is
  // a differential oracle: golden and HLS arriving at the same NaN agree about the
  // computation, whatever IEEE says about NaN's own identity.
  if (std::isnan(da) && std::isnan(db)) {{
    return true;
  }}
  if (std::isnan(da) || std::isnan(db)) {{
    return false;
  }}
  if (std::isinf(da) || std::isinf(db)) {{
    return da == db;
  }}
  long double diff = da > db ? da - db : db - da;
  long double scale = std::fabs(da) > std::fabs(db) ? std::fabs(da) : std::fabs(db);
  if (scale < 1.0L) scale = 1.0L;
  return diff <= 1e-6L * scale;
}}

int clamp_count(long long value, int limit) {{
  if (value < 0) return 0;
  if (value > limit) return limit;
  return static_cast<int>(value);
}}

int main() {{
  std::mt19937_64 rng({config.seed}ULL);
  // Evidence, counted at run time. The declared compare set can be non-empty and still
  // examine nothing: an active-length scalar that is always 0 makes every
  // `clamp_count(n, N)` zero, so each comparison loop runs zero times and any
  // implementation passes. The static contract cannot see that; only the count can.
  long long c2hlsc_comparisons = 0;
  for (int test_idx = 0; test_idx < {iterations}; ++test_idx) {{
{directed_decl}
{chr(10).join(declarations)}
{chr(10).join(initializers)}
{chr(10).join(compare_declarations)}

    {return_capture_ref}{fn.name}_ref({_call_args('ref', fn.args)});
    {return_capture_hls}{fn.name}({_call_args('hls', fn.args)});
{return_compare}
{chr(10).join(comparisons)}
  }}
{vacuity_guard}
  std::cout << "c2hlsc_agent: all {iterations} tests passed, seed={config.seed}"
            << " (compared " << c2hlsc_comparisons << " value(s)"
            << "; directed: {directed_names}{extra_note})\\n";
  return 0;
}}
"""
