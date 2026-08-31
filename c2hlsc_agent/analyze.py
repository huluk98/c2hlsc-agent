from __future__ import annotations

import ast
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


#: Scalar parameter names that conventionally carry the *active* length of a companion
#: array. When one is present and its configured range fits inside the array, every
#: testbench tier compares only that many elements — the rest of the buffer is outside
#: the declared contract and comparing it would report a false mismatch.
LENGTH_NAMES = {
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


def looks_like_length_name(scalar_name: str, array_name: str) -> bool:
    name = scalar_name.lower()
    array = array_name.lower()
    return (
        name in LENGTH_NAMES
        or name in {f"{array}_n", f"n_{array}", f"{array}_len", f"{array}_length", f"{array}_size", f"{array}_count"}
        or name.startswith("num_")
        or name.endswith("_len")
        or name.endswith("_length")
        or name.endswith("_size")
        or name.endswith("_count")
    )


def active_length_arg(array_arg: FunctionArg, scalars: list[FunctionArg]) -> FunctionArg | None:
    """The bounded scalar that acts as ``array_arg``'s active length, if any."""

    for scalar in scalars:
        if not scalar.scalar_range:
            continue
        lo, hi = scalar.scalar_range
        if lo < 0 or array_arg.length is None or hi > array_arg.length:
            continue
        if looks_like_length_name(scalar.name, array_arg.name):
            return scalar
    return None


_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)[ \t]+([^\n/]+)", re.M)
_CONST_RE = re.compile(r"\bconst\s+(?:unsigned\s+|signed\s+)?(?:int|long|short|char|size_t)\s+(\w+)\s*=\s*([^;]+);")
_ENUM_RE = re.compile(r"\benum\s*\w*\s*\{([^}]*)\}")
_LOCAL_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"', re.M)

_CONST_EXPR_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Div, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.LShift, ast.RShift, ast.BitOr, ast.BitAnd, ast.BitXor, ast.Invert,
)


def local_include_text(path: Path, depth: int = 0, seen: set[Path] | None = None) -> str:
    """Concatenated text of the headers ``path`` includes with quotes, recursively.

    Compile-time constants routinely live in a companion header -- the HLS-LeVeri
    benchmark keeps every array bound in ``test.h`` -- so resolving a bound means reading
    more than the one file the top was found in.
    """

    seen = seen if seen is not None else set()
    resolved = path.resolve()
    if depth > 4 or resolved in seen or not path.exists():
        return ""
    seen.add(resolved)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    parts = [text]
    for name in _LOCAL_INCLUDE_RE.findall(text):
        parts.append(local_include_text(path.parent / name, depth + 1, seen))
    return "\n".join(parts)


def collect_constants(text: str) -> dict[str, int]:
    """Integer compile-time constants: ``#define``, ``const int``, and enumerators.

    Resolved iteratively so a constant defined in terms of earlier ones
    (``#define TOTAL (ROWS * COLS)``) still lands.
    """

    raw: dict[str, str] = {}
    for name, value in _DEFINE_RE.findall(text):
        raw.setdefault(name, value.strip())
    for name, value in _CONST_RE.findall(text):
        raw.setdefault(name, value.strip())
    for body in _ENUM_RE.findall(text):
        counter = 0
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                key, _, value = item.partition("=")
                raw.setdefault(key.strip(), value.strip())
                try:
                    counter = int(value.strip(), 0) + 1
                except ValueError:
                    counter += 1
            else:
                raw.setdefault(item.split()[0], str(counter))
                counter += 1

    constants: dict[str, int] = {}
    for _ in range(4):  # a few passes let chained definitions resolve
        progressed = False
        for name, expression in raw.items():
            if name in constants:
                continue
            value = eval_const_expr(expression, constants)
            if value is not None:
                constants[name] = value
                progressed = True
        if not progressed:
            break
    return constants


def eval_const_expr(expression: str, constants: dict[str, int]) -> int | None:
    """Evaluate a C integer constant expression, or ``None`` if it is not constant.

    Only arithmetic over integer literals and known constants is accepted -- anything
    with a call, subscript, attribute or unknown name is not a compile-time bound and
    must keep being treated as one.
    """

    text = expression.strip().rstrip(";").strip()
    if not text:
        return None
    text = re.sub(r"\b([0-9]+)[uUlL]+\b", r"\1", text)          # 16u / 32UL -> 16 / 32
    text = re.sub(r"\((?:unsigned|signed|int|long|short|char|size_t)[ \t*]*\)", "", text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, _CONST_EXPR_NODES):
            return None
        if isinstance(node, ast.Name) and node.id not in constants:
            return None
        if isinstance(node, ast.Constant) and not isinstance(node.value, int):
            return None
    try:
        value = eval(compile(tree, "<const>", "eval"), {"__builtins__": {}}, dict(constants))
    except Exception:
        return None
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


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


