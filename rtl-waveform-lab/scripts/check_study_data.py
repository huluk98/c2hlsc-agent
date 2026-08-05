#!/usr/bin/env python3
"""Cross-check generated trace data and syntax-critical study assets."""

from __future__ import annotations

import csv
from html.parser import HTMLParser
import json
import pathlib
import re
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]

EXPECTED_ROWS = [
    ("E0", "5", "1", "0", "0", "0", "0", "0", "0", "0", "0"),
    ("E1", "15", "1", "0", "0", "0", "0", "0", "0", "0", "0"),
    ("E2", "25", "0", "1", "3", "5", "1", "1", "8", "0", "0"),
    ("E3", "35", "0", "0", "42", "99", "0", "0", "8", "1", "8"),
    ("E4", "45", "0", "1", "200", "100", "1", "1", "300", "0", "8"),
    ("E5", "55", "0", "1", "255", "255", "1", "1", "510", "1", "300"),
    ("E6", "65", "0", "0", "17", "34", "0", "0", "510", "1", "510"),
    ("E7", "75", "0", "0", "17", "34", "0", "0", "510", "0", "510"),
    ("E8", "85", "0", "1", "1", "2", "1", "1", "3", "0", "510"),
    ("E9", "95", "1", "0", "0", "0", "0", "0", "0", "0", "0"),
    ("E10", "105", "0", "0", "0", "0", "0", "0", "0", "0", "0"),
]


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def find_wave_signal(items: list[object], name: str) -> dict[str, object]:
    for item in items:
        if isinstance(item, dict) and item.get("name") == name:
            return item
        if isinstance(item, list):
            found = find_wave_signal(item[1:], name)
            if found:
                return found
    return {}


class StudyTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row = []
        elif tag == "td":
            self.in_cell = True
            self.cell_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self.row.append(" ".join("".join(self.cell_parts).split()))
            self.in_cell = False
        elif tag == "tr" and self.row:
            self.rows.append(self.row)


with (ROOT / "docs/expected_waveform.json").open(encoding="utf-8") as handle:
    wave = json.load(handle)
if not isinstance(wave.get("signal"), list):
    fail("expected_waveform.json has no signal list")
if find_wave_signal(wave["signal"], "Rising edge").get("data") != [f"E{i}" for i in range(11)]:
    fail("WaveJSON rising-edge labels drifted")
if find_wave_signal(wave["signal"], "sum (unsigned)").get("data") != ["0", "8", "300", "510", "0"]:
    fail("WaveJSON sum values drifted")

wave_html = (ROOT / "docs/expected_waveform.html").read_text(encoding="utf-8")
wave_match = re.search(
    r'<script type="WaveDrom">\s*(\{.*?\})\s*</script>',
    wave_html,
    flags=re.DOTALL,
)
if wave_match is None or json.loads(wave_match.group(1)) != wave:
    fail("expected_waveform.html embedded data differs from expected_waveform.json")

with (ROOT / "build/cycle_trace.csv").open(newline="", encoding="utf-8") as handle:
    reader = csv.reader(handle)
    header = next(reader, None)
    rows = [tuple(row) for row in reader]

expected_header = [
    "edge", "time_ns", "rst", "in_valid", "a", "b", "input_accepted",
    "pending_valid", "pending_sum", "out_valid", "sum",
]
if header != expected_header:
    fail(f"unexpected trace header: {header!r}")
if rows != EXPECTED_ROWS:
    fail("build/cycle_trace.csv drifted from the reviewed E0-E10 trace")

html = (ROOT / "docs/study.html").read_text(encoding="utf-8")
if len(re.findall(r"\{ q: ", html)) != 10:
    fail("study.html must contain exactly ten quiz records")
for required in ("generation_contract.md", "ordered_exercises.md", "fundamentals_field_guide.md", "pdf_reading_map.md"):
    if required not in html:
        fail(f"study.html does not link {required}")
    if not (ROOT / "docs" / required).is_file():
        fail(f"study.html target does not exist: {required}")
