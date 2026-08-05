#!/usr/bin/env python3
"""Unit checks for deterministic page ranking and starter selection."""

from __future__ import annotations

import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from pdf_study_analyzer import (  # noqa: E402
    AnalysisError,
    OutputLimitError,
    _normalized,
    _run_poppler,
    _term_count,
    analyze_pages,
)


class PdfStudyAnalyzerTests(unittest.TestCase):
    def test_day1_returns_two_distinct_page_candidates_and_starters(self) -> None:
        pages = [
            "Cover and copyright",
            "Chapter 2 Boolean Algebra\nTruth table logic gate XOR exclusive or half adder. " * 3,
            "Boolean review truth table",
            "Chapter 4 Hardware Description Languages\nSystemVerilog multiplexer mux combinational logic always_comb latch blocking assignment. " * 3,
            "Index",
        ]
        result = analyze_pages(pages, "book.pdf", "day1")
        self.assertEqual([candidate["viewerPage"] for candidate in result["candidates"]], [2, 4])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(len(result["starters"]), 2)
        self.assertTrue(all(candidate["available"] for candidate in result["candidates"]))
        self.assertTrue(all("verify" in starter["caveat"] for starter in result["starters"]))

    def test_unmatched_profile_is_explicitly_unavailable(self) -> None:
        result = analyze_pages(["A poetry anthology with unrelated prose."], "other.pdf", "day3")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(all(not candidate["available"] for candidate in result["candidates"]))
        self.assertEqual(result["starters"], [])

    def test_index_pages_and_generic_hls_mentions_do_not_create_false_matches(self) -> None:
        pages = [
            "Index.qxd\nBoolean algebra truth table XOR half adder multiplexer always_comb " * 4,
            "A general article mentions HLS repeatedly but defines no high-level synthesis evidence.",
        ]
        day1 = analyze_pages(pages, "index.pdf", "day1")
        day3 = analyze_pages(pages, "notes.pdf", "day3")
        self.assertTrue(all(not item["available"] for item in day1["candidates"]))
        self.assertFalse(day3["candidates"][1]["available"])

    def test_content_identified_hlstrans_v3_uses_sprint_pages_one_and_two(self) -> None:
        pages = [
            "arXiv:2507.04315v3 HLS TRANS : DATASET FOR C- TO -HLS HARDWARE CODE SYNTHESIS "
            "Qingyun Zou High-Level Synthesis transformations testbenches synthesis.",
            "Each entry includes a validation testbench, latency and resource metrics, and five categories.",
            "Later evaluation material.",
        ]
        result = analyze_pages(pages, "misleading-name.pdf", "day3")
        self.assertEqual([item["viewerPage"] for item in result["candidates"]], [1, 2])
        self.assertEqual([item["id"] for item in result["candidates"]], [
            "hls-task-definition", "hls-dataset-evidence",
        ])
        self.assertTrue(all(item["available"] for item in result["candidates"]))

    def test_substrings_and_single_concepts_do_not_qualify_unrelated_pages(self) -> None:
        pages = [
            ("An unrelated paragraph repeats one acronym while discussing travel, weather, music, "
             "and ordinary correspondence without technical instruction. " + "XOR " * 12),
            ("General prose repeats one device name while discussing schedules, notebooks, meetings, "
             "and unrelated editorial work. " + "multiplexer " * 8),
            ("The author assigned a long writing task; this ordinary prose discusses deadlines, "
             "review notes, and editorial responsibilities. " + "assigned " * 10),
            "More unrelated prose deliberately padded beyond the page-length screening threshold. " * 2,
        ]
        result = analyze_pages(pages, "prose.pdf", "day1")
        self.assertTrue(all(not item["available"] for item in result["candidates"]))
        self.assertEqual(_term_count(_normalized(pages[2]), "signed"), 0)

    def test_hlstrans_override_requires_independent_page_two_evidence(self) -> None:
        pages = [
            "arXiv:2507.04315v3 HLS TRANS : DATASET FOR C- TO -HLS HARDWARE CODE SYNTHESIS "
            "Qingyun Zou High-Level Synthesis transformations testbenches synthesis-based annotations.",
            "",
            "Unrelated appendix.",
        ]
        result = analyze_pages(pages, "paper.pdf", "day3")
        self.assertFalse(any(
            item["available"] and item["viewerPage"] == 2 for item in result["candidates"]
        ))

    def test_poppler_stdout_is_bounded_while_streaming(self) -> None:
        with self.assertRaises(OutputLimitError):
            _run_poppler(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096)"],
                timeout=5,
                max_output_bytes=128,
            )

    def test_invalid_block_fails_closed(self) -> None:
        with self.assertRaises(AnalysisError):
            analyze_pages(["truth table"], "book.pdf", "day4")


if __name__ == "__main__":
    unittest.main()
