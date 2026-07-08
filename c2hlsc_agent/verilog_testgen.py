from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .analyze import AnalysisResult, FunctionArg
from .config import AgentConfig


RTL_TESTBENCH_POLICY_ID = "hls_rtl_cosim_selfcheck_v1"

RTL_TESTBENCH_SYSTEM_PROMPT = """You are the rtl_testbench_agent for AUTO RTL.

Purpose:
- Emit a standalone, self-checking Verilog/SystemVerilog testbench that drives the
  *synthesized* HLS RTL directly, independent of Vitis auto-cosim.
- Keep the original C in the oracle path: expected vectors are produced by the
  macro-renamed golden C top, never by re-deriving values from the HLS-C design.

Core requirements:
- Model the AMD/Xilinx Vitis HLS default RTL contract: ap_ctrl_hs block-level control
  (ap_clk/ap_rst/ap_start/ap_done/ap_idle/ap_ready), ap_memory arrays with a
  registered one-cycle read latency, ap_none scalars, and ap_return.
- Reproduce the same directed + pseudo-random stimulus schedule as the host and CSim
  testbenches so a failing test is reproducible from the seed.
- Compare only the declared active output range so a correct design is never failed on
  inactive output elements.
- Detect the true RTL port names/widths/reset polarity from the synthesized netlist when
  it is available, and fall back to the interface contract otherwise.
- Skip cleanly (no failure) when no RTL simulator is installed, so generated projects
  stay portable.
- Treat a testbench PASS as bounded, stimulus-driven RTL evidence, not a universal proof.
"""


@dataclass(frozen=True)
class RtlTestbenchContract:
    policy_id: str
    owner_agent: str
    owns_hlsc_generation: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "owner_agent": self.owner_agent,
            "owns_hlsc_generation": self.owns_hlsc_generation,
        }


@dataclass(frozen=True)
class VerilogTBBundle:
    vectors_tb: str
    gen_script: str
    run_script: str
    manifest_json: str
    policy_id: str = RTL_TESTBENCH_POLICY_ID


def get_rtl_testbench_contract() -> RtlTestbenchContract:
    return RtlTestbenchContract(
        policy_id=RTL_TESTBENCH_POLICY_ID,
        owner_agent="shift_left_testbench_agent",
    )


# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------

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


def _clean_type(c_type: str) -> str:
    return " ".join(token for token in c_type.split() if token not in {"const", "volatile"})


def _is_float(c_type: str) -> bool:
    tokens = _clean_type(c_type).split()
    return "float" in tokens or "double" in tokens


def _is_unsigned(c_type: str) -> bool:
    t = _clean_type(c_type)
    return "unsigned" in t.split() or t.startswith("uint") or "ap_uint" in t


def _elem_bits(c_type: str) -> int:
    t = _clean_type(c_type)
    if "double" in t.split():
        return 64
    if "float" in t.split():
        return 32
    match = re.search(r"\b(?:u?int)(\d+)_t\b", t)
    if match:
        return int(match.group(1))
    match = re.search(r"\bap_u?int\s*<\s*(\d+)\s*>", t)
    if match:
        return int(match.group(1))
    if "long long" in t or re.search(r"\blong\b", t):
        return 64
    if re.search(r"\bshort\b", t):
        return 16
    if re.search(r"\b(?:signed\s+char|unsigned\s+char|char)\b", t):
        return 8
    if re.search(r"\b(?:_Bool|bool)\b", t):
        return 1
    # int / unsigned / unsigned int / ap_int without an explicit width
    return 32


def _addr_bits(depth: int) -> int:
    if depth <= 1:
        return 1
    return max(1, (depth - 1).bit_length())


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


def _array_ports(direction: str) -> list[str]:
    reads = direction in {"input", "inout"}
    writes = direction in {"output", "inout"}
    ports = ["address0", "ce0"]
    if writes:
        ports.extend(["we0", "d0"])
    if reads:
        ports.append("q0")
    return ports


# ---------------------------------------------------------------------------
# Contract spec (serialized as the manifest consumed by gen_rtl_tb.py)
# ---------------------------------------------------------------------------

_AXI_MODES = {"s_axilite", "m_axi", "axis"}

# Reserved words that, as a scalar arg name, force an escaped identifier in the generated
# testbench (kept in sync with the KEYWORDS set embedded in gen_rtl_tb.py).
_RESERVED_WORDS = {
    "input", "output", "inout", "wire", "reg", "integer", "real", "realtime", "time",
    "event", "type", "bit", "byte", "logic", "int", "shortint", "longint", "signed",
    "unsigned", "string", "begin", "end", "module", "initial", "always", "assign",
    "parameter", "localparam", "function", "task", "wait", "force", "fork", "join",
    "case", "default", "for", "while", "repeat", "genvar", "generate", "and", "or",
    "not", "xor", "posedge", "negedge", "if", "else", "do", "return",
}


