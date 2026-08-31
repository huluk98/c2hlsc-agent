"""Transitive file-scope closure extraction for a top function.

Why this module exists
----------------------

The generated translation unit was not self-contained. :func:`convert._include_for_types`
handed the generated header and source a hardcoded two-entry declaration set --
``#include <stdint.h>``, plus ``<ap_int.h>`` when the type text happened to mention it --
and then spliced the top's signature into the header and its body into the source
*verbatim*, carrying none of the typedefs, macros, enums, structs, globals or helper
functions they reference.

That single defect is what every failing row of the CHStone and Rosetta agent rungs
reduces to, wearing three different sets of clothes:

* the header cannot name its own signature (Rosetta: ``DataType``, ``IMAGE_HEIGHT``,
  ``Triangle_3D`` undeclared in ``hls_top.hpp``);
* the source cannot name its own context (CHStone ``gsm``/``jpeg``/``mips``/``sha``:
  ``word``, ``N``, ``main_result``, ``imem``, ``AND`` undeclared in ``hls_top.cpp``);
* the repair agent's *workaround* for the second case -- ``#include "../input.c"`` into a
  ``.cpp`` -- drags C89 into ``g++`` and dies on K&R parameter definitions and tentative
  re-declarations (CHStone ``blowfish``/``motion``).

The general fix is to compute what the top actually closes over and re-emit it as valid
C++, so the generated artifact is one self-contained translation unit. That matters beyond
host equivalence: a TU that needs a separately-linked C object would pass rung 1 and then
have nothing coherent to hand to Vitis for synthesis.

Why libclang
------------

The closure has to be *computed*, not pattern-matched. Two of the three faces above are
exactly the kind of C that regexes get wrong -- K&R definitions whose parameter types live
on their own lines, and macro constants that a preprocessing step would erase before a
parser ever saw them. libclang recovers both: ``BF_cfb64_encrypt``'s six K&R parameters
come back with types and names (so the signature can be re-emitted ANSI-style), and
``PARSE_DETAILED_PROCESSING_RECORD`` keeps ``#define`` cursors addressable (so ``mips``'s
33 opcode constants can be hoisted).

libclang is an *optional* dependency. When it is unavailable this module reports that
fact and the caller keeps its previous behaviour rather than failing the conversion --
see :attr:`ClosureResult.available`.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Cursor kinds that can appear at file scope and be hoisted into the generated TU.
_TYPE_KINDS = {"TYPEDEF_DECL", "STRUCT_DECL", "UNION_DECL", "ENUM_DECL", "CLASS_DECL"}
_VALUE_KINDS = {"VAR_DECL"}
_FUNCTION_KINDS = {"FUNCTION_DECL", "CXX_METHOD", "FUNCTION_TEMPLATE"}
_HOISTABLE = _TYPE_KINDS | _VALUE_KINDS | _FUNCTION_KINDS

# Directories whose declarations are supplied by an #include rather than hoisted. Hoisting
# a libc declaration would collide with the real header the generated TU also includes.
_SYSTEM_HINTS = ("/usr/include", "/usr/lib/gcc", "/usr/lib/llvm", "/usr/local/include",
                 "\\vc\\include", "\\windows kits\\", "/native/", "site-packages")


class LibclangUnavailable(RuntimeError):
    """libclang could not be loaded; the caller must fall back."""


@dataclass
class ClosureResult:
    """What the top function closes over, ready to paste into a generated TU."""

    preamble: str = ""
    """C++-valid declarations: includes, macros, types, globals, prototypes, bodies."""

    type_preamble: str = ""
    """The subset a *header* needs: includes, macros and types only (no definitions)."""

    definition_preamble: str = ""
    """The rest -- globals, prototypes, bodies -- for a source that already includes the
    header. Emitting :attr:`preamble` there instead would re-define every type."""

    symbols: list[str] = field(default_factory=list)
    macros: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    normalizations: list[str] = field(default_factory=list)
    """Human-readable record of every C-to-C++ rewrite applied, for the audit trail."""

    diagnostics: list[str] = field(default_factory=list)
    available: bool = True
    """False when libclang is missing; ``preamble`` is then empty and the caller falls back."""

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "symbols": list(self.symbols),
            "macros": list(self.macros),
            "includes": list(self.includes),
            "has_definitions": bool(self.definition_preamble),
            "normalizations": list(self.normalizations),
            "diagnostics": list(self.diagnostics),
            "symbol_count": len(self.symbols),
            "macro_count": len(self.macros),
        }


# --------------------------------------------------------------------------------------
# libclang discovery
# --------------------------------------------------------------------------------------


def _candidate_library_files() -> list[str]:
    """Places libclang.so/dll/dylib plausibly lives, most specific first."""

    override = os.environ.get("C2HLSC_LIBCLANG")
    found: list[str] = [override] if override else []
    patterns = [
        # the pip `libclang` wheel bundles the shared library next to the bindings
        os.path.join(os.path.dirname(__file__), "..", "**", "clang", "native", "libclang*"),
        "/usr/lib/llvm-*/lib/libclang.so*",
        "/usr/lib/x86_64-linux-gnu/libclang*.so*",
        "/usr/local/lib/libclang*",
        "/opt/homebrew/opt/llvm/lib/libclang.dylib",
        "C:\\Program Files\\LLVM\\bin\\libclang.dll",
        "C:\\msys64\\ucrt64\\bin\\libclang.dll",
    ]
    for pattern in patterns:
        found.extend(sorted(glob.glob(pattern, recursive=True)))
    return [f for f in found if f and os.path.exists(f)]


def _load_clang():
    """Import clang.cindex with a working shared library, or raise LibclangUnavailable."""

    try:
        import clang.cindex as cindex  # type: ignore
    except Exception as exc:  # noqa: BLE001 - optional dependency
        raise LibclangUnavailable(
            f"python bindings for libclang are not installed ({exc}); "
            "install the optional extra: pip install 'c2hlsc-agent[closure]'"
        ) from exc

    try:  # already configured / discoverable on the default search path
        cindex.Index.create()
        return cindex
    except Exception:  # noqa: BLE001 - fall through to explicit discovery
        pass

    for candidate in _candidate_library_files():
        try:
            cindex.Config.set_library_file(candidate)
            cindex.Index.create()
            return cindex
        except Exception:  # noqa: BLE001 - try the next candidate
            cindex.Config.loaded = False
            continue
    raise LibclangUnavailable(
        "libclang shared library not found; set C2HLSC_LIBCLANG to its path or "
        "install the optional extra: pip install 'c2hlsc-agent[closure]'"
    )


def _builtin_include_dirs() -> list[str]:
    """Compiler builtin headers (stddef.h and friends).

    The pip ``libclang`` wheel ships the shared library without clang's own resource
    headers, so every parse otherwise fails at ``'stddef.h' file not found`` inside the
    first system header. Any toolchain's builtin directory serves.
    """

    patterns = [
        "/usr/lib/llvm-*/lib/clang/*/include",
        "/usr/lib/clang/*/include",
        "/usr/lib/gcc/*/*/include",
        "/usr/local/lib/clang/*/include",
        "/opt/homebrew/opt/llvm/lib/clang/*/include",
        "C:\\msys64\\ucrt64\\lib\\clang\\*\\include",
        "C:\\Program Files\\LLVM\\lib\\clang\\*\\include",
    ]
    dirs: list[str] = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            if os.path.exists(os.path.join(path, "stddef.h")):
                dirs.append(path)
    return dirs[:1]


def libclang_status() -> tuple[bool, str]:
    """Report whether closure extraction is usable, and why not when it isn't."""

    try:
        _load_clang()
    except LibclangUnavailable as exc:
        return False, str(exc)
    return True, "libclang available"


