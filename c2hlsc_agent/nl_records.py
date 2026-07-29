"""HLS_NL record utilities and the C/C++ signature parser.

Lifted from ``scripts/generate_hls_nl_testbenches.py`` so package modules (the
cross-reference differential oracle) can import it normally — ``scripts/`` is not
distributed (pyproject packages only ``c2hlsc_agent``), and importlib path tricks
belong in tests, not shipped code. The script now imports these names from here;
behavior is unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


TYPE_PREFIX_RE = (
    r"(?:void|bool|int|unsigned\s+int|unsigned|long|short|char|float|double|"
    r"ap_u?int\s*<[^>]+>|ap_u?fixed\s*<[^>]+>|hls::stream\s*<[^>]+>|"
    r"u?int\d+_t)"
)


@dataclass
class Arg:
    raw: str
    c_type: str
    name: str

    @property
    def base_type(self) -> str:
        text = self.c_type
        text = re.sub(r"\bconst\b", "", text)
        text = text.replace("&", "").replace("*", "")
        return re.sub(r"\s+", " ", text).strip()

    @property
    def is_reference_or_pointer(self) -> bool:
        return "&" in self.c_type or "*" in self.c_type

    @property
    def is_const(self) -> bool:
        return bool(re.search(r"\bconst\b", self.c_type))

    @property
    def is_stream(self) -> bool:
        return "hls::stream" in self.c_type

    @property
    def direction(self) -> str:
        if self.is_stream:
            lower = self.name.lower()
            if lower.startswith(("in", "input")) or lower.endswith(("_in", "input")):
                return "input"
            if lower.startswith(("out", "output")) or lower.endswith(("_out", "output")):
                return "output"
            return "inout"
        if self.is_reference_or_pointer and not self.is_const:
            return "output"
        return "input"


@dataclass
class FunctionSig:
    return_type: str
    name: str
    args: list[Arg]
    signature: str


def split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    angle = paren = bracket = 0
    for idx, ch in enumerate(text):
        if ch == "<":
            angle += 1
        elif ch == ">" and angle:
            angle -= 1
        elif ch == "(":
            paren += 1
        elif ch == ")" and paren:
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]" and bracket:
            bracket -= 1
        elif ch == "," and angle == paren == bracket == 0:
            parts.append(text[start:idx].strip())
            start = idx + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def find_matching(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def parse_arg(raw: str) -> Arg | None:
    raw = raw.strip()
    if not raw or raw == "void":
        return None
    raw = raw.split("=", 1)[0].strip()
    raw = re.sub(r"\s+", " ", raw)
    match = re.match(r"(?P<prefix>.+?)(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\])*$", raw)
    if not match:
        return None
    c_type = match.group("prefix").strip()
    name = match.group("name").strip()
    return Arg(raw=raw, c_type=c_type, name=name)


def _iter_functions(code: str) -> Iterator[FunctionSig]:
    """Yield every plausible function DEFINITION (body-bearing) in source order."""

    pattern = re.compile(
        rf"(?P<ret>{TYPE_PREFIX_RE}(?:\s*[*&]|\s+\w[\w:<>,\s*&]*)?)\s+"
        rf"(?P<name>[A-Za-z_]\w*)\s*\(",
        re.S,
    )
    for match in pattern.finditer(code):
        open_idx = code.find("(", match.end() - 1)
        close_idx = find_matching(code, open_idx, "(", ")")
        if close_idx < 0:
            continue
        after = code[close_idx + 1 :].lstrip()
        if not after.startswith("{"):
            continue
        args_text = code[open_idx + 1 : close_idx]
        args = [arg for part in split_top_level_commas(args_text) if (arg := parse_arg(part))]
        ret = re.sub(r"\s+", " ", match.group("ret")).strip()
        name = match.group("name")
        signature = f"{ret} {name}({', '.join(arg.raw for arg in args)})"
        yield FunctionSig(ret, name, args, signature)


def extract_function(code: str) -> FunctionSig | None:
    """First function definition in the file (historic behavior of the batch scripts)."""

    return next(_iter_functions(code), None)


def extract_named_function(code: str, name: str) -> FunctionSig | None:
    """Definition of the NAMED function, scanning past helpers.

    ``extract_function`` returns the first definition, which in generated HLS code is
    often a static helper rather than the top — named lookup avoids mislabeling such
    records as signature mismatches.
    """

    for sig in _iter_functions(code):
        if sig.name == name:
            return sig
    return None


def extract_design_title(prompt: str) -> str | None:
    match = re.search(r"\*\*Design Task:\*\*\s*([^\n]+)", prompt)
    if match:
        return match.group(1).strip()
    match = re.search(r"Design Task:\s*([^\n]+)", prompt)
    return match.group(1).strip() if match else None


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load either HLS_NL.json or repaired HLS_NL JSONL records."""

    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if path.suffix.lower() == ".jsonl" or (stripped and not stripped.startswith("[")):
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise SystemExit(f"JSONL line {line_no} must be an object")
            records.append(row)
        return records

    data = json.loads(text)
    if not isinstance(data, list):
        raise SystemExit("HLS_NL JSON root must be a list")
    if not all(isinstance(record, dict) for record in data):
        raise SystemExit("HLS_NL records must be objects")
    return data


def record_source_file(record: dict[str, Any]) -> Any:
    return record.get("file") or record.get("original_file") or record.get("source")


def record_design_title(record: dict[str, Any]) -> str | None:
    return record.get("design_title") or extract_design_title(str(record.get("HLS_instruction", "")))


def record_id_for(record: dict[str, Any], fallback: int) -> int:
    try:
        return int(record.get("record_id", fallback))
    except (TypeError, ValueError):
        return fallback


def identifier(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_").lower()
    return text or "record"


def macro_lines(code: str) -> list[str]:
    lines: list[str] = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("#define "):
            lines.append(stripped)
    return lines


def cpp_string(text: str) -> str:
    return json.dumps(text)


def is_integer_type(c_type: str) -> bool:
    base = re.sub(r"\s+", " ", c_type)
    return bool(
        "ap_uint" in base
        or "ap_int" in base
        or re.search(r"\b(u?int\d+_t|int|unsigned|long|short|char|bool)\b", base)
    )
