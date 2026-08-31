from __future__ import annotations

from .analyze import AnalysisResult, FunctionArg
from .config import AgentConfig


_LENGTH_NAMES = {
    "n",
    "len",
    "length",
    "size",
    "count",
    "num",
    "limit",
    "samples",
    "elements",
}


def _is_unsigned(c_type: str) -> bool:
    return "unsigned" in c_type or c_type.strip().startswith("uint") or "ap_uint" in c_type


def _scalar_decl(arg: FunctionArg) -> str:
    if arg.scalar_range:
        lo, hi = arg.scalar_range
        return f"bounded_scalar<{arg.c_type}>(test_idx, rng, {lo}LL, {hi}LL)"
    return f"random_value<{arg.c_type}>(rng)"


def _storage_type(arg: FunctionArg) -> str:
    return " ".join(token for token in arg.c_type.split() if token not in {"const", "volatile"})


def _value_print(expr: str) -> str:
    # `static_cast<long long>` does not compile for a struct-typed element; `printable`
    # is overloaded on arithmetic-ness in the emitted testbench.
    return f"printable({expr})"


# A bound resolved from real constants can be large (Rosetta's spam-filter kernel declares
# NUM_FEATURES * NUM_TRAINING = 4,608,000 elements). Anything above this is clamped and the
# clamp is stated in the testbench contract, because a silently-shrunk stimulus is exactly
# the kind of unsound pass this generator is supposed to make impossible.
_MAX_TEST_ELEMENTS = 1 << 24


def _element_count(arg: FunctionArg) -> int:
    return min(int(arg.length or 0), _MAX_TEST_ELEMENTS)


# Total element-writes per iteration above which the configured repetition count is
# reduced. Chosen so a full-size kernel still runs several complete iterations.
_STIMULUS_BUDGET = 1 << 21


