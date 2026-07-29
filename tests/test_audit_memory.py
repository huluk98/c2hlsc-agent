import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.audit_memory import (
    append_cards,
    card_from_outcome,
    load_cards,
    promotable_outcomes,
    promote_run,
    resolve_store_path,
    retrieve_cards,
)
from c2hlsc_agent.cli import build_parser, run_convert
from c2hlsc_agent.config import AgentConfig
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.hlsc_repair_agent import RepairFileChange, RepairOutcome
from c2hlsc_agent.llm import build_repair_prompt


def _change(before: str = "aaa", after: str = "bbb") -> RepairFileChange:
    return RepairFileChange(
        path="src/hls_top.cpp",
        action="llm repair (model=fake, family=synthesis_failure) for csynth stage",
        before_sha256=before,
        after_sha256=after,
        diff="--- a\n+++ b\n@@ -1,2 +1,2 @@\n-old\n+new",
    )


def _outcome(
    iteration: int,
    stage: str = "csynth",
    family: str = "synthesis_failure",
    status: str = "applied_llm",
    with_change: bool = True,
    after: str = "bbb",
) -> RepairOutcome:
    return RepairOutcome(
        iteration=iteration,
        stage=stage,
        family=family,
        owner_agent="hlsc_repair_agent",
        status=status,
        summary="test outcome",
        target_files=("src/hls_top.cpp",) if with_change else (),
        changes=(_change(after=after),) if with_change else (),
        evidence_excerpt="error: 'foo_t' has not been declared\nsome banner line",
        next_action="patch the type",
        repair_scope="src/hls_top.cpp",
    )


