#!/usr/bin/env python3
"""Cross-check generated trace data and syntax-critical study assets."""

from __future__ import annotations

import ast
import csv
from html.parser import HTMLParser
import json
import pathlib
import re
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]

PYTHON_ASSETS = (
    ROOT / "scripts/check_study_data.py",
    ROOT / "scripts/pdf_study_analyzer.py",
    ROOT / "scripts/study_server.py",
    ROOT / "scripts/test_pdf_study_analyzer.py",
    ROOT / "scripts/test_study_server.py",
)

TEXT_ASSETS = (
    ROOT / "README.md",
    ROOT / "Makefile",
    ROOT / "docs/study.html",
    ROOT / "docs/reading_coach.js",
    ROOT / "docs/pdf_shelf.js",
    ROOT / "docs/pdf_reading_map.md",
    ROOT / "docs/ordered_exercises.md",
    ROOT / "docs/fundamentals_field_guide.md",
    ROOT / "docs/generation_contract.md",
    ROOT / "scripts/check_environment.sh",
    *PYTHON_ASSETS,
)

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


for text_path in TEXT_ASSETS:
    raw_text = text_path.read_bytes()
    if raw_text.startswith(b"\xef\xbb\xbf"):
        fail(f"text asset must not contain a UTF-8 BOM: {text_path.relative_to(ROOT)}")
    try:
        decoded_text = raw_text.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        fail(f"text asset is not strict UTF-8: {text_path.relative_to(ROOT)}: {error}")
    for codepoint in (0x00C2, 0x00C3, 0x00E2, 0xFFFD):
        if chr(codepoint) in decoded_text:
            fail(
                "text asset contains a common mojibake marker "
                f"U+{codepoint:04X}: {text_path.relative_to(ROOT)}"
            )

for python_path in PYTHON_ASSETS:
    try:
        ast.parse(python_path.read_text(encoding="utf-8"), filename=str(python_path))
    except SyntaxError as error:
        fail(f"Python syntax error in {python_path.relative_to(ROOT)}: {error}")


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
    if f'href="{required}"' not in html:
        fail(f"study.html does not link {required}")
    if not (ROOT / "docs" / required).is_file():
        fail(f"study.html target does not exist: {required}")

reading_map_path = ROOT / "docs/pdf_reading_map.md"
reading_map = reading_map_path.read_text(encoding="utf-8")
try:
    reading_map.encode("ascii")
except UnicodeEncodeError as error:
    fail(f"pdf_reading_map.md must remain ASCII-safe to prevent mojibake: {error}")

ordered_reading_sections = (
    "## How to use this map",
    "## Source and page-number rules",
    "## Opt-in local PDF detection",
    "## Route at a glance",
    "## Day 1 - Preflight and Stages 1-4",
    "## Day 2 - Stages 5-8",
    "## Day 3 - Stages 9-12",
    "### Stage 11A - HLStrans paper extraction",
    "### Stage 11B - actual HLS execution",
    "### Stage 12 - vendor documentation and capstone",
    "## Definition of done",
)
last_position = -1
for section in ordered_reading_sections:
    position = reading_map.find(section)
    if position <= last_position:
        fail(f"pdf_reading_map.md is missing or misorders section: {section}")
    last_position = position
for stage in range(1, 13):
    if re.search(rf"\bStage {stage}(?!\d)", reading_map) is None:
        fail(f"pdf_reading_map.md does not cover Stage {stage}")
if "../../papers/2507.04315v3.pdf" not in reading_map:
    fail("pdf_reading_map.md does not identify the supplied HLStrans PDF")
if not (ROOT.parent / "papers/2507.04315v3.pdf").is_file():
    fail("the mapped HLStrans PDF is missing from the parent papers directory")
for required_analysis_statement in (
    "nothing is transmitted automatically",
    "exactly two distinct, qualified page candidates",
    "text-only keyword/concept heuristic",
    "unconfirmed locators",
    "no starter runs automatically",
):
    if required_analysis_statement not in reading_map:
        fail(f"pdf_reading_map.md is missing local-analysis contract: {required_analysis_statement}")
