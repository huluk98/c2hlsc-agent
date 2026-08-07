#!/usr/bin/env python3
"""Extract local PDF text and rank two RTL study readings per sprint block."""

from __future__ import annotations

import argparse
from functools import lru_cache
import json
import os
import pathlib
import re
import selectors
import shutil
import signal
import subprocess
import time
from typing import Any


ANALYZER_VERSION = "1"
MAX_PAGES = 650
MAX_SNIPPET_CHARS = 520
MAX_EXTRACT_BYTES = 8 * 1024 * 1024
MAX_INFO_BYTES = 64 * 1024


class AnalysisError(RuntimeError):
    """A safe, user-displayable local PDF analysis failure."""


class OutputLimitError(RuntimeError):
    """A Poppler child exceeded its bounded stdout allowance."""


PROFILES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "day1": (
        {
            "id": "boolean-gates",
            "title": "Boolean algebra, gates, XOR, and adders",
            "stage": "Preflight and Stages 1-2",
            "artifact": "XOR and half-adder truth tables, equations, RTL, and exhaustive checks.",
            "gate": "MANUAL: every input row agrees with an independent truth table.",
            "terms": {
                "boolean algebra": 5, "truth table": 5, "logic gate": 4, "exclusive or": 5,
                "xor": 4, "half adder": 6, "full adder": 3, "two's complement": 3,
            },
            "minScore": 12,
            "minMatched": 2,
            "requiredGroups": (
                ("boolean algebra", "truth table", "logic gate"),
                ("exclusive or", "xor", "half adder", "full adder"),
            ),
            "starter": "xor_half_adder",
        },
        {
            "id": "combinational-rtl",
            "title": "Muxes, combinational RTL, width, and signedness",
            "stage": "Stages 3-4",
            "artifact": "Two mux implementations, a corrected latch example, and a width/sign worksheet.",
            "gate": "MANUAL: truth tables match, no unintended latch remains, and widths are explained.",
            "terms": {
                "multiplexer": 6, "mux": 4, "combinational logic": 5, "always_comb": 6,
                "hardware description language": 4, "systemverilog": 3, "bit width": 4,
                "signed": 2, "latch": 4, "blocking assignment": 4,
            },
            "minScore": 12,
            "minMatched": 2,
            "requiredGroups": (
                ("multiplexer", "mux"),
                ("combinational logic", "always_comb", "hardware description language", "systemverilog", "latch"),
            ),
            "starter": "mux",
        },
    ),
    "day2": (
        {
            "id": "sequential-state",
            "title": "Clocked state, registers, reset, and nonblocking assignment",
            "stage": "Stage 5",
            "artifact": "An edge-by-edge old-state/new-state prediction table and clocked RTL.",
            "gate": "MANUAL: every right-hand side uses pre-edge state.",
            "terms": {
                "sequential logic": 5, "flip-flop": 5, "register": 3, "clock edge": 5,
                "nonblocking assignment": 6, "always_ff": 6, "synchronous reset": 5,
                "setup time": 3, "hold time": 3,
            },
            "preferEarliestStrongPage": True,
            "minScore": 12,
            "minMatched": 2,
            "requiredGroups": (
                ("sequential logic", "flip-flop", "register"),
                ("clock edge", "nonblocking assignment", "always_ff", "synchronous reset", "setup time", "hold time"),
            ),
            "starter": "registered_stage",
        },
        {
            "id": "fsm-transactions",
            "title": "Finite-state machines and transaction timing",
            "stage": "Stages 6-8",
            "artifact": "FSM tables plus waveform and ready/valid transfer classifications.",
            "gate": "MANUAL FSM/handshake review plus LOCAL waveform simulation.",
            "terms": {
                "finite state machine": 7, "state transition": 5, "next state": 4,
                "moore machine": 5, "mealy machine": 5, "ready valid": 7,
                "handshake": 5, "latency": 3, "throughput": 3, "pipeline": 3,
            },
            "requiredAny": (
                "finite state machine", "state transition", "next state", "moore machine",
                "mealy machine", "ready valid", "handshake",
            ),
            "minScore": 12,
            "minMatched": 2,
            "requiredGroups": (
                ("finite state machine", "moore machine", "mealy machine"),
                ("state transition", "next state"),
            ),
            "starter": "fsm",
        },
    ),
    "day3": (
        {
            "id": "verification-synthesis",
            "title": "Testbench, oracle, coverage, and synthesis evidence",
            "stage": "Stages 9-10",
            "artifact": "Independent-oracle tests, mutation evidence, and annotated synthesis results.",
            "gate": "LOCAL: exhaustive, mutation, lint, and synthesis checks pass.",
            "terms": {
                "testbench": 8, "self-checking": 10, "test vector": 6,
                "verification": 4, "coverage": 5, "assertion": 4,
                "synthesis": 5, "netlist": 3, "timing analysis": 4, "resource utilization": 4,
                "simulation": 2, "formal verification": 5,
            },
            "minScore": 12,
            "minMatched": 2,
            "preferEarliestStrongPage": True,
            "requiredGroups": (
                ("testbench", "self-checking", "verification", "assertion", "formal verification"),
                ("test vector", "coverage", "synthesis", "netlist", "timing analysis", "resource utilization", "simulation"),
            ),
            "starter": "self_checking_tb",
        },
        {
            "id": "hls-evidence",
            "title": "C-to-HLS transformations and external evidence",
            "stage": "Stages 11-12",
            "artifact": "An HLS evidence-ledger row and a filled generation contract.",
            "gate": "MANUAL literature review; EXTERNAL only for preserved real-tool results.",
            "terms": {
                "high-level synthesis": 8, "hls": 3, "c to rtl": 7, "co-simulation": 7,
                "cosimulation": 7, "pragma": 4, "initiation interval": 6,
                "quality of results": 5, "latency": 2, "resource": 2,
            },
            "requiredAny": ("high-level synthesis", "c to rtl", "co-simulation", "cosimulation", "pragma", "initiation interval", "quality of results"),
            "minScore": 12,
            "minMatched": 2,
            "requiredGroups": (
                ("high-level synthesis", "hls", "c to rtl"),
                ("co-simulation", "cosimulation", "pragma", "initiation interval", "quality of results", "latency", "resource"),
            ),
            "starter": "hls_reference",
        },
    ),
}