def build_spec(analysis: AnalysisResult, config: AgentConfig) -> dict[str, object]:
    fn = analysis.function
    arrays = [arg for arg in fn.args if arg.is_pointer_like]
    scalars = [arg for arg in fn.args if not arg.is_pointer_like]

    notes: list[str] = []
    if config.interface_mode in _AXI_MODES:
        notes.append(
            f"interface_mode={config.interface_mode!r} uses an AXI adapter whose RTL ports are "
            "not modeled by this standalone testbench; regenerate from the synthesized netlist "
            "(gen_rtl_tb.py --from-rtl) or rely on Vitis cosim for AXI interfaces."
        )

    array_specs: list[dict[str, object]] = []
    for arg in arrays:
        depth = arg.length or 16
        is_float = _is_float(arg.c_type)
        cmp_scalar = _active_length_arg(arg, scalars)
        array_specs.append(
            {
                "name": arg.name,
                "dir": arg.direction,
                "bits": _elem_bits(arg.c_type),
                "signed": not _is_unsigned(arg.c_type),
                "float": is_float,
                "advisory": is_float,
                "depth": depth,
                "addr_bits": _addr_bits(depth),
                "ports": _array_ports(arg.direction),
                "cmp_scalar": cmp_scalar.name if cmp_scalar else None,
            }
        )
        if is_float:
            notes.append(f"array {arg.name!r} is floating-point; RTL compare is advisory (warn-only).")

    scalar_specs = [
        {
            "name": arg.name,
            "bits": _elem_bits(arg.c_type),
            "signed": not _is_unsigned(arg.c_type),
            "float": _is_float(arg.c_type),
        }
        for arg in scalars
    ]
    for arg in scalars:
        if arg.name in _RESERVED_WORDS:
            notes.append(
                f"scalar {arg.name!r} collides with a Verilog/SystemVerilog reserved word; the "
                "testbench emits it as an escaped identifier. If the synthesized RTL mangles the "
                "port name, regenerate with gen_rtl_tb.py --from-rtl or rename the argument."
            )

    ret_spec: dict[str, object] | None = None
    if fn.return_type != "void":
        ret_float = _is_float(fn.return_type)
        ret_spec = {
            "bits": _elem_bits(fn.return_type),
            "signed": not _is_unsigned(fn.return_type),
            "float": ret_float,
            "advisory": ret_float,
        }
        if ret_float:
            notes.append("return value is floating-point; RTL compare is advisory (warn-only).")

    return {
        "policy_id": RTL_TESTBENCH_POLICY_ID,
        "top": fn.name,
        "num_tests": config.num_tests,
        "seed": config.seed,
        "interface_mode": config.interface_mode,
        "clock_period": config.clock,
        "block_protocol": "ap_ctrl_hs",
        "reset": {"name": "ap_rst", "active_low": False, "cycles": 4},
        "ap_continue": False,
        "vectors_dir": "rtl_vectors",
        "arrays": array_specs,
        "scalars": scalar_specs,
        "ret": ret_spec,
        "files": {
            "vectors_tb": "tb/rtl_vectors_tb.cpp",
            "gen_script": "tb/gen_rtl_tb.py",
            "run_script": "tb/run_rtl_sim.py",
            "manifest": "tb/rtl_tb_manifest.json",
            "testbench": f"tb/{fn.name}_tb.sv",
            "report": "coverage/rtl_tb_report.json",
        },
        "make_targets": ["rtl-vectors", "rtl-testbench", "rtl-cosim"],
        "notes": notes,
    }