for marker in (
    'id="library"',
    'id="reading-route"',
    'href="#reading-route"',
    'id="choose-pdf"',
    'id="pdf-input"',
    'id="pdf-list"',
    'id="pdf-viewer"',
    'id="coach-readings"',
    'id="coach-runs"',
    'id="pdf-analysis-consent"',
    'id="analyze-pdf-locally"',
    'id="apply-analysis-locators"',
    'id="pdf-analysis-results"',
    'id="pdf-code-starters"',
    'src="reading_coach.js"',
    'src="pdf_shelf.js"',
    "connect-src 'self'",
    "frame-src 'self' blob:",
):
    if marker not in html:
        fail(f"study.html is missing PDF shelf marker: {marker}")
if html.find('src="reading_coach.js"') > html.find('src="pdf_shelf.js"'):
    fail("study.html must load reading_coach.js before pdf_shelf.js")
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

reading_coach_script = ROOT / "docs/reading_coach.js"
pdf_shelf_script = ROOT / "docs/pdf_shelf.js"
pdf_analyzer_script = ROOT / "scripts/pdf_study_analyzer.py"
study_server_script = ROOT / "scripts/study_server.py"
reading_coach_source = reading_coach_script.read_text(encoding="utf-8")
pdf_shelf_source = pdf_shelf_script.read_text(encoding="utf-8")
pdf_analyzer_source = pdf_analyzer_script.read_text(encoding="utf-8")
study_server_source = study_server_script.read_text(encoding="utf-8")
makefile_source = (ROOT / "Makefile").read_text(encoding="utf-8")
for local_resource in re.findall(r'url:\s*"([a-z0-9_]+\.md)"', reading_coach_source):
    if not (ROOT / "docs" / local_resource).is_file():
        fail(f"reading_coach.js links missing local resource: {local_resource}")
for element_id in re.findall(r'document\.querySelector\("#([^\"]+)"\)', pdf_shelf_source):
    if f'id="{element_id}"' not in html:
        fail(f"pdf_shelf.js expects missing study.html element id: {element_id}")