# --------------------------------------------------------------------------------------
# closure computation
# --------------------------------------------------------------------------------------


def _is_system(path: str | None) -> bool:
    if not path:
        return True
    lowered = path.replace("\\", "/").lower()
    return any(hint.replace("\\", "/") in lowered for hint in _SYSTEM_HINTS)


def _kind_name(cursor) -> str:
    return str(cursor.kind).rsplit(".", 1)[-1]


def _read(path: str, cache: dict[str, bytes]) -> bytes:
    if path not in cache:
        try:
            cache[path] = Path(path).read_bytes()
        except OSError:
            cache[path] = b""
    return cache[path]


def _extent_text(cursor, cache: dict[str, bytes]) -> str:
    extent = cursor.extent
    if not extent.start.file:
        return ""
    data = _read(extent.start.file.name, cache)
    return data[extent.start.offset : extent.end.offset].decode("utf-8", "replace")


def _original_includes(source_path: Path) -> list[str]:
    """The top file's own #include lines.

    Re-emitting these verbatim is how the generated TU keeps access to libc and to any
    project header, without this module having to hoist (and risk colliding with)
    declarations that a real header already provides.
    """

    includes: list[str] = []
    try:
        text = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return includes
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#include"):
            # Only system includes are re-emitted. Everything a local quoted header
            # declares is hoisted by name, so re-including it would double-define those
            # symbols -- and would tie the generated TU back to the original source tree,
            # which is the opposite of self-contained.
            if "<" not in stripped:
                continue
            if stripped not in includes:
                includes.append(stripped)
    return includes