def _manifest(analysis: AnalysisResult, config: AgentConfig, spec: dict[str, object]) -> str:
    return json.dumps(spec, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Golden vector dumper (C++), design-specific
# ---------------------------------------------------------------------------

_VECTORS_HELPERS = """template <typename T>
T random_value(std::mt19937_64& rng) {
  if (std::numeric_limits<T>::is_integer) {
    return static_cast<T>(rng());
  }
  // Subtract in signed arithmetic: rng() % 20001 is unsigned, so an unsigned "- 10000"
  // would wrap huge-positive for the low half instead of yielding negative floats.
  return static_cast<T>(static_cast<long long>(rng() % 20001) - 10000) / static_cast<T>(100);
}

template <typename T>
T bounded_scalar(int test_idx, std::mt19937_64& rng, long long lo, long long hi) {
  if (hi < lo) return static_cast<T>(lo);
  long long value = lo;
  if (test_idx == 0) {
    value = lo;
  } else if (test_idx == 1) {
    value = hi;
  } else if (test_idx == 2) {
    value = lo + ((hi - lo) / 2);
  } else if (test_idx == 3 && lo <= 1 && hi >= 1) {
    value = 1;
  } else {
    const unsigned long long span = static_cast<unsigned long long>(hi - lo) + 1ULL;
    value = lo + static_cast<long long>(rng() % span);
  }
  return static_cast<T>(value);
}

template <typename T>
T patterned_value(int test_idx, int element_idx, std::mt19937_64& rng, bool is_unsigned) {
  if (test_idx == 0) return static_cast<T>(0);
  if (test_idx == 1) return static_cast<T>(~static_cast<unsigned long long>(0));
  if (test_idx == 2 && std::numeric_limits<T>::is_integer) {
    return is_unsigned ? std::numeric_limits<T>::max()
                       : (element_idx % 2 ? std::numeric_limits<T>::max() : std::numeric_limits<T>::min());
  }
  if (test_idx == 3) return static_cast<T>(element_idx % 2 ? 0xAAAAAAAAULL : 0x55555555ULL);
  return random_value<T>(rng);
}

int clamp_count(long long value, int limit) {
  if (value < 0) return 0;
  if (value > limit) return limit;
  return static_cast<int>(value);
}

void dump_hex(std::ofstream& os, unsigned long long value, int bits) {
  unsigned long long mask = (bits >= 64) ? ~0ULL : ((1ULL << bits) - 1ULL);
  int digits = (bits + 3) / 4;
  os << std::hex << std::setw(digits) << std::setfill('0') << (value & mask) << std::dec << "\\n";
}

void dump_float32(std::ofstream& os, float value) {
  uint32_t bits = 0;
  std::memcpy(&bits, &value, sizeof(bits));
  dump_hex(os, static_cast<unsigned long long>(bits), 32);
}

void dump_float64(std::ofstream& os, double value) {
  uint64_t bits = 0;
  std::memcpy(&bits, &value, sizeof(bits));
  dump_hex(os, static_cast<unsigned long long>(bits), 64);
}
"""


def _dump_stmt(stream: str, expr: str, bits: int, is_float: bool) -> str:
    if is_float and bits == 32:
        return f"      dump_float32({stream}, static_cast<float>({expr}));"
    if is_float and bits == 64:
        return f"      dump_float64({stream}, static_cast<double>({expr}));"
    return f"      dump_hex({stream}, static_cast<unsigned long long>(static_cast<long long>({expr})), {bits});"


def _storage_type(arg: FunctionArg) -> str:
    return " ".join(token for token in arg.c_type.split() if token not in {"const", "volatile"})


def _vectors_tb(analysis: AnalysisResult, config: AgentConfig, spec: dict[str, object]) -> str:
    fn = analysis.function
    arrays = [arg for arg in fn.args if arg.is_pointer_like]
    scalars = [arg for arg in fn.args if not arg.is_pointer_like]
    array_spec = {item["name"]: item for item in spec["arrays"]}  # type: ignore[index]
    scalar_spec = {item["name"]: item for item in spec["scalars"]}  # type: ignore[index]
    vdir = spec["vectors_dir"]

    streams: list[str] = []
    for arg in arrays:
        info = array_spec[arg.name]
        if arg.direction in {"input", "inout"}:
            streams.append(f'  std::ofstream f_vec_{arg.name}("{vdir}/rtl_vec_{arg.name}.mem");')
        if arg.direction in {"output", "inout"}:
            streams.append(f'  std::ofstream f_exp_{arg.name}("{vdir}/rtl_exp_{arg.name}.mem");')
            streams.append(f'  std::ofstream f_cmp_{arg.name}("{vdir}/rtl_cmp_{arg.name}.mem");')
    for arg in scalars:
        streams.append(f'  std::ofstream f_scalar_{arg.name}("{vdir}/rtl_scalar_{arg.name}.mem");')
    if fn.return_type != "void":
        streams.append(f'  std::ofstream f_exp_return("{vdir}/rtl_exp_return.mem");')

    open_check_names = []
    for arg in arrays:
        if arg.direction in {"input", "inout"}:
            open_check_names.append(f"f_vec_{arg.name}")
        if arg.direction in {"output", "inout"}:
            open_check_names.append(f"f_exp_{arg.name}")
            open_check_names.append(f"f_cmp_{arg.name}")
    for arg in scalars:
        open_check_names.append(f"f_scalar_{arg.name}")
    if fn.return_type != "void":
        open_check_names.append("f_exp_return")
    open_check = " || ".join(f"!{name}" for name in open_check_names) or "false"

    declarations: list[str] = []
    initializers: list[str] = []
    input_dumps: list[str] = []
    scalar_dumps: list[str] = []
    output_dumps: list[str] = []

    for arg in arrays:
        info = array_spec[arg.name]
        storage = _storage_type(arg)
        depth = info["depth"]
        declarations.append(f"    {storage} ref_{arg.name}[{depth}] = {{}};")
        unsigned = "true" if info["signed"] is False else "false"
        if arg.direction in {"input", "inout"}:
            initializers.append(
                f"    for (int i = 0; i < {depth}; ++i) ref_{arg.name}[i] = "
                f"patterned_value<{storage}>(test_idx, i, rng, {unsigned});"
            )
            input_dumps.append(f"    for (int i = 0; i < {depth}; ++i) {{")
            input_dumps.append(_dump_stmt(f"f_vec_{arg.name}", f"ref_{arg.name}[i]", info["bits"], info["float"]))
            input_dumps.append("    }")
        else:
            initializers.append(f"    for (int i = 0; i < {depth}; ++i) ref_{arg.name}[i] = 0;")

    for arg in scalars:
        info = scalar_spec[arg.name]
        if arg.scalar_range:
            lo, hi = arg.scalar_range
            declarations.append(f"    {arg.c_type} {arg.name} = bounded_scalar<{arg.c_type}>(test_idx, rng, {lo}LL, {hi}LL);")
        else:
            declarations.append(f"    {arg.c_type} {arg.name} = random_value<{arg.c_type}>(rng);")
        scalar_dumps.append(_dump_stmt(f"f_scalar_{arg.name}", arg.name, info["bits"], info["float"]))

    call_args = []
    for arg in fn.args:
        call_args.append(f"ref_{arg.name}" if arg.is_pointer_like else arg.name)
    return_prefix = f"{fn.return_type} ref_ret = " if fn.return_type != "void" else ""

    for arg in arrays:
        info = array_spec[arg.name]
        depth = info["depth"]
        if arg.direction in {"output", "inout"}:
            output_dumps.append(f"    for (int i = 0; i < {depth}; ++i) {{")
            output_dumps.append(_dump_stmt(f"f_exp_{arg.name}", f"ref_{arg.name}[i]", info["bits"], info["float"]))
            output_dumps.append("    }")
            cmp_scalar = info["cmp_scalar"]
            if cmp_scalar:
                output_dumps.append(
                    f"      dump_hex(f_cmp_{arg.name}, "
                    f"static_cast<unsigned long long>(clamp_count(static_cast<long long>({cmp_scalar}), {depth})), 32);"
                )
            else:
                output_dumps.append(
                    f"      dump_hex(f_cmp_{arg.name}, static_cast<unsigned long long>({depth}), 32);"
                )
    if fn.return_type != "void":
        output_dumps.append(_dump_stmt("f_exp_return", "ref_ret", spec["ret"]["bits"], spec["ret"]["float"]))  # type: ignore[index]

    body = f"""// Generated by c2hlsc_agent ({RTL_TESTBENCH_POLICY_ID}).
// Golden-C RTL stimulus/expected vector dumper. Runs the macro-renamed original C top
// over the same directed+random stimulus schedule as the host/CSim testbenches and
// writes hex .mem files that the generated RTL testbench loads with $readmemh.
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <random>

extern "C" {{
#define restrict __restrict__
#define {fn.name} {fn.name}_ref
#include "../input.c"
#undef {fn.name}
}}

{_VECTORS_HELPERS}
int main() {{
{chr(10).join(streams)}
  if ({open_check}) {{
    std::cerr << "rtl_vectors_tb: failed to open a vector file under {vdir}/ (create the dir first)\\n";
    return 1;
  }}
  std::mt19937_64 rng({config.seed}ULL);
  for (int test_idx = 0; test_idx < {config.num_tests}; ++test_idx) {{
{chr(10).join(declarations)}
{chr(10).join(initializers)}
{chr(10).join(input_dumps)}
{chr(10).join(scalar_dumps)}
    {return_prefix}{fn.name}_ref({', '.join(call_args)});
{chr(10).join(output_dumps)}
  }}
  std::cout << "rtl_vectors_tb: wrote {config.num_tests} test vectors, seed={config.seed}\\n";
  return 0;
}}
"""
    return body


# ---------------------------------------------------------------------------
# Standalone generator + runner scripts (design-independent, driven by manifest)
# ---------------------------------------------------------------------------

_GEN_RTL_TB = r'''#!/usr/bin/env python3
"""Render a self-checking RTL testbench for a synthesized HLS design.

Two modes:
  --from-contract   build the testbench from tb/rtl_tb_manifest.json (interface contract)
  --from-rtl PATH   parse the synthesized Verilog module for the true port names, widths,
                    reset polarity, and single/dual-port memory shape, then render.

The original C stays the oracle: this only produces the RTL driver/comparator. Expected
values come from rtl_vectors/*.mem written by the golden-C dumper (tb/rtl_vectors_tb.cpp).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Verilog-2001 + SystemVerilog reserved words a C identifier can legally collide with.
# A scalar arg named e.g. `type`, `time`, `bit`, `input` must be emitted as an escaped
# identifier so the testbench (compiled with iverilog -g2012 / xvlog --sv) still parses.
KEYWORDS = {
    "input", "output", "inout", "wire", "reg", "integer", "real", "realtime", "time",
    "event", "type", "bit", "byte", "logic", "int", "shortint", "longint", "signed",
    "unsigned", "string", "begin", "end", "module", "endmodule", "initial", "always",
    "assign", "parameter", "localparam", "function", "task", "wait", "force", "release",
    "fork", "join", "case", "casex", "casez", "default", "for", "while", "repeat",
    "genvar", "generate", "and", "or", "not", "xor", "nand", "nor", "buf", "posedge",
    "negedge", "supply0", "supply1", "tri", "wand", "wor", "if", "else", "do", "return",
}


def esc(name: str) -> str:
    """Escaped identifier for keyword-colliding names; a bare name otherwise."""
    return ("\\" + name + " ") if name in KEYWORDS else name


def load_spec(manifest_path: Path) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def parse_rtl_ports(text: str, top: str) -> dict:
    start = re.search(r"\bmodule\s+" + re.escape(top) + r"\b", text)
    if not start:
        return {}
    tail = text[start.start():]
    end = re.search(r"\bendmodule\b", tail)
    region = tail[: end.start()] if end else tail
    ports: dict[str, tuple[str, int]] = {}
    # Terminator is [;,)] so this matches both non-ANSI declarations (`output [3:0] x;`,
    # what Vitis HLS emits) and ANSI headers (`module m(input wire [3:0] x, output y);`).
    # The required input/output/inout keyword keeps it from matching bare header names.
    decl = re.compile(
        r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?:signed\s+)?"
        r"(?:\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*[;,)]"
    )
    for m in decl.finditer(region):
        direction, hi, lo, names = m.group(1), m.group(2), m.group(3), m.group(4)
        bits = (abs(int(hi) - int(lo)) + 1) if hi is not None else 1
        for name in (n.strip() for n in names.split(",")):
            if name:
                ports[name] = (direction, bits)
    return ports


def reconcile(spec: dict, ports: dict) -> dict:
    if not ports:
        return spec
    reset = spec.setdefault("reset", {"name": "ap_rst", "active_low": False, "cycles": 4})
    if "ap_rst_n" in ports:
        reset["name"], reset["active_low"] = "ap_rst_n", True
    elif "ap_rst" in ports:
        reset["name"], reset["active_low"] = "ap_rst", False

    # ap_continue only exists under ap_ctrl_chain (dataflow tops); tie it high so the
    # design is not stalled waiting for downstream backpressure in a standalone run.
    spec["ap_continue"] = "ap_continue" in ports

    for scalar in spec.get("scalars", []):
        if scalar["name"] in ports:
            scalar["bits"] = ports[scalar["name"]][1]

    # Only refine an existing return spec. Never fabricate one from a stray ap_return port:
    # the golden-C dumper writes rtl_exp_return.mem only for a non-void top, so inventing a
    # return comparison here would $readmemh a missing file and report phantom mismatches.
    ret = spec.get("ret")
    if ret is not None and "ap_return" in ports:
        ret["bits"] = ports["ap_return"][1]

    for array in spec.get("arrays", []):
        name = array["name"]
        found = []
        for suffix in ("address0", "ce0", "we0", "d0", "q0", "address1", "ce1", "we1", "d1", "q1"):
            if f"{name}_{suffix}" in ports:
                found.append(suffix)
        if found:
            array["ports"] = found
            # Keep the true per-port widths so a byte-enable we bus or a wider address is
            # declared exactly as the netlist drives it.
            array["port_bits"] = {p: ports[f"{name}_{p}"][1] for p in found}
        addr = ports.get(f"{name}_address0")
        if addr:
            array["addr_bits"] = addr[1]
        data = ports.get(f"{name}_q0") or ports.get(f"{name}_d0")
        if data:
            array["bits"] = data[1]
    return spec


def render(spec: dict) -> str:
    top = spec["top"]
    reset = spec.get("reset", {"name": "ap_rst", "active_low": False, "cycles": 4})
    arrays = spec.get("arrays", [])
    scalars = spec.get("scalars", [])
    ret = spec.get("ret")
    vdir = spec.get("vectors_dir", "rtl_vectors")
    lines: list[str] = []
    add = lines.append

    add("`timescale 1ns/1ps")
    add(f"// Generated by gen_rtl_tb.py ({spec.get('policy_id', 'hls_rtl_cosim_selfcheck_v1')}).")
    add("// Standalone self-checking testbench for the synthesized HLS RTL top.")
    add(f"// Golden expected vectors come from {vdir}/*.mem (golden-C oracle).")
    add(f"module {top}_tb;")
    add("  localparam integer NUM_TESTS = %d;" % int(spec["num_tests"]))
    add("  localparam integer HALF = 5;")
    add(f"  reg ap_clk = 1'b0;")
    add(f"  reg {reset['name']} = 1'b0;")
    add("  reg ap_start = 1'b0;")
    add("  wire ap_done, ap_idle, ap_ready;")
    add("  integer errors = 0;")
    add("  integer warns = 0;")
    add("  integer t, i;")

    for scalar in scalars:
        w = int(scalar["bits"]) - 1
        add(f"  reg [{w}:0] {esc(scalar['name'])};")
        add(f"  reg [{w}:0] {scalar['name']}_vec [0:NUM_TESTS-1];")

    if ret is not None:
        w = int(ret["bits"]) - 1
        add(f"  wire [{w}:0] ap_return;")
        add(f"  reg  [{w}:0] ret_exp [0:NUM_TESTS-1];")
        add(f"  reg  [{w}:0] ret_actual;")

    for array in arrays:
        name = array["name"]
        dw = int(array["bits"]) - 1
        aw = int(array["addr_bits"]) - 1
        depth = int(array["depth"])
        # depth is inlined as a literal (not a per-array localparam) so two arg names that
        # differ only in case cannot collide on a shared uppercase localparam identifier.
        add(f"  reg [{dw}:0] {name}_ram [0:{depth - 1}];")
        if array["dir"] in ("input", "inout"):
            add(f"  reg [{dw}:0] {name}_vec [0:NUM_TESTS*{depth}-1];")
        if array["dir"] in ("output", "inout"):
            add(f"  reg [{dw}:0] {name}_exp [0:NUM_TESTS*{depth}-1];")
            add(f"  reg [31:0] {name}_cmp [0:NUM_TESTS-1];")
        port_bits = array.get("port_bits", {})
        for port in array["ports"]:
            override = port_bits.get(port)
            if port.startswith("address"):
                w = (override - 1) if override else aw
                add(f"  wire [{w}:0] {name}_{port};")
            elif port.startswith("q"):
                w = (override - 1) if override else dw
                add(f"  reg [{w}:0] {name}_{port};")
            elif port.startswith("d"):
                w = (override - 1) if override else dw
                add(f"  wire [{w}:0] {name}_{port};")
            elif port.startswith("we") and override and override > 1:
                add(f"  wire [{override - 1}:0] {name}_{port};")
            else:
                add(f"  wire {name}_{port};")

    conns = [
        ".ap_clk(ap_clk)",
        f".{reset['name']}({reset['name']})",
        ".ap_start(ap_start)",
        ".ap_done(ap_done)",
        ".ap_idle(ap_idle)",
        ".ap_ready(ap_ready)",
    ]
    for array in arrays:
        for port in array["ports"]:
            conns.append(f".{array['name']}_{port}({array['name']}_{port})")
    if spec.get("ap_continue"):
        conns.append(".ap_continue(1'b1)")
    for scalar in scalars:
        conns.append(f".{esc(scalar['name'])}({esc(scalar['name'])})")
    if ret is not None:
        conns.append(".ap_return(ap_return)")
    add(f"  {top} dut (")
    add("    " + ",\n    ".join(conns))
    add("  );")

    add("  always #(HALF) ap_clk = ~ap_clk;")

    for array in arrays:
        name = array["name"]
        ports = array["ports"]
        has_w0 = "we0" in ports
        has_q0 = "q0" in ports
        add("  always @(posedge ap_clk) begin")
        if has_w0:
            add(f"    if ({name}_ce0 && {name}_we0) {name}_ram[{name}_address0] <= {name}_d0;")
        if has_q0:
            add(f"    if ({name}_ce0) {name}_q0 <= {name}_ram[{name}_address0];")
        add("  end")
        if "address1" in ports:
            add("  always @(posedge ap_clk) begin")
            if "we1" in ports:
                add(f"    if ({name}_ce1 && {name}_we1) {name}_ram[{name}_address1] <= {name}_d1;")
            if "q1" in ports:
                add(f"    if ({name}_ce1) {name}_q1 <= {name}_ram[{name}_address1];")
            add("  end")

    add("  initial begin")
    for array in arrays:
        name = array["name"]
        if array["dir"] in ("input", "inout"):
            add(f'    $readmemh("{vdir}/rtl_vec_{name}.mem", {name}_vec);')
        if array["dir"] in ("output", "inout"):
            add(f'    $readmemh("{vdir}/rtl_exp_{name}.mem", {name}_exp);')
            add(f'    $readmemh("{vdir}/rtl_cmp_{name}.mem", {name}_cmp);')
    for scalar in scalars:
        add(f'    $readmemh("{vdir}/rtl_scalar_{scalar["name"]}.mem", {scalar["name"]}_vec);')
    if ret is not None:
        add(f'    $readmemh("{vdir}/rtl_exp_return.mem", ret_exp);')
    add("  end")

    active = "1'b0" if reset["active_low"] else "1'b1"
    inactive = "1'b1" if reset["active_low"] else "1'b0"
    add("  initial begin")
    add(f"    {reset['name']} = {active};")
    add("    ap_start = 1'b0;")
    add(f"    repeat ({int(reset['cycles'])}) @(posedge ap_clk);")
    add(f"    #1 {reset['name']} = {inactive};")
    add("    for (t = 0; t < NUM_TESTS; t = t + 1) begin")
    for array in arrays:
        name = array["name"]
        depth = int(array["depth"])
        if array["dir"] in ("input", "inout"):
            add(f"      for (i = 0; i < {depth}; i = i + 1) {name}_ram[i] = {name}_vec[t*{depth} + i];")
        else:
            add(f"      for (i = 0; i < {depth}; i = i + 1) {name}_ram[i] = 0;")
    for scalar in scalars:
        add(f"      {esc(scalar['name'])} = {scalar['name']}_vec[t];")
    # Handshake sampled on negedge: values are stable there (post-NBA), which avoids the
    # posedge race where a lingering ap_done pulse from the previous transaction would be
    # read before its clear, capturing a stale ap_return / stale output memory.
    add("      @(negedge ap_clk);")
    add("      ap_start = 1'b1;")
    add("      @(negedge ap_clk);")
    add("      while (ap_done !== 1'b1) @(negedge ap_clk);")
    if ret is not None:
        add("      ret_actual = ap_return;")
    add("      ap_start = 1'b0;")
    add("      @(negedge ap_clk);")
    for array in arrays:
        if array["dir"] not in ("output", "inout"):
            continue
        name = array["name"]
        depth = int(array["depth"])
        advisory = bool(array.get("advisory"))
        kind = "WARN" if advisory else "MISMATCH"
        counter = "warns" if advisory else "errors"
        add(f"      for (i = 0; i < {name}_cmp[t]; i = i + 1) begin")
        add(f"        if ({name}_ram[i] !== {name}_exp[t*{depth} + i]) begin")
        add(f'          $display("RTL_TB: {kind} test=%0d {name}[%0d] expected=%h actual=%h", t, i, {name}_exp[t*{depth} + i], {name}_ram[i]);')
        add(f"          {counter} = {counter} + 1;")
        add("        end")
        add("      end")
    if ret is not None:
        advisory = bool(ret.get("advisory"))
        kind = "WARN" if advisory else "MISMATCH"
        counter = "warns" if advisory else "errors"
        add("      if (ret_actual !== ret_exp[t]) begin")
        add(f'        $display("RTL_TB: {kind} test=%0d return expected=%h actual=%h", t, ret_exp[t], ret_actual);')
        add(f"        {counter} = {counter} + 1;")
        add("      end")
    add("    end")
    has_observable = any(a["dir"] in ("output", "inout") for a in arrays) or ret is not None
    if not has_observable:
        # A void top with no output/inout array has nothing observable to check, so a bare
        # "PASS" would be vacuous. Say so explicitly instead of implying equivalence.
        add('    $display("RTL_TB: NOTE no observable outputs to compare; PASS means the design only reached ap_done");')
    add('    if (errors == 0) $display("RTL_TB: PASS %0d tests (%0d advisory warnings)", NUM_TESTS, warns);')
    add('    else $display("RTL_TB: FAIL %0d mismatches (%0d advisory warnings)", errors, warns);')
    add("    $finish;")
    add("  end")

    add("  initial begin")
    add("    #100000000;")
    add('    $display("RTL_TB: FAIL timeout before all tests completed");')
    add("    $finish;")
    add("  end")
    add("endmodule")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a self-checking RTL testbench")
    parser.add_argument("--manifest", default=str(ROOT / "tb" / "rtl_tb_manifest.json"))
    parser.add_argument("--from-rtl", default="", help="synthesized Verilog file for the top module")
    parser.add_argument("--from-contract", action="store_true", help="use the interface contract only")
    parser.add_argument("--out", default="", help="output .sv path (default tb/<top>_tb.sv)")
    args = parser.parse_args()

    spec = load_spec(Path(args.manifest))
    top = spec["top"]
    if args.from_rtl and not args.from_contract:
        rtl_path = Path(args.from_rtl)
        ports = parse_rtl_ports(rtl_path.read_text(encoding="utf-8", errors="replace"), top)
        if ports:
            spec = reconcile(spec, ports)
        else:
            print(f"gen_rtl_tb: module {top!r} not found in {rtl_path}; using the interface contract")
    out = Path(args.out) if args.out else (ROOT / "tb" / f"{top}_tb.sv")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(spec), encoding="utf-8")
    print(f"gen_rtl_tb: wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


_RUN_RTL_SIM = r'''#!/usr/bin/env python3
"""Drive the synthesized HLS RTL through the standalone self-checking testbench.

Portable by design: if the synthesized RTL or an RTL simulator is missing, this writes a
`skipped` report and exits 0, so a generated project stays runnable without Vitis or a
simulator installed. Supported simulators: AMD xsim (xvlog/xelab/xsim) and Icarus Verilog
(iverilog/vvp).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_DIR = ROOT / "coverage"
REPORT_PATH = COVERAGE_DIR / "rtl_tb_report.json"
MANIFEST = ROOT / "tb" / "rtl_tb_manifest.json"


def rtl_sources(rtl_dir: Path) -> list[Path]:
    return sorted(rtl_dir.glob("*.v")) + sorted(rtl_dir.glob("*.sv"))


def write_report(payload: dict) -> None:
    COVERAGE_DIR.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def run(cmd, **kwargs):
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, **kwargs)


def find_rtl_dir() -> Path | None:
    override = os.environ.get("C2HLSC_RTL_DIR")
    candidates = []
    if override:
        candidates.append(Path(override))
    candidates.append(ROOT / "c2hlsc_project" / "solution1" / "syn" / "verilog")
    for path in candidates:
        if path.is_dir() and rtl_sources(path):
            return path
    return None


def ensure_vectors(spec: dict, logs: list) -> bool:
    vdir = ROOT / spec.get("vectors_dir", "rtl_vectors")
    top = spec["top"]
    needed = vdir.exists() and any(vdir.glob("rtl_*.mem"))
    if needed and not os.environ.get("C2HLSC_RTL_FORCE_VECTORS"):
        return True
    cxx = os.environ.get("CXX", "g++")
    if shutil.which(cxx) is None:
        return False
    vdir.mkdir(parents=True, exist_ok=True)
    exe = ROOT / "rtl_vectors_tb"
    flags = ["-std=c++17", "-O0", "-I", "src"]
    compile_cmd = [cxx, *flags, "tb/rtl_vectors_tb.cpp", "-o", str(exe)]
    built = run(compile_cmd)
    logs.append({"cmd": compile_cmd, "returncode": built.returncode, "stderr": built.stderr[-4000:]})
    if built.returncode != 0:
        return False
    ran = run([str(exe)])
    logs.append({"cmd": [str(exe)], "returncode": ran.returncode, "stdout": ran.stdout[-2000:], "stderr": ran.stderr[-2000:]})
    return ran.returncode == 0


def generate_sv(spec: dict, rtl_dir: Path | None, logs: list) -> Path:
    top = spec["top"]
    gen = ["python3", "tb/gen_rtl_tb.py"]
    if rtl_dir is not None:
        top_v = None
        module_re = re.compile(r"\bmodule\s+" + re.escape(top) + r"\b")
        for path in rtl_sources(rtl_dir):
            if module_re.search(path.read_text(encoding="utf-8", errors="replace")):
                top_v = path
                break
        if top_v is not None:
            gen += ["--from-rtl", str(top_v)]
    else:
        gen += ["--from-contract"]
    result = run(gen)
    logs.append({"cmd": gen, "returncode": result.returncode, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]})
    return ROOT / "tb" / f"{top}_tb.sv"


def simulate(spec: dict, tb: Path, rtl_dir: Path, logs: list) -> str:
    top = spec["top"]
    # Absolute paths so a synthesized-RTL directory outside the project root
    # (e.g. C2HLSC_RTL_DIR=/opt/vitis/.../verilog) does not crash on relative_to.
    rtl_files = [str(p) for p in rtl_sources(rtl_dir)]
    tb_rel = str(tb)

    if shutil.which("xvlog") and shutil.which("xelab") and shutil.which("xsim"):
        comp = run(["xvlog", "--sv", tb_rel, *rtl_files])
        logs.append({"simulator": "xvlog", "returncode": comp.returncode, "stdout": comp.stdout[-4000:], "stderr": comp.stderr[-4000:]})
        elab = run(["xelab", f"{top}_tb", "-s", "rtl_tb_sim", "-timescale", "1ns/1ps"])
        logs.append({"simulator": "xelab", "returncode": elab.returncode, "stdout": elab.stdout[-4000:], "stderr": elab.stderr[-4000:]})
        if comp.returncode != 0 or elab.returncode != 0:
            return ""
        sim = run(["xsim", "rtl_tb_sim", "-runall"])
        logs.append({"simulator": "xsim", "returncode": sim.returncode, "stdout": sim.stdout[-8000:], "stderr": sim.stderr[-4000:]})
        return sim.stdout

    if shutil.which("iverilog") and shutil.which("vvp"):
        COVERAGE_DIR.mkdir(exist_ok=True)
        vvp = COVERAGE_DIR / "rtl_tb.vvp"
        compiled = run(["iverilog", "-g2012", "-o", str(vvp), "-s", f"{top}_tb", tb_rel, *rtl_files])
        logs.append({"simulator": "iverilog", "step": "compile", "returncode": compiled.returncode, "stderr": compiled.stderr[-4000:]})
        if compiled.returncode != 0:
            return ""
        sim = run(["vvp", str(vvp)])
        logs.append({"simulator": "vvp", "returncode": sim.returncode, "stdout": sim.stdout[-8000:], "stderr": sim.stderr[-4000:]})
        return sim.stdout

    return "__NO_SIM__"


def main() -> int:
    if not MANIFEST.exists():
        write_report({"status": "skipped", "reason": "tb/rtl_tb_manifest.json not found"})
        print("RTL cosim skipped: manifest not found")
        return 0
    spec = json.loads(MANIFEST.read_text(encoding="utf-8"))
    logs: list = []

    rtl_dir = find_rtl_dir()
    if not ensure_vectors(spec, logs):
        write_report({"status": "skipped", "reason": "golden vectors unavailable (need g++)", "commands": logs})
        print("RTL cosim skipped: could not build golden vectors (g++ missing)")
        return 0

    tb = generate_sv(spec, rtl_dir, logs)

    if rtl_dir is None:
        write_report({
            "status": "skipped",
            "reason": "synthesized RTL not found; run csynth (make vitis) first",
            "testbench": str(tb.relative_to(ROOT)) if tb.exists() else None,
            "commands": logs,
        })
        print("RTL cosim skipped: synthesized RTL not found (run synthesis first)")
        return 0

    stdout = simulate(spec, tb, rtl_dir, logs)
    if stdout == "__NO_SIM__":
        write_report({"status": "skipped", "reason": "no RTL simulator (xsim/iverilog) found", "commands": logs})
        print("RTL cosim skipped: no RTL simulator found")
        return 0

    passed = "RTL_TB: PASS" in stdout
    failed = "RTL_TB: FAIL" in stdout or "RTL_TB: MISMATCH" in stdout
    status = "pass" if (passed and not failed) else "fail"
    write_report({
        "status": status,
        "policy_id": spec.get("policy_id"),
        "top": spec["top"],
        "rtl_dir": str(rtl_dir),
        "testbench": str(tb.relative_to(ROOT)),
        "stdout_tail": stdout[-8000:],
        "commands": logs,
    })
    print(f"RTL cosim {status}: report written to {REPORT_PATH}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def generate_verilog_testbenches(analysis: AnalysisResult, config: AgentConfig) -> VerilogTBBundle:
    spec = build_spec(analysis, config)
    return VerilogTBBundle(
        vectors_tb=_vectors_tb(analysis, config, spec),
        gen_script=_GEN_RTL_TB,
        run_script=_RUN_RTL_SIM,
        manifest_json=_manifest(analysis, config, spec),
    )
