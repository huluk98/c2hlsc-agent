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


def _constant_dim(dim: str) -> int | None:
    """Fold a literal array bound such as ``64 * 64`` into its value.

    Only literal arithmetic is folded. A bound naming a macro cannot be resolved from
    here, because the parser sees one parameter at a time rather than the translation
    unit; those fall through to the configured length instead.
    """

    text = re.sub(r"\b(\d+)[uUlL]+\b", r"\1", dim.strip())
    if not text:
        return None
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None

    def fold(node: ast.AST) -> int | None:
        if isinstance(node, ast.Expression):
            return fold(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
            return fold(node.operand)
        if isinstance(node, ast.BinOp):
            left = fold(node.left)
            right = fold(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, (ast.Div, ast.FloorDiv)):
                return left // right if right else None
            if isinstance(node.op, ast.LShift):
                return left << right if 0 <= right < 64 else None
            return None
        return None

    value = fold(tree)
    return value if value is not None and value > 0 else None


# `T (*p)[4][4]` -- a pointer to an array, as AES spells its state. The parentheses
# bind the star to the name, so a naive split leaves the closing paren stuck to it.
_POINTER_TO_ARRAY = re.compile(r"\(\s*(?P<stars>\*+)\s*(?P<name>[A-Za-z_]\w*)\s*\)")


def _parse_arg(raw: str, metadata: ArgumentConfig | None = None) -> FunctionArg:
    raw = raw.strip()
    # `restrict` is a C99 keyword that is not valid C++. Drop it from the parameter text
    # so the generated header/definition signatures (built from FunctionArg.raw) compile.
    raw = re.sub(r"\b(?:restrict|__restrict|__restrict__)\b", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    array_dims = re.findall(r"\[([^\]]*)\]", raw)
    raw_no_arrays = re.sub(r"\[[^\]]*\]", "", raw).strip()

    declarator = _POINTER_TO_ARRAY.search(raw_no_arrays)
    if declarator:
        pointer_depth = len(declarator.group("stars"))
        name = declarator.group("name")
        c_type = raw_no_arrays[: declarator.start()].strip()
        return _finish_arg(raw, name, c_type, pointer_depth, array_dims, metadata)

    pointer_depth = raw_no_arrays.count("*")
    tokens = raw_no_arrays.replace("*", " * ").split()
    if not tokens:
        raise ValueError(f"cannot parse argument: {raw}")
    name = tokens[-1]
    type_tokens = [t for t in tokens[:-1] if t not in {"*", "restrict", "__restrict", "__restrict__"}]
    c_type = " ".join(type_tokens).strip()
    return _finish_arg(raw, name, c_type, pointer_depth, array_dims, metadata)


def _finish_arg(
    raw: str,
    name: str,
    c_type: str,
    pointer_depth: int,
    array_dims: list[str],
    metadata: ArgumentConfig | None,
) -> FunctionArg:
    c_type = re.sub(r"\s+", " ", c_type).strip()
    is_const = "const" in c_type.split()
    if metadata is None:
        metadata = ArgumentConfig()
    direction = metadata.direction or ("input" if is_const or pointer_depth == 0 and not array_dims else "inout")
    length = metadata.length
    if length is None:
        # The testbench models every array argument as one flat buffer, so a
        # multi-dimensional bound contributes its product. Taking only the first
        # dimension under-allocates and the generated test indexes past its own array.
        folded = [_constant_dim(dim) for dim in array_dims]
        if folded and all(value is not None for value in folded):
            product = 1
            for value in folded:
                product *= value
            length = product
        else:
            for value in folded:
                if value is not None:
                    length = value
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
    declarator = _POINTER_TO_ARRAY.search(raw_no_arrays)
    if declarator:
        return declarator.group("name")
    return raw_no_arrays.replace("*", " * ").split()[-1]


def _pointer_escapes(name: str, body: str) -> bool:
    """Report whether a pointer argument flows somewhere the write patterns cannot follow.

    ``_infer_pointer_directions`` can only see writes spelled through the argument
    itself (``p[i] = ...`` or ``*p = ...``). Aliasing the pointer into a local, or
    handing it to a helper such as ``memcpy``, moves the write under a different name,
    so a written-to argument would otherwise be classified ``input`` and then silently
    dropped from the equivalence comparison.
    """

    bare = rf"(?<![A-Za-z0-9_*&]){re.escape(name)}(?![A-Za-z0-9_])\s*(?!\[)"
    aliased = re.search(rf"=\s*(?:\([^)]*\)\s*)?{bare}", body)
    passed = re.search(rf"\b[A-Za-z_]\w*\s*\([^;{{}}]*{bare}", body)
    return bool(aliased or passed)


def _infer_pointer_directions(function: FunctionInfo, config: AgentConfig) -> list[str]:
    """Assign a direction to every pointer argument, returning the unprovable ones."""

    body = strip_comments(function.body)
    unprovable: list[str] = []
    for arg in function.args:
        if not arg.is_pointer_like:
            continue
        if arg.name in config.arguments and config.arguments[arg.name].direction:
            continue
        if not arg.is_const and _pointer_escapes(arg.name, body):
            # The pointer escapes and nothing in the C type forbids a write, so a
            # read-only classification cannot be justified. "inout" is the safe
            # choice: the argument still receives real stimulus and is compared.
            arg.direction = "inout"
            unprovable.append(arg.name)
            continue
        name = re.escape(arg.name)
        # Every spelling of a write through this argument. The parenthesised and
        # arrow forms matter for pointers to arrays and to structs: AES writes its
        # state as `(*state)[i][j] ^= ...`, which reads as no write at all unless
        # the deref is matched with the subscripts that follow it.
        write_targets = "|".join(
            (
                rf"\*\s*{name}",                                 # *p =
                rf"{name}(?:\s*\[[^\]]+\])+",                    # p[i] =, p[i][j] =
                rf"\(\s*\*\s*{name}\s*\)(?:\s*\[[^\]]+\])*",  # (*p)[i][j] =, (*p) =
                rf"{name}\s*->\s*\w+",                          # p->field =
            )
        )
        write_ops = r"(?:=(?!=)|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|\+\+|--)"
        write_pattern = rf"(?:{write_targets})\s*{write_ops}"
        writes = bool(re.search(write_pattern, body))
        body_without_lhs_writes = re.sub(write_pattern, "", body)
        read_pattern = "|".join(
            (
                rf"\*\s*{name}",
                rf"{name}\s*\[[^\]]+\]",
                rf"\(\s*\*\s*{name}\s*\)",
                rf"{name}\s*->\s*\w+",
                rf"{name}\s*\+",
            )
        )
        reads = bool(re.search(rf"(?:{read_pattern})", body_without_lhs_writes))
        if writes and reads:
            arg.direction = "inout"
        elif writes:
            arg.direction = "output"
        else:
            arg.direction = "input"
    return unprovable


def _compile_time_constants(source: str) -> set[str]:
    """Collect names usable as a fixed array bound: macros, const/constexpr, enumerators."""

    names: set[str] = set()
    for match in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+\S", source, re.M):
        names.add(match.group(1))
    for match in re.finditer(r"\bconst(?:expr)?\s+[\w\s*]*?\b([A-Za-z_]\w*)\s*=", source):
        names.add(match.group(1))
    for match in re.finditer(r"\benum\b[^{;]*\{([^}]*)\}", source):
        for item in match.group(1).split(","):
            name = item.split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                names.add(name)
    return names


def _unsupported(function: FunctionInfo, full_source: str = "") -> list[Diagnostic]:
    body = strip_comments(function.body)
    constants = _compile_time_constants(full_source or function.definition)
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
        bound = local_array.group(1).strip()
        identifiers = set(re.findall(r"[A-Za-z_]\w*", bound))
        if identifiers and identifiers <= constants:
            # Every name in the bound resolves to a compile-time constant, so the array
            # has a fixed size even though the bound is not spelled as a literal.
            continue
        diagnostics.append(Diagnostic("error", "variable-length-array", f"variable-length array bound {bound!r} detected", function.source_path.name, "Use fixed compile-time bounds or caller-managed buffers."))
    return diagnostics


def _is_integer_scalar(c_type: str) -> bool:
    tokens = set(c_type.replace("*", " ").split())
    if tokens & {"float", "double"}:
        return False
    if tokens & {"int", "char", "short", "long", "unsigned", "signed", "size_t", "ssize_t", "bool"}:
        return True
    return bool(re.search(r"\bu?int\d+_t\b", c_type))


def _has_observable_output(function: FunctionInfo) -> bool:
    """Report whether the testbench will have anything to compare.

    Mirrors the comparison rule in ``testgen``: a non-void return, or at least one
    pointer argument carrying data back out. With neither, every stimulus trivially
    agrees and a passing run says nothing about equivalence.
    """

    if function.return_type != "void":
        return True
    return any(arg.direction in {"output", "inout"} for arg in function.args if arg.is_pointer_like)


def unsupported_in_generated(source: str, top: str, config: AgentConfig, label: str) -> list[Diagnostic]:
    """Re-run the synthesizability checks against generated HLS-C.

    Diagnostics from the *input* mean "this source needs transforming"; diagnostics
    from the *output* mean the transformation did not succeed. Only the latter should
    decide whether a conversion failed.
    """

    try:
        function = _extract_function(source, top, Path(label), config)
    except Exception:
        # A top the extractor cannot parse is reported by the verifier instead; the
        # compile step gives a far better message than a guess from here would.
        return []
    return [
        Diagnostic(diag.severity, diag.code, diag.message, label, diag.suggestion)
        for diag in _unsupported(function, source)
    ]


def _type_mappings(function: FunctionInfo) -> list[dict[str, str]]:
    rows = [{"name": "return", "original": function.return_type, "generated": function.return_type}]
    for arg in function.args:
        rows.append({"name": arg.name, "original": arg.c_type, "generated": arg.c_type})
    return rows


def analyze_source(input_file: Path, top: str, config: AgentConfig) -> AnalysisResult:
    source = input_file.read_text(encoding="utf-8")
    diagnostics = DiagnosticBag()
    function = _extract_function(source, top, input_file, config)
    for name in _infer_pointer_directions(function, config):
        diagnostics.add(
            "warning",
            "unprovable-pointer-direction",
            f"argument {name!r} escapes the top function, so a read-only direction cannot be proven; comparing it as 'inout'",
            input_file.name,
            "Set arguments.<name>.direction in config.yaml to record the intended direction.",
        )
    if not _has_observable_output(function):
        diagnostics.add(
            "error",
            "no-observable-output",
            "the top function exposes no return value and no output/inout argument, so equivalence cannot be checked",
            input_file.name,
            "Declare the written argument with arguments.<name>.direction: output, or give the top a return value.",
        )
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
    pointer_lengths = [arg.length for arg in function.args if arg.is_pointer_like and arg.length]
    if pointer_lengths:
        # An unranged integer scalar is generated as a full-range random value. Beside a
        # fixed-size array argument that is a loop bound waiting to run off the end, and
        # the resulting segfault surfaces only as a make failure with no diagnostic.
        safe_bound = min(pointer_lengths)
        for arg in function.args:
            if arg.is_pointer_like or arg.scalar_range or not _is_integer_scalar(arg.c_type):
                continue
            arg.scalar_range = (0, safe_bound)
            diagnostics.add(
                "warning",
                "unbounded-scalar-stimulus",
                f"scalar {arg.name!r} has no configured range; clamping stimulus to [0, {safe_bound}] so it cannot index past the shortest array argument",
                input_file.name,
                "Set arguments.<name>.range in config.yaml to exercise the intended input space.",
            )
    unsupported = _unsupported(function, source)
    diagnostics.extend(unsupported)
    return AnalysisResult(function, diagnostics, _type_mappings(function), unsupported)