def _ansi_signature(cursor) -> str:
    """Rebuild a function signature from the parse tree rather than copying source text.

    This is what makes CHStone's K&R definitions compile as C++. ``BF_cfb64_encrypt (in,
    out, length, ivec, num, encrypt)`` followed by six free-standing parameter
    declarations is legal C89 and a hard error for ``g++`` -- but libclang has already
    resolved it, so the ANSI form can simply be printed.
    """

    # A K&R definition parses as FUNCTIONNOPROTO, whose type has no variadic flag at all;
    # asking anyway raises inside the bindings. Treat "cannot tell" as "not variadic".
    try:
        variadic = cursor.type.is_function_variadic()
    except Exception:  # noqa: BLE001 - FUNCTIONNOPROTO and friends
        variadic = False

    params: list[str] = []
    for index, arg in enumerate(cursor.get_arguments()):
        name = arg.spelling or f"_arg{index}"
        type_text = arg.type.spelling
        if "[" in type_text and "(" not in type_text:  # keep array parameter shape
            base, _, dims = type_text.partition("[")
            params.append(f"{base.strip()} {name}[{dims}")
        elif "(*)" in type_text:  # function pointer: name goes inside the parens
            params.append(type_text.replace("(*)", f"(*{name})", 1))
        else:
            params.append(f"{type_text} {name}")
    if not params:
        params = ["void"] if not variadic else []
    if variadic and params != ["void"]:
        params.append("...")
    return f"{cursor.result_type.spelling} {cursor.spelling}({', '.join(params)})"


def _var_decl_text(cursor, cache: dict[str, bytes]) -> tuple[str, str | None]:
    """Rebuild a file-scope variable declaration. Returns (text, normalization_note).

    Copying the source extent does not work here, for three reasons that all show up in
    CHStone: a multi-declarator statement (``int i, j;``) gives each declarator an extent
    covering only its own name, so ``j`` would be emitted as ``static j;``; C's ``register``
    storage class conflicts with the ``static`` we add for internal linkage; and an
    anonymous aggregate type cannot be re-spelled at all. Reconstructing from the parse
    tree sidesteps all three.
    """

    type_text = cursor.type.spelling
    name = cursor.spelling
    note: str | None = None

    if "(unnamed" in type_text or "(anonymous" in type_text:
        # An anonymous aggregate has no spellable type name, and the variable's own extent
        # is often just its identifier (`union {...} s, t;` gives `t` an extent of "t").
        # Give the aggregate a synthetic tag so the variable can be declared normally.
        declaration = cursor.type.get_declaration()
        aggregate = _extent_text(declaration, cache).strip().rstrip(";").strip()
        tag = f"_c2hlsc_anon_{abs(hash(declaration.get_usr() or aggregate)) % 100000}"
        keyword = re.match(r"^(struct|union|enum)\b", aggregate)
        if keyword and aggregate:
            named = re.sub(r"^(struct|union|enum)\b", rf"\1 {tag}", aggregate, count=1)
            return (
                f"{named};\nstatic {keyword.group(1)} {tag} {name};",
                f"named anonymous {keyword.group(1)} backing global {name!r} as {tag!r}",
            )
        text = re.sub(r"\bregister\b\s*", "", _extent_text(cursor, cache).strip())
        if not text.endswith(";"):
            text += ";"
        if not re.match(r"^\s*(static|extern)\b", text):
            text = "static " + text
        return text, f"gave anonymous-aggregate global {name!r} internal linkage"

    # `word [160]` -> `word name[160]`; `int (*)(int)` -> `int (*name)(int)`
    array = re.match(r"^(.*?)\s*((?:\[[^\]]*\])+)$", type_text)
    if array:
        decl = f"{array.group(1)} {name}{array.group(2)}"
    elif "(*)" in type_text:
        decl = type_text.replace("(*)", f"(*{name})", 1)
    else:
        decl = f"{type_text} {name}"

    # Take the initializer from the source text, not from the child cursors: for
    # `word so[N];` the array-size expression `N` is also a child, and treating it as an
    # initializer emits `word so[160] = N;`.
    raw = _extent_text(cursor, cache)
    equals = raw.find("=")
    if equals >= 0:
        initializer = raw[equals + 1 :].strip().rstrip(";").strip()
        if initializer:
            decl += f" = {initializer}"

    if "extern" not in type_text:
        decl = "static " + decl
        note = f"gave file-scope global {name!r} internal linkage"
    return decl + ";", note