class PromotionChainRuleTests(unittest.TestCase):
    def test_only_last_entry_of_a_chain_is_promoted(self):
        history = [
            _outcome(1, status="applied_llm", after="v1"),
            _outcome(2, status="applied_llm", after="v2"),
        ]
        picked = promotable_outcomes(history)
        self.assertEqual([o.iteration for o in picked], [2])

    def test_distinct_chains_each_promote_their_last(self):
        history = [
            _outcome(1, stage="software_equivalence", family="testbench_or_c_semantics", after="v1"),
            _outcome(2, stage="csynth", family="synthesis_failure", after="v2"),
        ]
        picked = promotable_outcomes(history)
        self.assertEqual([o.iteration for o in picked], [1, 2])

    def test_non_promotable_statuses_never_promote(self):
        for status in ("pass", "blocked", "no_change", "oscillation_rejected"):
            history = [_outcome(1, status=status, with_change=(status == "oscillation_rejected"))]
            self.assertEqual(promotable_outcomes(history), [], status)

    def test_entry_without_changes_never_promotes(self):
        self.assertEqual(promotable_outcomes([_outcome(1, with_change=False)]), [])

    def test_failed_run_promotes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store.jsonl"
            fresh = promote_run(store, [_outcome(1)], "proj", functional_status="fail")
            self.assertEqual(fresh, [])
            self.assertFalse(store.exists())

    def test_passing_run_promotes_and_dedups(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store.jsonl"
            first = promote_run(store, [_outcome(1)], "proj", functional_status="pass")
            self.assertEqual(len(first), 1)
            again = promote_run(store, [_outcome(1)], "proj", functional_status="pass")
            self.assertEqual(again, [])
            self.assertEqual(len(load_cards(store)), 1)


class StoreTests(unittest.TestCase):
    def test_round_trip_and_torn_line_tolerance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "nested" / "store.jsonl"
            card = card_from_outcome(_outcome(1), "proj", timestamp="2026-07-23T00:00:00")
            append_cards(store, [card])
            with store.open("a", encoding="utf-8") as handle:
                handle.write('{"torn": ')  # simulated killed writer
            loaded = load_cards(store)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["card_id"], card["card_id"])
            self.assertTrue(loaded[0]["audited"])

    def test_missing_store_loads_empty(self):
        self.assertEqual(load_cards(Path("/nonexistent/audit_memory.jsonl")), [])

    def test_resolve_store_path_precedence(self):
        config = AgentConfig(audit_memory=True, audit_memory_path="/tmp/explicit.jsonl")
        with patch.dict(os.environ, {"C2HLSC_AUDIT_MEMORY": "/tmp/env.jsonl"}):
            self.assertEqual(resolve_store_path(config), Path("/tmp/explicit.jsonl"))
            config.audit_memory_path = None
            self.assertEqual(resolve_store_path(config), Path("/tmp/env.jsonl"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("C2HLSC_AUDIT_MEMORY", None)
            self.assertEqual(resolve_store_path(config), Path("~/.c2hlsc/audit_memory.jsonl").expanduser())

    def test_card_never_contains_golden_source(self):
        # Cards are built from evidence + diffs of generated files only; input.c is
        # never in changes. Guard the harvest surface with a distinctive marker.
        outcome = _outcome(1)
        card = card_from_outcome(outcome, "proj")
        text = json.dumps(card)
        self.assertNotIn("input.c", text)
        self.assertNotIn("_ref", card["evidence_snippet"])


class RetrievalTests(unittest.TestCase):
    def _store_with(self, tmp: Path, *cards) -> Path:
        store = tmp / "store.jsonl"
        append_cards(store, list(cards))
        return store

    def test_family_match_ranked_by_evidence_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            near = card_from_outcome(_outcome(1, after="v1"), "p1")
            far = card_from_outcome(_outcome(2, after="v2"), "p2")
            far = dict(far, card_id="ffff", evidence_snippet="totally unrelated words")
            store = self._store_with(Path(tmp), near, far)
            cards = retrieve_cards(store, "synthesis_failure", "csynth", "error: 'foo_t' has not been declared")
            self.assertEqual(len(cards), 2)
            self.assertIn("foo_t", cards[0])

    def test_stage_fallback_when_family_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = card_from_outcome(_outcome(1), "p1")
            store = self._store_with(Path(tmp), card)
            cards = retrieve_cards(store, "never_seen_family", "csynth", "whatever")
            self.assertEqual(len(cards), 1)

    def test_no_match_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = card_from_outcome(_outcome(1), "p1")
            store = self._store_with(Path(tmp), card)
            self.assertEqual(retrieve_cards(store, "other_family", "cosim", "x"), [])

    def test_duplicate_card_ids_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = card_from_outcome(_outcome(1), "p1")
            store = self._store_with(Path(tmp), card, card)  # concurrent-append duplicate
            cards = retrieve_cards(store, "synthesis_failure", "csynth", "error")
            self.assertEqual(len(cards), 1)


class PromptInjectionTests(unittest.TestCase):
    def _analysis(self, tmp: Path):
        from c2hlsc_agent.analyze import analyze_source

        source = tmp / "input.c"
        source.write_text("int bump(int n) { return n + 1; }\n", encoding="utf-8")
        return analyze_source(source, "bump", AgentConfig())

    def test_cards_render_between_history_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis = self._analysis(Path(tmp))
            decision = type("D", (), {"family": "synthesis_failure", "next_action": "", "repair_scope": ""})()
            _, without_cards = build_repair_prompt(
                analysis, decision, "csynth", "the evidence", "src/hls_top.cpp", "int bump(int n){return n+1;}"
            )
            _, with_cards = build_repair_prompt(
                analysis, decision, "csynth", "the evidence", "src/hls_top.cpp", "int bump(int n){return n+1;}",
                audit_cards=["[csynth/synthesis_failure] fix: add the include"],
            )
            self.assertNotIn("AUDITED", without_cards)
            self.assertIn("AUDITED", with_cards)
            self.assertIn("add the include", with_cards)
            # Cards section precedes the evidence block, so the tail-sliced evidence
            # budget is untouched.
            self.assertLess(with_cards.index("AUDITED"), with_cards.index("Earliest-failure evidence"))

    def test_none_cards_leaves_prompt_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis = self._analysis(Path(tmp))
            decision = type("D", (), {"family": "f", "next_action": "", "repair_scope": ""})()
            base = build_repair_prompt(analysis, decision, "csynth", "e", "src/hls_top.cpp", "int bump(int n){return n+1;}")
            with_none = build_repair_prompt(
                analysis, decision, "csynth", "e", "src/hls_top.cpp", "int bump(int n){return n+1;}", audit_cards=None
            )
            self.assertEqual(base, with_none)


class CliIntegrationTests(unittest.TestCase):
    def _convert_args(self, input_path: Path, out_dir: Path, store: Path) -> object:
        return build_parser().parse_args(
            [
                "convert",
                "--input",
                str(input_path),
                "--top",
                "bump",
                "--out",
                str(out_dir),
                "--no-run-vitis",
                "--max-iterations",
                "2",
                "--auto-repair",
                "--audit-memory-path",
                str(store),
            ]
        )

    def test_passing_run_with_mechanical_repair_promotes_a_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.c"
            input_path.write_text(
                "#include <stddef.h>\nsize_t bump(size_t n) { return n + 1; }\n", encoding="utf-8"
            )
            store = root / "kb" / "store.jsonl"
            args = self._convert_args(input_path, root / "out", store)
            first = VerificationState()
            first.add_phase(PhaseResult("software_equivalence", "fail", stderr="error: 'size_t' has not been declared"))
            second = VerificationState()
            second.add_phase(PhaseResult("software_equivalence", "pass"))

            with patch("c2hlsc_agent.cli.verify_project", side_effect=[first, second]):
                rc = run_convert(args)

            self.assertEqual(rc, 0)
            cards = load_cards(store)
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["mechanism"], "mechanical")
            self.assertEqual(cards[0]["stage"], "software_equivalence")
            self.assertTrue(cards[0]["audited"])

    def test_failing_run_promotes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.c"
            input_path.write_text(
                "#include <stddef.h>\nsize_t bump(size_t n) { return n + 1; }\n", encoding="utf-8"
            )
            store = root / "kb" / "store.jsonl"
            args = self._convert_args(input_path, root / "out", store)
            failing = VerificationState()
            failing.add_phase(PhaseResult("software_equivalence", "fail", stderr="error: 'size_t' has not been declared"))

            with patch("c2hlsc_agent.cli.verify_project", return_value=failing):
                rc = run_convert(args)

            self.assertEqual(rc, 1)
            self.assertFalse(store.exists())

    def test_disabled_by_default_leaves_no_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.c"
            input_path.write_text("int bump(int n) { return n + 1; }\n", encoding="utf-8")
            args = build_parser().parse_args(
                [
                    "convert",
                    "--input",
                    str(input_path),
                    "--top",
                    "bump",
                    "--out",
                    str(root / "out"),
                    "--no-run-vitis",
                ]
            )
            state = VerificationState()
            state.add_phase(PhaseResult("software_equivalence", "pass"))
            marker = root / "should_not_exist.jsonl"
            with patch.dict(os.environ, {"C2HLSC_AUDIT_MEMORY": str(marker)}):
                with patch("c2hlsc_agent.cli.verify_project", return_value=state):
                    rc = run_convert(args)
            self.assertEqual(rc, 0)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