for script_path in (reading_coach_script, pdf_shelf_script):
    result = subprocess.run(
        ["node", "--check", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        sys.stderr.write(result.stderr)
        fail(f"{script_path.name} failed node --check")

for make_marker in (
    "pdf-analyzer-test:",
    "./scripts/test_pdf_study_analyzer.py",
    "./scripts/test_study_server.py",
    "$(PYTHON) ./scripts/study_server.py --port 4173 --directory .",
):
    if make_marker not in makefile_source:
        fail(f"Makefile is missing local PDF-analysis integration: {make_marker}")
if re.search(r"^verify:.*\bpdf-analyzer-test\b", makefile_source, flags=re.MULTILINE) is None:
    fail("Makefile verify target must include pdf-analyzer-test")
if "python3 -m http.server" in makefile_source or "$(PYTHON) -m http.server" in makefile_source:
    fail("Makefile serve must use study_server.py, not the static http.server")

for server_marker in (
    '"/api/v1/session"',
    'prefix = "/api/v1/pdf-candidates/"',
    'StudyServer(("127.0.0.1", args.port)',
    "TemporaryDirectory(prefix=\"rtl-study-\")",
    '"X-Study-Token"',
    '"INSUFFICIENT_CANDIDATES"',
    'result.pop("fileName", None)',
    '"textOnly": True',
    "if not self._trusted_host():",
):
    if server_marker not in study_server_source:
        fail(f"study_server.py is missing loopback PDF-analysis contract: {server_marker}")

for analyzer_marker in (
    "MAX_PAGES = 650",
    "MAX_EXTRACT_BYTES = 8 * 1024 * 1024",
    "STARTERS: dict[str, dict[str, str]] =",
    '"Page matches are keyword-ranked local suggestions, not semantic proof."',
    '"Review each candidate in the PDF before confirming its locator or using a starter."',
    "start_new_session=True",
    '"requiredGroups"',
    "page_two_evidence >= 4",
    "selectors.DefaultSelector()",
    "total > max_output_bytes",
):
    if analyzer_marker not in pdf_analyzer_source:
        fail(f"pdf_study_analyzer.py is missing bounded deterministic behavior: {analyzer_marker}")

coach_contract_check = r"""
const coach = require(process.argv[1]);
const expectedRuns = {
  day1: [
    "https://ocw.mit.edu/courses/6-004-computation-structures-spring-2017/pages/c2/c2s2/c2s2v4/",
    "https://ocw.mit.edu/courses/6-004-computation-structures-spring-2017/pages/c4/c4s2/"
  ],
  day2: [
    "https://www.youtube.com/watch?v=3LQUrpSADx8",
    "https://ocw.mit.edu/courses/6-004-computation-structures-spring-2017/pages/c6/c6s2/"
  ],
  day3: [
    "https://www.youtube.com/watch?v=xU9WsqcICR0",
    "https://docs.amd.com/r/en-US/Vitis-Tutorials-Getting-Started/Vitis-HLS"
  ]
};
const sources = {
  textbook: "Digital Design and Computer Architecture.pdf",
  hlstrans: "2507.04315v3.pdf",
  unmapped: "my-private-notes.pdf"
};
const required = ["title", "detail", "stage", "artifact", "gate", "source", "kind", "availability"];
function assert(condition, message) {
  if (!condition) throw new Error(message);
}
function synchronizedProfile(sourceType, block, pdfId = `pdf-${sourceType}-${block}`) {
  const profile = coach.createEmptyProfile();
  profile.pdfId = pdfId;
  profile.sourceOverride = sourceType;
  profile.identity.confirmed = true;
  profile.identity.pageConvention = "PDF viewer pages; cover is page 1";
  if (sourceType === "hlstrans") {
    profile.identity.titleAndAuthor = "HLStrans - Qingyun Zou et al.";
    profile.identity.version = "arXiv:2507.04315v3, 4 December 2025";
  } else if (sourceType === "textbook") {
    profile.identity.titleAndAuthor = "Digital Design and Computer Architecture - confirmed authors";
    profile.identity.version = "learner-confirmed owned edition";
  } else {
    profile.identity.titleAndAuthor = "My uploaded RTL reference - Example Author";
    profile.identity.version = "learner-confirmed 2026 edition";
  }
  profile.blocks[block].locators = [
    { value: "First confirmed chapter/section", viewerPage: 7, confirmed: true },
    { value: "Second confirmed chapter/section", viewerPage: 19, confirmed: true }
  ];
  return profile;
}
for (const block of Object.keys(coach.BLOCK_LABELS)) {
  for (const [sourceType, fileName] of Object.entries(sources)) {
    const assignment = coach.getReadingAssignment(fileName, block);
    assert(assignment.sourceType === sourceType, `${block}/${sourceType}: classification drifted`);
    assert(!assignment.sync.ready, `${block}/${sourceType}: filename suggestion must not produce ready sync`);
    assert(assignment.readings.length === 2, `${block}/${sourceType}: expected exactly two readings`);
    assert(assignment.runs.length === 2, `${block}/${sourceType}: expected exactly two runs`);
    for (const [groupName, items] of [["reading", assignment.readings], ["run", assignment.runs]]) {
      for (const item of items) {
        for (const field of required) {
          assert(typeof item[field] === "string" && item[field].trim(), `${block}/${sourceType}/${groupName}: missing ${field}`);
        }
        assert(item.url === null || /^(https:\/\/|[a-z0-9_]+\.md$)/.test(item.url), `${block}/${sourceType}: unsafe resource URL`);
      }
    }
    assert(JSON.stringify(assignment.runs.map((item) => item.url)) === JSON.stringify(expectedRuns[block]), `${block}: curated run URLs drifted`);
    const prompt = coach.buildCodexPrompt(fileName, block);
    assert(prompt.includes(fileName), `${block}/${sourceType}: handoff omits filename`);
    assert(/private PDF shelf/.test(prompt) && /PDF attached in this conversation/.test(prompt), `${block}/${sourceType}: privacy handoff drifted`);
  }
  for (const [sourceType, fileName] of Object.entries(sources)) {
    const profile = synchronizedProfile(sourceType, block);
    const assignment = coach.getReadingAssignment(fileName, block, profile);
    assert(assignment.sync.ready, `${block}/${sourceType}: confirmed identity and locators should be ready`);
    assert(assignment.readings.every((item) => item.locatorConfirmed && Number.isSafeInteger(item.viewerPage) && item.viewerPage > 0), `${block}/${sourceType}: synced reading lacks a positive viewer page`);
    const worksheet = coach.buildOutputWorksheet(fileName, block, profile);
    assert(worksheet.includes("Status: SYNC READY"), `${block}/${sourceType}: ready worksheet status missing`);
    assert(worksheet.includes(`PDF shelf record: ${profile.pdfId}`), `${block}/${sourceType}: worksheet does not identify PDF record`);
    assert((worksheet.match(/^## Reading /gm) || []).length === 2, `${block}/${sourceType}: worksheet must contain two readings`);
    assert((worksheet.match(/^## Run /gm) || []).length === 2, `${block}/${sourceType}: worksheet must contain two runs`);
    assert(worksheet.includes("PDF viewer page 7") && worksheet.includes("PDF viewer page 19"), `${block}/${sourceType}: worksheet omits synced viewer pages`);
  }
  const unknown = coach.getReadingAssignment(sources.unmapped, block);
  const unknownText = unknown.readings.map((item) => `${item.title} ${item.detail}`).join(" ");
  assert(!/\bPDF pages?\s+\d/i.test(unknownText), `${block}/unmapped: invented exact page guidance`);
  assert(/explicit consent/.test(unknown.summary) && /unconfirmed/.test(unknown.summary), `${block}/unmapped: opt-in analysis boundary is not explicit`);
}
function visibleAssignmentText(assignment) {
  return [assignment.title, assignment.summary, ...assignment.steps, assignment.artifact, assignment.gate,
    ...assignment.readings.flatMap((item) => [item.title, item.detail, item.source, item.artifact, item.gate])].join(" ");
}
const exactPageClaim = /\b(?:PDF\s+)?pages?\s+\d/i;
const pendingHls = coach.getReadingAssignment(sources.hlstrans, "day3");
assert(!exactPageClaim.test(visibleAssignmentText(pendingHls)), "Unconfirmed HLStrans must not expose exact page claims anywhere in the visible assignment");
const wrongVersion = synchronizedProfile("hlstrans", "day3");
wrongVersion.identity.version = "arXiv:2507.04315v2";
const wrongHls = coach.getReadingAssignment(sources.hlstrans, "day3", wrongVersion);
assert(!wrongHls.sync.ready && !exactPageClaim.test(visibleAssignmentText(wrongHls)), "Wrong HLStrans version must remain pending without exact pages anywhere in the visible assignment");
const unsafePage = synchronizedProfile("textbook", "day1");
unsafePage.blocks.day1.locators[0].viewerPage = Number.MAX_SAFE_INTEGER + 1;
assert(!coach.getReadingAssignment(sources.textbook, "day1", unsafePage).sync.ready, "Unsafe viewer page must block synchronization");
const misleadingName = synchronizedProfile("unmapped", "day1", "pdf-misleading-name");
assert(coach.getReadingAssignment(sources.hlstrans, "day1", misleadingName).sourceType === "unmapped", "Explicit learner source selection must override a misleading filename");
assert(coach.classifySource("DDCA.pdf") === "textbook", "DDCA alias is not recognized");
assert(coach.classifySource("HLStrans-paper.pdf") === "hlstrans", "HLStrans alias is not recognized");
assert(/defer/i.test(coach.getReadingAssignment(sources.hlstrans, "day1").title), "Day 1 HLStrans route must be deferred");
assert(/defer/i.test(coach.getReadingAssignment(sources.hlstrans, "day2").title), "Day 2 HLStrans route must be deferred");
"""
result = subprocess.run(
    ["node", "-e", coach_contract_check, str(reading_coach_script)],
    check=False,
    capture_output=True,
    text=True,
)
if result.returncode:
    sys.stderr.write(result.stderr)
    fail("reading_coach.js failed the 2+2 source/block behavior contract")

for required_source_marker in (
    "async function updatePdfMetadata(metadata)",
    "const changeRequiresReconfirmation = identityChanged || locatorChanged",
    "changeRequiresReconfirmation ? false : locatorChecks[index]",
    "Number.isSafeInteger(page)",
    "#page=${page}",
    "coachProfile",
):
    if required_source_marker not in pdf_shelf_source:
        fail(f"pdf_shelf.js is missing synchronization/jump behavior: {required_source_marker}")

consent_binding = 'pdfAnalysisConsent.addEventListener("change"'
analysis_binding = 'analyzePdfLocally.addEventListener("click"'
apply_binding = 'applyAnalysisLocators.addEventListener("click"'
sync_binding = 'coachSyncForm.addEventListener("submit"'
consent_start = pdf_shelf_source.find(consent_binding)
analysis_start = pdf_shelf_source.find(analysis_binding)
analysis_end = pdf_shelf_source.find(apply_binding, analysis_start)
apply_end = pdf_shelf_source.find(sync_binding, analysis_end)
if min(consent_start, analysis_start, analysis_end, apply_end) < 0 or not consent_start < analysis_start < analysis_end < apply_end:
    fail("pdf_shelf.js must bind consent, analysis click, locator application, and sync save in order")
analysis_handler = pdf_shelf_source[analysis_start:analysis_end]
for required_step in (
    "if (!activePdfRecord || !pdfAnalysisConsent.checked) return;",
    "const stored = await getPdfBlob(record.id)",
    'fetch("/api/v1/session"',
    "fetch(`/api/v1/pdf-candidates/${block}`",
    '"Content-Type": "application/pdf"',
    '"X-Study-Token": sessionPayload.token',
    "validateLocalAnalysis(payload)",
    "persistPdfAnalysis(record.id, block, validated)",
    "selectedGeneration !== selectionGeneration",
):
    if required_step not in analysis_handler:
        fail(f"pdf_shelf.js opt-in analysis click is missing step: {required_step}")
for atomic_merge_marker in (
    "async function persistPdfAnalysis(recordId, block, analysis)",
    "const record = await requestResult(store.get(recordId))",
    "const profile = copyProfileForEdit(getStoredCoachProfile(record))",
):
    if atomic_merge_marker not in pdf_shelf_source:
        fail(f"pdf_shelf.js must merge analysis into fresh PDF metadata: {atomic_merge_marker}")
for fetch_marker in ('fetch("/api/v1/session"', "fetch(`/api/v1/pdf-candidates/${block}`"):
    if pdf_shelf_source.count(fetch_marker) != 1 or fetch_marker not in analysis_handler:
        fail(f"pdf_shelf.js must fetch only after the explicit analysis click: {fetch_marker}")
consent_handler = pdf_shelf_source[consent_start:analysis_start]
if "analyzePdfLocally.disabled = !activePdfRecord || !pdfAnalysisConsent.checked" not in consent_handler:
    fail("pdf_shelf.js consent change must gate the local-analysis button")
apply_handler = pdf_shelf_source[analysis_end:apply_end]
for required_step in (
    "activePdfAnalysis.candidates.length !== 2",
    "coachLocator1Confirmed.checked = false",
    "coachLocator2Confirmed.checked = false",
    "unconfirmed locators",
):
    if required_step not in apply_handler:
        fail(f"pdf_shelf.js candidate application is missing preview/confirm boundary: {required_step}")
for fixed_starter_marker in (
    "code.textContent = starter.code",
    "navigator.clipboard.writeText(starter.code)",
    'preview.addEventListener("click", () => jumpToPdfPage(candidate.viewerPage))',
):
    if fixed_starter_marker not in pdf_shelf_source:
        fail(f"pdf_shelf.js is missing copy/preview-only starter behavior: {fixed_starter_marker}")
for forbidden_execution_marker in ("eval(", "new Function("):
    if forbidden_execution_marker in pdf_shelf_source:
        fail(f"pdf_shelf.js must never execute PDF-derived starter content: {forbidden_execution_marker}")

controller_contract = {
    'coachSyncForm.addEventListener("submit"': (
        "coachProfile = collectCoachProfile()",
        "await updatePdfMetadata(updatedRecord)",
        "activePdfRecord = updatedRecord",
        "renderReadingCoach(activePdfRecord)",
        "profileForCoach(activePdfRecord)",
    ),
    'copyOutputWorksheet.addEventListener("click"': (
        "copyOutputWorksheet.disabled",
        "navigator.clipboard.writeText(coachOutputWorksheet.textContent)",
    ),
}
for binding, required_steps in controller_contract.items():
    start = pdf_shelf_source.find(binding)
    end = pdf_shelf_source.find("\n});", start)
    if start < 0 or end < 0:
        fail(f"pdf_shelf.js is missing controller binding: {binding}")
    handler = pdf_shelf_source[start:end]
    for step in required_steps:
        if step not in handler:
            fail(f"pdf_shelf.js controller binding {binding} is missing step: {step}")

print("PASS: strict UTF-8 assets, Python/JavaScript syntax, WaveJSON, E0-E10 trace, study links, ASCII-safe PDF workflow, opt-in loopback PDF analysis, exactly-two-or-fail candidates, learner-confirmed PDF sync, fixed copy-only starters, one-click page locators, 2+2 reading/run worksheet contract, PDF shelf structure, and quiz count agree.")