STARTERS: dict[str, dict[str, str]] = {
    "xor_half_adder": {
        "title": "XOR and half-adder starter",
        "language": "systemverilog",
        "code": """module xor_gate(input logic a, b, output logic y);
  assign y = a ^ b;
endmodule

module half_adder(input logic a, b, output logic sum, carry);
  assign sum   = a ^ b;
  assign carry = a & b;
endmodule""",
    },
    "mux": {
        "title": "Complete combinational mux starter",
        "language": "systemverilog",
        "code": """module mux2 #(parameter int W = 8) (
  input  logic [W-1:0] d0, d1,
  input  logic         sel,
  output logic [W-1:0] y
);
  always_comb begin
    y = d0;
    if (sel) y = d1;
  end
endmodule""",
    },
    "registered_stage": {
        "title": "Synchronous registered-stage starter",
        "language": "systemverilog",
        "code": """module registered_stage #(parameter int W = 8) (
  input  logic         clk, rst,
  input  logic         in_valid,
  input  logic [W-1:0] in_data,
  output logic         out_valid,
  output logic [W-1:0] out_data
);
  always_ff @(posedge clk) begin
    if (rst) begin
      out_valid <= 1'b0;
      out_data  <= '0;
    end else begin
      out_valid <= in_valid;
      if (in_valid) out_data <= in_data;
    end
  end
endmodule""",
    },
    "fsm": {
        "title": "Two-process Moore FSM starter",
        "language": "systemverilog",
        "code": """typedef enum logic [1:0] {IDLE, RUN, DONE} state_t;
state_t state_q, state_d;

always_comb begin
  state_d = state_q;
  unique case (state_q)
    IDLE: if (start) state_d = RUN;
    RUN:  if (finish) state_d = DONE;
    DONE: state_d = IDLE;
    default: state_d = IDLE;
  endcase
end

always_ff @(posedge clk) begin
  if (rst) state_q <= IDLE;
  else     state_q <= state_d;
end""",
    },
    "self_checking_tb": {
        "title": "Independent self-checking testbench starter",
        "language": "systemverilog",
        "code": """task automatic check_case(input logic [7:0] a, b);
  logic [8:0] expected;
  expected = {1'b0, a} + {1'b0, b};
  drive_and_wait(a, b);
  if (sum !== expected)
    $fatal(1, "mismatch a=%0d b=%0d got=%0d expected=%0d",
           a, b, sum, expected);
endtask""",
    },
    "hls_reference": {
        "title": "Defined-width HLS reference starter",
        "language": "c",
        "code": """#include <stdint.h>

uint16_t add_u8(uint8_t a, uint8_t b) {
  return (uint16_t)a + (uint16_t)b;
}""",
    },
}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


