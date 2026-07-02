from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import AgentConfig, ArgumentConfig
from .diagnostics import Diagnostic, DiagnosticBag


@dataclass
class FunctionArg:
    raw: str
    name: str
    c_type: str
    pointer_depth: int = 0
    array_dims: list[str] = field(default_factory=list)
    is_const: bool = False
    direction: str = "input"
    length: int | None = None
    scalar_range: tuple[int, int] | None = None
    interface: str | None = None

    @property
    def is_pointer_like(self) -> bool:
        return self.pointer_depth > 0 or bool(self.array_dims)


@dataclass
class FunctionInfo:
    name: str
    return_type: str
    args: list[FunctionArg]
    signature: str
    body: str
    definition: str
    source_path: Path


@dataclass
class AnalysisResult:
    function: FunctionInfo
    diagnostics: DiagnosticBag
    type_mappings: list[dict[str, str]]
    unsupported_constructs: list[Diagnostic]


def strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//.*", "", source)
    return source


def _find_matching_brace(source: str, open_index: int) -> int:
    depth = 0
    in_string: str | None = None
    escape = False
    for idx in range(open_index, len(source)):
        ch = source[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in {'"', "'"}:
            in_string = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    raise ValueError("unmatched function body brace")


def _split_params(params: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in params:
        if ch == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                out.append(part)
            current = []
            continue
        current.append(ch)
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
    part = "".join(current).strip()
    if part and part != "void":
        out.append(part)
    return out


def _parse_arg(raw: str, metadata: ArgumentConfig | None = None) -> FunctionArg:
    raw = raw.strip()
    # `restrict` is a C99 keyword that is not valid C++. Drop it from the parameter text
    # so the generated header/definition signatures (built from FunctionArg.raw) compile.
    raw = re.sub(r"\b(?:restrict|__restrict|__restrict__)\b", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    array_dims = re.findall(r"\[([^\]]*)\]", raw)
    raw_no_arrays = re.sub(r"\[[^\]]*\]", "", raw).strip()
    pointer_depth = raw_no_arrays.count("*")
    tokens = raw_no_arrays.replace("*", " * ").split()
    if not tokens:
        raise ValueError(f"cannot parse argument: {raw}")
    name = tokens[-1]
    type_tokens = [t for t in tokens[:-1] if t not in {"*", "restrict", "__restrict", "__restrict__"}]
    c_type = " ".join(type_tokens).strip()
    c_type = re.sub(r"\s+", " ", c_type)
    is_const = "const" in c_type.split()
    if metadata is None:
        metadata = ArgumentConfig()
    direction = metadata.direction or ("input" if is_const or pointer_depth == 0 and not array_dims else "inout")
    length = metadata.length
    if length is None:
        for dim in array_dims:
            if dim.strip().isdigit():
                length = int(dim.strip())
                break
    return FunctionArg(
        raw=raw,
        name=name,
        c_type=c_type,
        pointer_depth=pointer_depth,
        array_dims=array_dims,
        is_const=is_const,
        direction=direction,
        length=length,
        scalar_range=metadata.range,
        interface=metadata.interface,
    )


def _extract_function(source: str, top: str, source_path: Path, config: AgentConfig) -> FunctionInfo:
    pattern = re.compile(
        rf"(?P<ret>[A-Za-z_][\w\s\*\d]*?)\s+{re.escape(top)}\s*\((?P<params>[^;{{}}]*)\)\s*\{{",
        flags=re.S,
    )
    match = pattern.search(source)
    if not match:
        raise ValueError(f"top function {top!r} not found")
    open_brace = source.find("{", match.start())
    close_brace = _find_matching_brace(source, open_brace)
    params = match.group("params")
    args = [_parse_arg(part, config.arguments.get(_guess_arg_name(part))) for part in _split_params(params)]
    return_type = re.sub(r"\s+", " ", match.group("ret")).strip()
    signature = f"{return_type} {top}({', '.join(arg.raw for arg in args)})"
    definition = source[match.start() : close_brace + 1].strip()
    body = source[open_brace + 1 : close_brace]
    return FunctionInfo(top, return_type, args, signature, body, definition, source_path)


def _guess_arg_name(raw: str) -> str:
    raw_no_arrays = re.sub(r"\[[^\]]*\]", "", raw).strip()
    return raw_no_arrays.replace("*", " * ").split()[-1]


def _infer_pointer_directions(function: FunctionInfo, config: AgentConfig) -> None:
    body = strip_comments(function.body)
    for arg in function.args:
        if not arg.is_pointer_like:
            continue
        if arg.name in config.arguments and config.arguments[arg.name].direction:
            continue
        name = re.escape(arg.name)
        write_pattern = rf"(?:\*\s*{name}|{name}\s*\[[^\]]+\])\s*(?:=(?!=)|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|\+\+|--)"
        writes = bool(re.search(write_pattern, body))
        body_without_lhs_writes = re.sub(write_pattern, "", body)
        reads = bool(re.search(rf"(?:\*\s*{name}|{name}\s*\[[^\]]+\]|{name}\s*\+)", body_without_lhs_writes))
        if writes and reads:
            arg.direction = "inout"
        elif writes:
            arg.direction = "output"
        else:
            arg.direction = "input"


def _unsupported(function: FunctionInfo) -> list[Diagnostic]:
    body = strip_comments(function.body)
    checks: list[tuple[str, str, str, str | None]] = [
        ("dynamic-allocation", r"\b(malloc|calloc|realloc|free)\s*\(", "dynamic allocation is not synthesizable", "Use fixed-size caller-managed buffers."),
        ("unsupported-stdlib-call", r"\b(rand|srand|qsort|bsearch|time|clock|exit|abort|setjmp|longjmp)\s*\(", "unsupported standard library call inside the top function", "Move non-deterministic or runtime library calls outside the synthesized top."),
        ("system-call", r"\b(system|popen|fork|exec\w*)\s*\(", "system calls are not synthesizable", "Move OS interaction to the testbench."),
        ("file-io", r"\b(fopen|fclose|fread|fwrite|fprintf|fscanf|printf|scanf)\s*\(", "file or console I/O inside the top is not synthesizable", "Move I/O to the testbench."),
        ("function-pointer", r"\(\s*\*\s*\w+\s*\)\s*\(", "function pointer calls are not safely convertible", "Replace indirect calls with explicit branches before conversion."),
        ("unbounded-loop", r"for\s*\(\s*;\s*;\s*\)|while\s*\(\s*1\s*\)", "unbounded loop detected", "Add a statically bounded loop limit."),
    ]
    diagnostics: list[Diagnostic] = []
    for code, pattern, message, suggestion in checks:
        if re.search(pattern, body):
            diagnostics.append(Diagnostic("error", code, message, function.source_path.name, suggestion))
    if re.search(rf"\b{re.escape(function.name)}\s*\(", body):
        diagnostics.append(Diagnostic("error", "recursion", "recursive top function call detected", function.source_path.name, "Refactor recursion into bounded iteration."))
    for arg in function.args:
        if not arg.is_pointer_like:
            continue
        name = re.escape(arg.name)
        pointer_arithmetic_patterns = [
            rf"(?:\+\+|--)\s*{name}\b",
            rf"\b{name}\s*(?:\+\+|--|\+=|-=)",
            rf"\b{name}\s*[+-]\s*[^;\),\]]+",
            rf"\*\s*\(\s*{name}\s*[+-]",
        ]
        if any(re.search(pattern, body) for pattern in pointer_arithmetic_patterns):
            diagnostics.append(
                Diagnostic(
                    "error",
                    "pointer-arithmetic",
                    f"unrestricted pointer arithmetic detected for argument {arg.name!r}",
                    function.source_path.name,
                    "Use indexed array access with a configured bound so the agent can verify generated tests.",
                )
            )
    for local_array in re.finditer(r"\b(?:int|char|short|long|float|double|uint\d+_t|int\d+_t)\s+\w+\s*\[([^\]\d][^\]]*)\]", body):
        diagnostics.append(Diagnostic("error", "variable-length-array", f"variable-length array bound {local_array.group(1)!r} detected", function.source_path.name, "Use fixed compile-time bounds or caller-managed buffers."))
    return diagnostics


def _type_mappings(function: FunctionInfo) -> list[dict[str, str]]:
    rows = [{"name": "return", "original": function.return_type, "generated": function.return_type}]
    for arg in function.args:
        rows.append({"name": arg.name, "original": arg.c_type, "generated": arg.c_type})
    return rows


def analyze_source(input_file: Path, top: str, config: AgentConfig) -> AnalysisResult:
    source = input_file.read_text(encoding="utf-8")
    diagnostics = DiagnosticBag()
    function = _extract_function(source, top, input_file, config)
    _infer_pointer_directions(function, config)
    for arg in function.args:
        if arg.is_pointer_like and arg.length is None:
            arg.length = 16
            diagnostics.add(
                "warning",
                "missing-pointer-bound",
                f"argument {arg.name!r} has no configured bound; using conservative test length 16",
                input_file.name,
                "Set arguments.<name>.length in config.yaml.",
            )
    unsupported = _unsupported(function)
    diagnostics.extend(unsupported)
    return AnalysisResult(function, diagnostics, _type_mappings(function), unsupported)