if not (ROOT.parent / "papers/2507.04315v3.pdf").is_file():
    fail("the mapped HLStrans PDF is missing from the parent papers directory")
for marker in (
    'id="library"',
    'id="choose-pdf"',
    'id="pdf-input"',
    'id="pdf-list"',
    'id="pdf-viewer"',
    'src="pdf_shelf.js"',
    "frame-src 'self' blob:",
):
    if marker not in html:
        fail(f"study.html is missing PDF shelf marker: {marker}")
for edge in range(11):
    if f">E{edge}<" not in html:
        fail(f"study.html table is missing E{edge}")

table_parser = StudyTableParser()
table_parser.feed(html)
expected_study_rows = [
    ["E0", "5 ns", "1", "0", "—", "0", "Reset clears state"],
    ["E1", "15 ns", "1", "0", "—", "0", "Reset remains active"],
    ["E2", "25 ns", "0", "1", "T0: 3 + 5 accepted", "0", "No output yet"],
    ["E3", "35 ns", "0", "0", "Bubble", "1", "T0 result: 8"],
    ["E4", "45 ns", "0", "1", "T1: 200 + 100 accepted", "0", "Bubble; held 8 ignored"],
    ["E5", "55 ns", "0", "1", "T2: 255 + 255 accepted", "1", "T1 result: 300"],
    ["E6", "65 ns", "0", "0", "Bubble", "1", "T2 result: 510"],
    ["E7", "75 ns", "0", "0", "Bubble", "0", "Held 510 ignored"],
    ["E8", "85 ns", "0", "1", "Probe: 1 + 2 accepted", "0", "No output"],
    ["E9", "95 ns", "1", "0", "—", "0", "Reset flushes probe"],
    ["E10", "105 ns", "0", "0", "—", "0", "No stale result appears"],
]
if table_parser.rows != expected_study_rows:
    fail("study.html E0-E10 table drifted from the reviewed trace")

cycle_rows = []
for line in (ROOT / "docs/cycle_table.md").read_text(encoding="utf-8").splitlines():
    if re.match(r"^\| E\d+ \|", line):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        cycle_rows.append((cells[0], cells[1], cells[2], cells[3], cells[4], cells[5], cells[7], cells[8], cells[9]))
expected_cycle_rows = [
    (row[0], f"{row[1]} ns", row[2], row[3], row[4], row[5], row[7], row[9], row[10])
    for row in EXPECTED_ROWS
]
if cycle_rows != expected_cycle_rows:
    fail("cycle_table.md values drifted from the generated trace")

scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)
if len(scripts) != 1:
    fail("study.html must contain exactly one inline script for syntax checking")
with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8") as handle:
    handle.write(scripts[0])
    handle.flush()
    result = subprocess.run(
        ["node", "--check", handle.name],
        check=False,
        capture_output=True,
        text=True,
    )
if result.returncode:
    sys.stderr.write(result.stderr)
    fail("study.html inline JavaScript failed node --check")

pdf_shelf_script = ROOT / "docs/pdf_shelf.js"
pdf_shelf_source = pdf_shelf_script.read_text(encoding="utf-8")
for element_id in re.findall(r'document\.querySelector\("#([^\"]+)"\)', pdf_shelf_source):
    if f'id="{element_id}"' not in html:
        fail(f"pdf_shelf.js expects missing study.html element id: {element_id}")
result = subprocess.run(
    ["node", "--check", str(pdf_shelf_script)],
    check=False,
    capture_output=True,
    text=True,
)
if result.returncode:
    sys.stderr.write(result.stderr)
    fail("pdf_shelf.js failed node --check")

print("PASS: WaveJSON, E0-E10 trace, study links, PDF shelf structure, quiz count, and JavaScript syntax agree.")