@lru_cache(maxsize=None)
def _term_pattern(term: str) -> re.Pattern[str]:
    pieces = [re.escape(piece) for piece in term.split()]
    return re.compile(r"(?<!\w)" + r"[\s/-]+".join(pieces) + r"(?!\w)")


def _term_count(normalized: str, term: str) -> int:
    return len(_term_pattern(term).findall(normalized))


def _contains_term(normalized: str, term: str) -> bool:
    return _term_pattern(term).search(normalized) is not None


def _score_page(text: str, terms: dict[str, int]) -> tuple[int, list[str]]:
    normalized = _normalized(text)
    if len(normalized) < 80:
        return 0, []
    first_lines = [re.sub(r"\s+", " ", line).strip().lower() for line in text.splitlines()[:50]]
    if any("index.qxd" in line or line in {"index", "table of contents", "contents"} for line in first_lines):
        return 0, []
    score = 0
    matched: list[str] = []
    heading_text = normalized[:1400]
    for term, weight in terms.items():
        count = _term_count(normalized, term)
        if not count:
            continue
        matched.append(term)
        score += weight * min(count, 4)
        if _contains_term(heading_text, term):
            score += weight
    return score, matched


def _heading(text: str, matched_terms: list[str], page: int) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if 4 <= len(line) <= 120]
    chapter_pattern = re.compile(r"^chapter\s+\d+\b", re.IGNORECASE)
    section_pattern = re.compile(
        r"^\d+\s*\.\s*\d+(?:\s*\.\s*\d+){0,2}\s+[A-Z][A-Za-z0-9-]*"
    )
    roman_section_pattern = re.compile(
        r"^[IVXLCDM]+\.\s+[A-Z](?:[A-Z ]*?[A-Z])(?=\s+[A-Za-z][a-z]|$)"
    )
    for line in lines[:140]:
        if ".qxd" in line.lower():
            continue
        roman_match = roman_section_pattern.search(line)
        if roman_match:
            heading = roman_match.group(0).strip()
            return re.sub(r"\b([A-Z])\s+([A-Z]{2,})\b", r"\1\2", heading)
        if chapter_pattern.search(line) or section_pattern.search(line):
            return re.sub(r"\s*\.\s*", ".", line)
    for line in lines[:80]:
        normalized = _normalized(line)
        if any(_contains_term(normalized, term) for term in matched_terms):
            return line
    return f"PDF viewer page {page}"


def _snippet(text: str, matched_terms: list[str]) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    selected = 0
    for index, line in enumerate(lines):
        normalized = _normalized(line)
        if any(_contains_term(normalized, term) for term in matched_terms):
            selected = index
            break
    excerpt = " ".join(lines[max(0, selected - 1): selected + 3])
    return excerpt[:MAX_SNIPPET_CHARS]


def _confidence(score: int) -> str:
    if score >= 28:
        return "high"
    if score >= 12:
        return "medium"
    if score > 0:
        return "low"
    return "unavailable"


def _is_hlstrans_v3(pages: list[str]) -> bool:
    """Recognize the supplied paper from extracted content, never its filename."""
    page_one = _normalized(pages[0])
    page_two = _normalized(pages[1])
    page_one_identity = (
        "2507.04315v3" in page_one
        and "hlstrans" in page_one.replace(" ", "")
        and "dataset for c- to -hls" in page_one
        and "qingyun zou" in page_one
    )
    page_one_evidence = sum(
        _contains_term(page_one, term)
        for term in ("high-level synthesis", "testbenches", "transformations", "synthesis-based annotations")
    )
    page_two_evidence = sum(
        _contains_term(page_two, term)
        for term in ("original c kernel", "optimized hls implementation", "validation testbench", "latency", "resource metrics", "five categories")
    )
    return page_one_identity and page_one_evidence >= 2 and page_two_evidence >= 4