def _is_kr_definition(cursor, cache: dict[str, bytes]) -> bool:
    """True when the definition uses a K&R parameter list."""

    text = _extent_text(cursor, cache)
    head, _, _ = text.partition("{")
    if "(" not in head:
        return False
    after_parens = head[head.rfind(")") + 1 :]
    return ";" in after_parens


def _function_text(cursor, cache: dict[str, bytes]) -> tuple[str, bool]:
    """Emit a function definition in ANSI form. Returns (text, was_rewritten).

    The head is *always* regenerated from the parse tree rather than copied. Detecting
    K&R textually is unreliable -- ``motion``'s ``decode_motion_vector`` has a parameter
    list spanning lines and slipped past a "is there a semicolon after the last paren"
    heuristic -- whereas libclang has already resolved every parameter, so printing the
    ANSI form unconditionally is both simpler and correct for all of them.
    """

    text = _extent_text(cursor, cache)
    if not text:
        return "", False
    brace = text.find("{")
    if brace < 0:  # a bare prototype
        return f"static {_ansi_signature(cursor)};", True
    body = text[brace:]
    return f"static {_ansi_signature(cursor)} {body}", True


class _ClosureBuilder:
    def __init__(self, cindex, tu, top_name: str):
        self.cindex = cindex
        self.K = cindex.CursorKind
        self.tu = tu
        self.top_name = top_name
        self.cache: dict[str, bytes] = {}
        self.file_scope: dict[str, object] = {}
        self.macros: dict[str, object] = {}
        self.macro_uses: list[object] = []
        self.seen_usr: set[str] = set()
        self.ordered: list[object] = []
        self.used_macros: list[str] = []
        self.normalizations: list[str] = []
        self._index_translation_unit()

    def _index_translation_unit(self) -> None:
        for cursor in self.tu.cursor.walk_preorder():
            kind = _kind_name(cursor)
            if kind == "MACRO_DEFINITION":
                if cursor.location.file and not _is_system(cursor.location.file.name):
                    self.macros.setdefault(cursor.spelling, cursor)
            elif kind == "MACRO_INSTANTIATION":
                self.macro_uses.append(cursor)
            elif kind in _HOISTABLE and cursor.spelling:
                # An anonymous aggregate cannot be re-spelled on its own; whichever
                # typedef or variable declaration encloses it carries it instead.
                if kind in _TYPE_KINDS and (
                    "unnamed" in cursor.spelling or "anonymous" in cursor.spelling
                ):
                    continue
                if cursor.location.file and not _is_system(cursor.location.file.name):
                    existing = self.file_scope.get(cursor.spelling)
                    # prefer the definition over a forward declaration
                    if existing is None or (cursor.is_definition() and not existing.is_definition()):
                        self.file_scope[cursor.spelling] = cursor

    def find_top(self):
        for cursor in self.tu.cursor.walk_preorder():
            if (
                _kind_name(cursor) in _FUNCTION_KINDS
                and cursor.spelling == self.top_name
                and cursor.is_definition()
            ):
                return cursor
        return None

    def _macros_in_extent(self, cursor) -> list[str]:
        extent = cursor.extent
        if not extent.start.file:
            return []
        name = extent.start.file.name
        lo, hi = extent.start.offset, extent.end.offset
        hits: list[str] = []
        for use in self.macro_uses:
            loc = use.location
            if loc.file and loc.file.name == name and lo <= loc.offset <= hi:
                if use.spelling in self.macros:
                    hits.append(use.spelling)
        return hits

    def _add_macro(self, name: str) -> None:
        if name in self.used_macros:
            return
        cursor = self.macros.get(name)
        if cursor is None:
            return
        self.used_macros.append(name)
        # a macro body can name other macros; recurse over its replacement text
        text = _extent_text(cursor, self.cache)
        for token in re.findall(r"[A-Za-z_]\w*", text)[1:]:
            if token in self.macros:
                self._add_macro(token)

    def visit(self, cursor) -> None:
        """Post-order DFS: a symbol's dependencies are emitted before the symbol."""

        for name in self._macros_in_extent(cursor):
            self._add_macro(name)
        for node in cursor.walk_preorder():
            referenced = node.referenced
            if referenced is None or not referenced.spelling:
                continue
            target = self.file_scope.get(referenced.spelling)
            if target is None or target.spelling == self.top_name:
                continue
            usr = target.get_usr() or target.spelling
            if usr in self.seen_usr:
                continue
            self.seen_usr.add(usr)
            self.visit(target)
            self.ordered.append(target)
        # the declared type of a variable/typedef is a dependency too, and does not always
        # surface as a TYPE_REF child (e.g. `word so[N]` where `word` is a typedef)
        for token in re.findall(r"[A-Za-z_]\w*", cursor.type.spelling if cursor.kind else ""):
            target = self.file_scope.get(token)
            if target is None or target.spelling == self.top_name:
                continue
            usr = target.get_usr() or target.spelling
            if usr in self.seen_usr:
                continue
            self.seen_usr.add(usr)
            self.visit(target)
            self.ordered.append(target)

    def seed_all(self) -> None:
        """Hoist every non-system file-scope declaration, in dependency order.

        A *minimal* closure -- only what the top demonstrably references -- is what you
        want for synthesis, but it cannot be computed reliably in the presence of
        token-pasting macros. CHStone's ``sha`` expands ``FUNC(n,i)`` into ``f##n(B,C,D)``,
        so ``f1`` exists only after preprocessing and appears in no reference edge; the
        minimal closure drops it and the TU fails to compile. Hoisting the whole file
        scope is complete by construction, and the per-symbol reference walk is still run
        first so the emission order stays dependency-correct.
        """

        for cursor in sorted(
            self.file_scope.values(),
            key=lambda c: (c.location.file.name if c.location.file else "", c.location.offset),
        ):
            usr = cursor.get_usr() or cursor.spelling
            if usr in self.seen_usr or cursor.spelling == self.top_name:
                continue
            self.seen_usr.add(usr)
            self.visit(cursor)
            self.ordered.append(cursor)

    def emit(self, source_path: Path, complete: bool = True) -> ClosureResult:
        top = self.find_top()
        if top is None:
            return ClosureResult(
                available=True,
                diagnostics=[f"top function {self.top_name!r} has no definition in {source_path.name}"],
            )
        self.visit(top)
        referenced_directly = len(self.ordered)
        if complete:
            self.seed_all()
            # macros are textual and cheap; take all of them so token-pasted names resolve
            for name in sorted(
                self.macros,
                key=lambda n: (
                    self.macros[n].location.file.name if self.macros[n].location.file else "",
                    self.macros[n].location.offset,
                ),
            ):
                self._add_macro(name)

        includes = _original_includes(source_path)
        macro_lines: list[str] = []
        for name in self.used_macros:
            text = _extent_text(self.macros[name], self.cache).rstrip()
            if not text.startswith("#"):
                text = "#define " + text
            macro_lines.append(f"#ifndef {name}\n{text}\n#endif")

        type_lines: list[str] = []
        value_lines: list[str] = []
        prototypes: list[str] = []
        bodies: list[str] = []
        for cursor in self.ordered:
            kind = _kind_name(cursor)
            text = _extent_text(cursor, self.cache).strip()
            if not text:
                continue
            if kind in _TYPE_KINDS:
                type_lines.append(text if text.endswith(";") else text + ";")
            elif kind in _VALUE_KINDS:
                # file-scope storage gets internal linkage so the hoisted copy cannot
                # collide with the golden reference's own definition at link time
                decl, note = _var_decl_text(cursor, self.cache)
                if note:
                    self.normalizations.append(note)
                value_lines.append(decl)
            elif kind in _FUNCTION_KINDS:
                body, rewritten = _function_text(cursor, self.cache)
                if not body:
                    continue
                if rewritten and _is_kr_definition(cursor, self.cache):
                    self.normalizations.append(
                        f"rewrote K&R definition of {cursor.spelling!r} into ANSI form"
                    )
                elif rewritten:
                    self.normalizations.append(
                        f"gave helper {cursor.spelling!r} internal linkage"
                    )
                prototypes.append(f"static {_ansi_signature(cursor)};")
                bodies.append(body)

        def block(title: str, lines: list[str]) -> str:
            if not lines:
                return ""
            return f"\n// --- {title} ---\n" + "\n".join(lines) + "\n"

        header_note = (
            "// c2hlsc_agent closure: file-scope context the top function references,\n"
            "// hoisted from the original source and normalized to valid C++ so this\n"
            "// translation unit is self-contained.\n"
        )
        preamble = (
            header_note
            + block("includes", includes)
            + block("macros", macro_lines)
            + block("types", type_lines)
            + block("globals", value_lines)
            + block("prototypes", prototypes)
            + block("definitions", bodies)
        )
        type_preamble = (
            header_note
            + block("includes", includes)
            + block("macros", macro_lines)
            + block("types", type_lines)
        )
        # What a source file that already includes the header still needs. Types are
        # deliberately absent: they came in via the header, and repeating them here would
        # be a redefinition rather than a redundancy.
        definition_preamble = (
            block("globals", value_lines)
            + block("prototypes", prototypes)
            + block("definitions", bodies)
        )
        return ClosureResult(
            preamble=preamble,
            type_preamble=type_preamble,
            definition_preamble=definition_preamble,
            symbols=[c.spelling for c in self.ordered],
            macros=list(self.used_macros),
            includes=includes,
            normalizations=self.normalizations,
            available=True,
            diagnostics=[
                f"top references {referenced_directly} file-scope symbols directly; "
                f"hoisted {len(self.ordered)} for completeness"
            ],
        )