def _effective_tests(arrays: list[FunctionArg], configured: int) -> int:
    """How many iterations the configured stimulus can actually afford."""

    per_iteration = sum(_element_count(arg) for arg in arrays)
    if per_iteration <= 0:
        return configured
    affordable = max(1, _STIMULUS_BUDGET // per_iteration)
    return max(1, min(configured, affordable))


def _array_declaration(arg: FunctionArg) -> list[str]:
    """Storage plus a view whose type matches the parameter's declared shape.

    Two things the old flat ``T ref_x[N];`` could not do. It could not bind to a
    multi-dimensional parameter -- ``bit8*`` does not convert to ``bit8 (*)[256]`` -- and
    at a real bound it would overflow the stack, since the testbench declares two copies of
    every argument. Allocating on the heap and casting to the declared shape fixes both.
    """

    storage_type = _storage_type(arg)
    count = _element_count(arg)
    lines: list[str] = []
    dims = arg.resolved_dims
    for prefix in ("ref", "hls"):
        lines.append(f"  std::vector<{storage_type}> {prefix}_{arg.name}_storage({count});")
        if len(dims) > 1:
            suffix = "".join(f"[{d}]" for d in dims[1:])
            lines.append(
                f"  auto {prefix}_{arg.name} = reinterpret_cast<{storage_type}(*){suffix}>"
                f"({prefix}_{arg.name}_storage.data());"
            )
        else:
            lines.append(
                f"  {storage_type}* {prefix}_{arg.name} = {prefix}_{arg.name}_storage.data();"
            )
    return lines


def _init_array(arg: FunctionArg) -> str:
    if arg.direction == "output":
        return f"""for (int i = 0; i < {_element_count(arg)}; ++i) {{
      auto v = output_sentinel<{_storage_type(arg)}>(test_idx, i);
      ref_{arg.name}_storage[i] = v;
      hls_{arg.name}_storage[i] = v;
    }}"""
    unsigned = "true" if _is_unsigned(arg.c_type) else "false"
    return f"""for (int i = 0; i < {_element_count(arg)}; ++i) {{
      auto v = patterned_value<{_storage_type(arg)}>(test_idx, i, rng, {unsigned});
      ref_{arg.name}_storage[i] = v;
      hls_{arg.name}_storage[i] = v;
    }}"""


def _call_args(prefix: str, args: list[FunctionArg]) -> str:
    values: list[str] = []
    for arg in args:
        if arg.is_pointer_like:
            values.append(f"{prefix}_{arg.name}")
        else:
            values.append(arg.name)
    return ", ".join(values)


def _looks_like_length_name(scalar_name: str, array_name: str) -> bool:
    name = scalar_name.lower()
    array = array_name.lower()
    return (
        name in _LENGTH_NAMES
        or name in {f"{array}_n", f"n_{array}", f"{array}_len", f"{array}_length", f"{array}_size", f"{array}_count"}
        or name.startswith("num_")
        or name.endswith("_len")
        or name.endswith("_length")
        or name.endswith("_size")
        or name.endswith("_count")
    )


def _active_length_arg(array_arg: FunctionArg, scalars: list[FunctionArg]) -> FunctionArg | None:
    for scalar in scalars:
        if not scalar.scalar_range:
            continue
        lo, hi = scalar.scalar_range
        if lo < 0 or array_arg.length is None or hi > array_arg.length:
            continue
        if _looks_like_length_name(scalar.name, array_arg.name):
            return scalar
    return None


def _scalar_log_expr(scalars: list[FunctionArg]) -> str:
    return "".join(f' << " {arg.name}=" << {_value_print(arg.name)}' for arg in scalars)


def _array_trace_lines(current: FunctionArg, arrays: list[FunctionArg]) -> str:
    lines: list[str] = []
    for arg in arrays:
        if arg.name == current.name:
            continue
        lines.append(
            f"""        if (i < {_element_count(arg)}) {{
          std::cerr << " {arg.name}[i]=" << {_value_print(f'ref_{arg.name}_storage[i]')};
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
    num_tests = _effective_tests(arrays, config.num_tests)
    contract_comment = _contract_comment(fn.args, fn.return_type, arrays, scalars)
    if num_tests != config.num_tests:
        contract_comment += (
            f"\n// - iterations reduced from {config.num_tests} to {num_tests}: the stimulus is "
            f"{sum(_element_count(a) for a in arrays)} elements per iteration at the kernel's "
            "declared bounds, which is the real shape rather than a truncated one"
        )
    declarations: list[str] = []
    initializers: list[str] = []
    array_declarations: list[str] = []
    for arg in arrays:
        array_declarations.extend(_array_declaration(arg))
        initializers.append("    " + _init_array(arg).replace("\n", "\n    "))
    for arg in scalars:
        declarations.append(f"    {arg.c_type} {arg.name} = {_scalar_decl(arg)};")

    return_compare = ""
    return_capture_ref = ""
    return_capture_hls = ""
    scalar_context = _scalar_log_expr(scalars)
    if fn.return_type != "void":
        return_capture_ref = f"{fn.return_type} ref_ret = "
        return_capture_hls = f"{fn.return_type} hls_ret = "
        return_compare = f"""
    if (!values_equal(ref_ret, hls_ret)) {{
      std::cerr << "Mismatch test=" << test_idx << " return expected="
                << {_value_print('ref_ret')} << " actual=" << {_value_print('hls_ret')}
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
                    f"    const int {compare_var} = clamp_count({_value_print(active_len.name)}, {_element_count(arg)});"
                )
            else:
                compare_declarations.append(f"    const int {compare_var} = {_element_count(arg)};")
            trace_lines = _array_trace_lines(arg, arrays)
            if trace_lines:
                trace_lines = "\n" + trace_lines
            comparisons.append(f"""    for (int i = 0; i < {compare_var}; ++i) {{
      if (!values_equal(ref_{arg.name}_storage[i], hls_{arg.name}_storage[i])) {{
        std::cerr << "Mismatch test=" << test_idx << " arg={arg.name} index=" << i
                  << " expected=" << {_value_print(f'ref_{arg.name}_storage[i]')}
                  << " actual=" << {_value_print(f'hls_{arg.name}_storage[i]')}
                  << " seed={config.seed}"
                  << " compare_len=" << {compare_var}{scalar_context};{trace_lines}
        std::cerr << "\\n";
        return 1;
      }}
    }}""")

    return f"""// Generated by c2hlsc_agent. This file is testbench-only code.
{contract_comment}
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <random>
#include <type_traits>
#include <vector>

extern "C" {{
#define restrict __restrict__
#define {fn.name} {fn.name}_ref
#include "../input.c"
#undef {fn.name}
}}

#include "../src/hls_top.hpp"

template <typename T>
T random_value(std::mt19937_64& rng) {{
  if (std::numeric_limits<T>::is_integer) {{
    return static_cast<T>(rng());
  }}
  return static_cast<T>((rng() % 20001) - 10000) / static_cast<T>(100);
}}

template <typename T>
T bounded_scalar(int test_idx, std::mt19937_64& rng, long long lo, long long hi) {{
  if (hi < lo) return static_cast<T>(lo);
  long long value = lo;
  if (test_idx == 0) {{
    value = lo;
  }} else if (test_idx == 1) {{
    value = hi;
  }} else if (test_idx == 2) {{
    value = lo + ((hi - lo) / 2);
  }} else if (test_idx == 3 && lo <= 1 && hi >= 1) {{
    value = 1;
  }} else {{
    const unsigned long long span = static_cast<unsigned long long>(hi - lo) + 1ULL;
    value = lo + static_cast<long long>(rng() % span);
  }}
  return static_cast<T>(value);
}}

template <typename T>
typename std::enable_if<std::is_arithmetic<T>::value, long long>::type
printable(const T& value) {{
  return static_cast<long long>(value);
}}

template <typename T>
typename std::enable_if<!std::is_arithmetic<T>::value, long long>::type
printable(const T&) {{
  return 0;  // a struct has no single integer summary; the mismatch index locates it
}}

template <typename T>
typename std::enable_if<!std::is_arithmetic<T>::value, T>::type
patterned_value(int test_idx, int element_idx, std::mt19937_64& rng, bool) {{
  // Struct-typed argument. Fill the object's bytes deterministically, masking each byte
  // to 0x3F so that any float or double member lands on a small finite value: an unmasked
  // fill can produce NaN, and NaN != NaN would be reported as a mismatch that is not one.
  T value{{}};
  unsigned char* bytes = reinterpret_cast<unsigned char*>(&value);
  unsigned long long mix = static_cast<unsigned long long>(test_idx + 1) * 0x9E3779B97F4A7C15ULL
                         ^ static_cast<unsigned long long>(element_idx + 1) * 0xBF58476D1CE4E5B9ULL;
  if (test_idx == 0) return value;  // all-zero struct stays a directed case
  for (size_t i = 0; i < sizeof(T); ++i) {{
    bytes[i] = static_cast<unsigned char>(((mix >> ((i % 8) * 8)) ^ (i * 31u)) & 0x3F);
  }}
  (void)rng;
  return value;
}}

template <typename T>
typename std::enable_if<std::is_arithmetic<T>::value, T>::type
patterned_value(int test_idx, int element_idx, std::mt19937_64& rng, bool is_unsigned) {{
  if (test_idx == 0) return static_cast<T>(0);
  if (test_idx == 1) return static_cast<T>(~static_cast<unsigned long long>(0));
  if (test_idx == 2 && std::numeric_limits<T>::is_integer) {{
    return is_unsigned ? std::numeric_limits<T>::max()
                       : (element_idx % 2 ? std::numeric_limits<T>::max() : std::numeric_limits<T>::min());
  }}
  if (test_idx == 3) return static_cast<T>(element_idx % 2 ? 0xAAAAAAAAULL : 0x55555555ULL);
  return random_value<T>(rng);
}}

template <typename T>
typename std::enable_if<!std::is_arithmetic<T>::value, T>::type
output_sentinel(int test_idx, int element_idx) {{
  T value{{}};
  unsigned char* bytes = reinterpret_cast<unsigned char*>(&value);
  for (size_t i = 0; i < sizeof(T); ++i) {{
    bytes[i] = static_cast<unsigned char>(((test_idx * 7 + element_idx * 13 + i * 3) & 0x3F) | 0x10);
  }}
  return value;
}}

template <typename T>
typename std::enable_if<std::is_arithmetic<T>::value, T>::type
output_sentinel(int test_idx, int element_idx) {{
  unsigned long long value = 0x9E3779B97F4A7C15ULL;
  value ^= static_cast<unsigned long long>(test_idx + 1) * 0xBF58476D1CE4E5B9ULL;
  value ^= static_cast<unsigned long long>(element_idx + 1) * 0x94D049BB133111EBULL;
  return static_cast<T>(value);
}}

template <typename T>
typename std::enable_if<!std::is_arithmetic<T>::value, bool>::type
values_equal(const T& a, const T& b) {{
  return std::memcmp(&a, &b, sizeof(T)) == 0;
}}

template <typename T>
typename std::enable_if<std::is_arithmetic<T>::value, bool>::type
values_equal(T a, T b) {{
  if (std::numeric_limits<T>::is_integer) {{
    return a == b;
  }}
  long double da = static_cast<long double>(a);
  long double db = static_cast<long double>(b);
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
// Argument storage is allocated once, not per test: at a real bound these are tens of
// megabytes, and the testbench holds two copies of every argument.
{chr(10).join(array_declarations)}
  for (int test_idx = 0; test_idx < {num_tests}; ++test_idx) {{
{chr(10).join(declarations)}
{chr(10).join(initializers)}
{chr(10).join(compare_declarations)}

    {return_capture_ref}{fn.name}_ref({_call_args('ref', fn.args)});
    {return_capture_hls}{fn.name}({_call_args('hls', fn.args)});
{return_compare}
{chr(10).join(comparisons)}
  }}
  std::cout << "c2hlsc_agent: all {num_tests} tests passed, seed={config.seed}\\n";
  return 0;
}}
"""