def _hlstrans_v3_candidates(pages: list[str]) -> list[dict[str, Any]]:
    mappings = (
        {
            "id": "hls-task-definition",
            "title": "Why C-to-HLS is a hardware transformation task",
            "viewerPage": 1,
            "location": "Abstract and Section 1 - task definition",
            "matchedTerms": ["high-level synthesis", "testbenches", "synthesis", "transformations"],
            "stage": "Stage 11A",
            "artifact": "Explain why this is not textual translation and list the five transformation categories.",
            "gate": "MANUAL: distinguish the paper's stated task and author claims from independently verified results.",
        },
        {
            "id": "hls-dataset-evidence",
            "title": "Dataset entries, synthesis annotations, and evidence limits",
            "viewerPage": 2,
            "location": "Section 1 - dataset contents and claimed evidence",
            "matchedTerms": ["validation testbench", "latency", "resource metrics", "five categories"],
            "stage": "Stage 11A",
            "artifact": "Record the entry triple, synthesis annotations, testbench role, and claims needing later evaluation evidence.",
            "gate": "MANUAL: do not treat pages 1-2 as proof that a new program passes CoSim or has fair QoR.",
        },
    )
    candidates = []
    for index, mapping in enumerate(mappings):
        page = mapping["viewerPage"]
        candidates.append({
            **mapping,
            "available": True,
            "score": 100 - index,
            "confidence": "high",
            "snippet": _snippet(pages[page - 1], mapping["matchedTerms"]),
        })
    return candidates


def _candidate_for_profile(
    pages: list[str], profile: dict[str, Any], excluded_pages: set[int]
) -> dict[str, Any]:
    ranked: list[tuple[int, int, list[str]]] = []
    for index, text in enumerate(pages, start=1):
        score, matched = _score_page(text, profile["terms"])
        if profile.get("requiredAny"):
            normalized = _normalized(text)
            if not any(_contains_term(normalized, required) for required in profile["requiredAny"]):
                score, matched = 0, []
        if profile.get("requiredGroups"):
            normalized = _normalized(text)
            if not all(
                any(_contains_term(normalized, term) for term in group)
                for group in profile["requiredGroups"]
            ):
                score, matched = 0, []
        if score < profile.get("minScore", 1) or len(matched) < profile.get("minMatched", 1):
            score, matched = 0, []
        ranked.append((score, index, matched))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    viable = [item for item in ranked if item[0] > 0 and item[1] not in excluded_pages]
    if viable and profile.get("preferEarliestStrongPage"):
        strong_score = max(1, int(viable[0][0] * 0.75))
        strong_pages = [item for item in viable if item[0] >= strong_score]
        chosen = min(strong_pages, key=lambda item: item[1])
    else:
        chosen = viable[0] if viable else None
    if chosen is None:
        return {
            "id": profile["id"], "title": profile["title"], "available": False,
            "viewerPage": None, "location": "No reliable page detected", "score": 0,
            "confidence": "unavailable", "matchedTerms": [], "snippet": "",
            "stage": profile["stage"], "artifact": profile["artifact"], "gate": profile["gate"],
        }
    score, page, matched = chosen
    excluded_pages.update({page - 1, page, page + 1})
    return {
        "id": profile["id"], "title": profile["title"], "available": True,
        "viewerPage": page, "location": _heading(pages[page - 1], matched, page),
        "score": score, "confidence": _confidence(score), "matchedTerms": matched[:8],
        "snippet": _snippet(pages[page - 1], matched), "stage": profile["stage"],
        "artifact": profile["artifact"], "gate": profile["gate"],
    }


def analyze_pages(pages: list[str], file_name: str, block: str) -> dict[str, Any]:
    if block not in PROFILES:
        raise AnalysisError(f"Unsupported study block: {block}")
    if not pages:
        raise AnalysisError("No PDF pages were extracted.")
    known_hlstrans = block == "day3" and len(pages) >= 2 and _is_hlstrans_v3(pages)
    if known_hlstrans:
        candidates = _hlstrans_v3_candidates(pages)
    else:
        excluded_pages: set[int] = set()
        candidates = [
            _candidate_for_profile(pages, profile, excluded_pages)
            for profile in PROFILES[block]
        ]
    starters = []
    for candidate, profile in zip(candidates, PROFILES[block], strict=True):
        if not candidate["available"]:
            continue
        starter = STARTERS[profile["starter"]]
        starters.append({
            "id": profile["starter"], "title": starter["title"],
            "language": starter["language"], "code": starter["code"],
            "detectedFrom": candidate["id"],
            "caveat": "Deterministic study starter selected by detected concepts; verify against your specification and gates.",
        })
    return {
        "analyzerVersion": ANALYZER_VERSION,
        "fileName": pathlib.Path(file_name).name,
        "block": block,
        "pageCount": len(pages),
        "candidates": candidates,
        "starters": starters,
        "warnings": [
            (
                "Pages 1-2 use the content-identified HLStrans v3 sprint map; source identity still requires learner confirmation."
                if known_hlstrans else
                "Page matches are keyword-ranked local suggestions, not semantic proof."
            ),
            "Review each candidate in the PDF before confirming its locator or using a starter.",
        ],
    }