def _parse_arg(
    raw: str,
    metadata: ArgumentConfig | None = None,
    constants: dict[str, int] | None = None,
) -> FunctionArg:
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
    direction = metadata.direction or ("input" if is_const or (pointer_depth == 0 and not array_dims) else "inout")
    length = metadata.length
    if length is None:
        # A bound is rarely a bare literal in real HLS code: `float a[NUM_FEATURE]` and
        # `float b[ROWS*COLS]` are the norm. Resolving them keeps the testbench buffers
        # the size the design actually indexes, instead of silently falling back to 16.
        resolved = [eval_const_expr(dim, constants or {}) for dim in array_dims]
        usable = [v for v in resolved if v is not None and v > 0]
        if usable and len(usable) == len(array_dims):
            # Every dimension of a multi-dimensional parameter counts: `int a[N][60]`
            # holds N*60 elements, and sizing the buffer from the first dimension alone
            # would under-allocate it by a factor of 60.
            total = 1
            for value in usable:
                total *= value
            length = total
        elif usable:
            length = usable[0]
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


def _extract_function(
    source: str,
    top: str,
    source_path: Path,
    config: AgentConfig,
    constants: dict[str, int] | None = None,
) -> FunctionInfo:
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
    args = [
        _parse_arg(part, config.arguments.get(_guess_arg_name(part)), constants)
        for part in _split_params(params)
    ]
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
        # One or more subscripts: a 2-D write is `table[i][j] = ...`, so anchoring the
        # assignment to a single `]` classified every multi-dimensional output array as an
        # input. That silently emptied the compare set -- the oracle then verified nothing
        # and reported pass.
        write_pattern = rf"(?:\*\s*{name}|{name}\s*(?:\[[^\]]+\])+)\s*(?:=(?!=)|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|\+\+|--)"
        writes = bool(re.search(write_pattern, body))
        body_without_lhs_writes = re.sub(write_pattern, "", body)
        reads = bool(re.search(rf"(?:\*\s*{name}|{name}\s*(?:\[[^\]]+\])+|{name}\s*\+)", body_without_lhs_writes))
        if writes and reads:
            arg.direction = "inout"
        elif writes:
            arg.direction = "output"
        else:
            arg.direction = "input"


def _unsupported(function: FunctionInfo, constants: dict[str, int] | None = None) -> list[Diagnostic]:
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
        bound = local_array.group(1)
        # `a[N]` and `a[ROWS*COLS]` are fixed bounds whenever those names are compile-time
        # constants, which is how almost all synthesizable C declares its arrays. Only a
        # bound that cannot be evaluated is a genuine variable-length array.
        if eval_const_expr(bound, constants or {}) is not None:
            continue
        diagnostics.append(Diagnostic("error", "variable-length-array", f"variable-length array bound {bound!r} detected", function.source_path.name, "Use fixed compile-time bounds or caller-managed buffers."))
    return diagnostics


def _type_mappings(function: FunctionInfo) -> list[dict[str, str]]:
    rows = [{"name": "return", "original": function.return_type, "generated": function.return_type}]
    for arg in function.args:
        rows.append({"name": arg.name, "original": arg.c_type, "generated": arg.c_type})
    return rows


def _require_observable_output(function: FunctionInfo, diagnostics: DiagnosticBag) -> None:
    """Refuse a top whose behaviour nothing can observe.

    The oracle compares the return value and every output/inout argument. When that set is
    empty there is nothing to assert, so the testbench passes for *any* implementation --
    an empty function body included. Reporting `pass` for a run that verified nothing is
    worse than reporting a failure, so this is an error rather than the comment it used to
    be: a benchmark number built from vacuous passes is not a result.
    """

    observable = function.return_type != "void" or any(
        arg.direction in {"output", "inout"} for arg in function.args if arg.is_pointer_like
    )
    if observable:
        return
    diagnostics.add(
        "error",
        "nothing-to-compare",
        f"'{function.name}' returns void and has no output or inout argument, so the oracle "
        "would compare nothing and pass for any implementation",
        function.source_path.name,
        "Declare the written argument as output/inout in the config if direction inference missed it.",
    )


def _require_a_test_to_run(config: AgentConfig, diagnostics: DiagnosticBag, source: Path) -> None:
    """Refuse a schedule that drives the design zero times.

    ``num_tests = 0`` produces a testbench that reports "all 0 tests passed" and a run that
    reports pass, having executed nothing. It is the same vacuity as an empty compare set,
    reached from the other end.
    """

    if config.num_tests >= 1:
        return
    diagnostics.add(
        "error",
        "no-tests-scheduled",
        f"num_tests is {config.num_tests}, so the oracle would run the design zero times "
        "and pass without executing anything",
        source.name,
        "Set num_tests to at least 1.",
    )


def analyze_source(input_file: Path, top: str, config: AgentConfig) -> AnalysisResult:
    source = input_file.read_text(encoding="utf-8")
    constants = collect_constants(strip_comments(local_include_text(input_file)))
    diagnostics = DiagnosticBag()
    function = _extract_function(source, top, input_file, config, constants)
    _infer_pointer_directions(function, config)
    _require_observable_output(function, diagnostics)
    _require_a_test_to_run(config, diagnostics, input_file)
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
    unsupported = _unsupported(function, constants)
    diagnostics.extend(unsupported)
    return AnalysisResult(function, diagnostics, _type_mappings(function), unsupported)