def extract_closure(
    source_path: Path | str,
    top_name: str,
    *,
    include_dirs: tuple[str, ...] = (),
    defines: tuple[str, ...] = (),
    extra_args: tuple[str, ...] = (),
    language: str = "c",
) -> ClosureResult:
    """Compute the transitive file-scope closure of ``top_name`` in ``source_path``.

    Returns a :class:`ClosureResult` whose ``preamble`` is C++-valid text providing every
    macro, type, global and helper the top references. When libclang is unavailable the
    result has ``available=False`` and an empty preamble; callers must keep their previous
    behaviour rather than treating that as an empty closure.
    """

    source_path = Path(source_path)
    try:
        cindex = _load_clang()
    except LibclangUnavailable as exc:
        return ClosureResult(available=False, diagnostics=[str(exc)])

    args: list[str] = [f"-x{language}"]
    args += [f"-I{d}" for d in include_dirs]
    args += [f"-I{source_path.parent}"]
    args += [f"-D{d}" for d in defines]
    for builtin in _builtin_include_dirs():
        args += ["-isystem", builtin]
    args += list(extra_args)

    options = cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    try:
        tu = cindex.Index.create().parse(str(source_path), args=args, options=options)
    except Exception as exc:  # noqa: BLE001 - a parse failure must not fail conversion
        return ClosureResult(available=False, diagnostics=[f"libclang parse failed: {exc}"])

    hard_errors = [d.spelling for d in tu.diagnostics if d.severity >= 3]
    builder = _ClosureBuilder(cindex, tu, top_name)
    result = builder.emit(source_path)
    if hard_errors:
        result.diagnostics.extend(f"libclang: {message}" for message in hard_errors[:5])
    return result