def _pdf_info(path: pathlib.Path) -> dict[str, str]:
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        raise AnalysisError("pdfinfo is required. Install Poppler, then restart make serve.")
    try:
        stdout, returncode = _run_poppler(
            [pdfinfo, str(path)], timeout=10, max_output_bytes=MAX_INFO_BYTES,
        )
    except TimeoutError as error:
        raise AnalysisError("pdfinfo timed out while inspecting this file.") from error
    except OutputLimitError as error:
        raise AnalysisError("pdfinfo returned excessive metadata.") from error
    if returncode:
        raise AnalysisError("pdfinfo could not inspect this PDF.")
    info: dict[str, str] = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            info[key.strip()] = value.strip()
    try:
        page_count = int(info.get("Pages", "0"))
    except ValueError as error:
        raise AnalysisError("The PDF page count is invalid.") from error
    if page_count < 1:
        raise AnalysisError("The PDF has no readable pages.")
    if page_count > MAX_PAGES:
        raise AnalysisError(f"The PDF has {page_count} pages; the local limit is {MAX_PAGES}.")
    if info.get("Encrypted", "no").lower() != "no":
        raise AnalysisError("Encrypted PDFs are not analyzed by this local route.")
    return info


def extract_pdf_pages(path: pathlib.Path) -> tuple[list[str], dict[str, str]]:
    info = _pdf_info(path)
    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        raise AnalysisError("pdftotext is required. Install Poppler, then restart make serve.")
    try:
        stdout, returncode = _run_poppler(
            [pdftotext, "-layout", "-enc", "UTF-8", str(path), "-"],
            timeout=30,
            max_output_bytes=MAX_EXTRACT_BYTES,
        )
    except TimeoutError as error:
        raise AnalysisError("Text extraction exceeded the 30-second local timeout.") from error
    except OutputLimitError as error:
        raise AnalysisError("Extracted text exceeds the 8 MiB local analysis limit.") from error
    if returncode:
        raise AnalysisError("pdftotext could not extract this PDF.")
    if len(stdout) > MAX_EXTRACT_BYTES:
        raise AnalysisError("Extracted text exceeds the 8 MiB local analysis limit.")
    text = stdout.decode("utf-8", errors="replace").replace("\x00", "")
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    declared_pages = int(info["Pages"])
    if len(pages) < declared_pages:
        pages.extend([""] * (declared_pages - len(pages)))
    elif len(pages) > declared_pages:
        pages = pages[:declared_pages]
    if sum(bool(page.strip()) for page in pages) < max(1, declared_pages // 20):
        raise AnalysisError("Too little text was extracted; this PDF may require OCR.")
    return pages, info


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run_poppler(argv: list[str], timeout: int, max_output_bytes: int) -> tuple[bytes, int]:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if process.stdout is None:
        _terminate_process_group(process)
        raise RuntimeError("Poppler stdout pipe was not created.")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    total = 0
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                raise TimeoutError
            for key, _ in selector.select(timeout=min(0.1, remaining)):
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > max_output_bytes:
                    _terminate_process_group(process)
                    raise OutputLimitError
                chunks.append(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            raise TimeoutError
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        raise TimeoutError from error
    finally:
        selector.close()
        process.stdout.close()
    return b"".join(chunks), process.returncode


def analyze_pdf(path: pathlib.Path, file_name: str, block: str) -> dict[str, Any]:
    pages, info = extract_pdf_pages(path)
    analysis = analyze_pages(pages, file_name, block)
    analysis["identitySuggestion"] = {
        "title": info.get("Title", ""),
        "author": info.get("Author", ""),
        "subject": info.get("Subject", ""),
        "pageCount": int(info["Pages"]),
        "confirmed": False,
    }
    return analysis


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=pathlib.Path)
    parser.add_argument("--block", choices=tuple(PROFILES), default="day1")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        result = analyze_pdf(args.pdf.resolve(), args.pdf.name, args.block)
    except AnalysisError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
