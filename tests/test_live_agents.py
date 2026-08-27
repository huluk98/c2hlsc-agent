"""Offline tests for the four newly-live agents: failure_analyst refinement,
audit_memory promotion/retrieval, contract_planner proposals, and the stimulus
augmenter. Everything here runs without a network, a model, or the anthropic SDK --
fake clients only -- because ordinary CI exercises exactly this path."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.audit_memory import (
    load_cards,
    memory_path,
    promote_repair_cards,
    relevant_cards,
)
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.contract_planner import propose_contract, write_proposals
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.hlsc_repair_agent import RepairFileChange, RepairOutcome
from c2hlsc_agent.stimulus_augment import validate_vectors
from c2hlsc_agent.testgen import generate_testbench


class ScriptedLLM:
    """Returns queued responses in order; repeats the last one when exhausted."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.model = "scripted"

    def complete(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        self.calls.append((system, user))
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def _write_analysis(tmp: Path, source: str, top: str, config: AgentConfig):
    src = tmp / "input.c"
    src.write_text(source, encoding="utf-8")
    return analyze_source(src, top, config)


_ACCUM_C = (
    "void accum(const int *in, int *out, int n) {\n"
    "    int s = 0;\n"
    "    for (int i = 0; i < n; i++) { s += in[i]; out[i] = s; }\n"
    "}\n"
)


def _accum_config(**overrides) -> AgentConfig:
    base = dict(
        top="accum",
        num_tests=6,
        arguments={
            "in": ArgumentConfig(direction="input", length=4),
            "out": ArgumentConfig(direction="output", length=4),
            "n": ArgumentConfig(range=(0, 4)),
        },
    )
    base.update(overrides)
    return AgentConfig(**base)


class FailureAnalystTests(unittest.TestCase):
    def _mismatch_state(self) -> VerificationState:
        state = VerificationState()
        state.add_phase(
            PhaseResult(
                "software_equivalence",
                "fail",
                1,
                stdout="Mismatch test=3 arg=out index=2 expected=5 actual=7 seed=1",
            )
        )
        return state

    def _decision(self, state):
        from c2hlsc_agent.agent_loop import classify_failure

        return classify_failure(state, False)

    def test_valid_refinement_is_adopted_with_status_pinned(self):
        from c2hlsc_agent.agent_loop import refine_failure_analysis

        state = self._mismatch_state()
        llm = ScriptedLLM(
            json.dumps(
                {
                    "family": "numeric_bitwidth",
                    "owner_agent": "hlsc_repair_agent",
                    "next_action": "Widen the accumulator; the running sum overflows at test 3.",
                    "evidence_needed": ["test 3 inputs", "accumulator type"],
                    "repair_scope": "generated HLS-C only",
                    "status": "blocked",  # a model may NOT set status; must be discarded
                }
            )
        )
        refined = refine_failure_analysis(self._decision(state), state, "software_equivalence", llm)
        self.assertIsNotNone(refined)
        self.assertEqual(refined.family, "numeric_bitwidth")
        self.assertEqual(refined.status, "needs_action")  # pinned, never model-writable
        self.assertIn("overflows", refined.next_action)

    def test_out_of_vocabulary_and_garbage_fall_back(self):
        from c2hlsc_agent.agent_loop import refine_failure_analysis

        state = self._mismatch_state()
        decision = self._decision(state)
        for response in (
            "I think the loop is wrong.",  # prose
            json.dumps({"family": "quantum_flux", "owner_agent": "hlsc_repair_agent", "next_action": "x"}),
            json.dumps({"family": "behavioral_mismatch", "owner_agent": "santa", "next_action": "x"}),
            json.dumps({"family": "behavioral_mismatch", "owner_agent": "hlsc_repair_agent"}),
        ):
            with self.subTest(response=response[:40]):
                refined = refine_failure_analysis(
                    decision, state, "software_equivalence", ScriptedLLM(response)
                )
                self.assertIsNone(refined)

    def test_blocked_decisions_never_reach_the_model(self):
        from c2hlsc_agent.agent_loop import classify_failure, refine_failure_analysis

        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        state.add_phase(PhaseResult("csim", "fail", summary="vitis_hls not found on PATH"))
        decision = classify_failure(state, True)
        self.assertEqual(decision.status, "blocked")
        llm = ScriptedLLM("{}")
        self.assertIsNone(refine_failure_analysis(decision, state, "csim", llm))
        self.assertEqual(llm.calls, [])  # not even called

    def test_analyst_stands_down_on_last_llm_call(self):
        from c2hlsc_agent.hlsc_repair_agent import _analyst_budget_allows

        class Budgeted:
            remaining_llm_calls = 1

        class Roomy:
            remaining_llm_calls = 2

        self.assertFalse(_analyst_budget_allows(Budgeted()))
        self.assertTrue(_analyst_budget_allows(Roomy()))
        self.assertTrue(_analyst_budget_allows(object()))  # raw client: no counter


class AnalystHardeningTests(unittest.TestCase):
    def _state_and_decision(self):
        from c2hlsc_agent.agent_loop import classify_failure

        state = VerificationState()
        state.add_phase(
            PhaseResult("software_equivalence", "fail", 1,
                        stdout="Mismatch test=0 arg=out index=0 expected=1 actual=2 seed=1")
        )
        return state, classify_failure(state, False)

    def test_budget_exhaustion_is_reraised_not_swallowed(self):
        from c2hlsc_agent.agent_loop import refine_failure_analysis
        from c2hlsc_agent.run_control import RunBudgetExceeded

        class Exhausted:
            model = "budgeted"

            def complete(self, system, user, **kw):
                raise RunBudgetExceeded("wall_seconds", "wall-time budget exhausted")

        state, decision = self._state_and_decision()
        with self.assertRaises(RunBudgetExceeded):
            refine_failure_analysis(decision, state, "software_equivalence", Exhausted())

    def test_non_list_evidence_needed_degrades_not_crashes(self):
        from c2hlsc_agent.agent_loop import refine_failure_analysis

        state, decision = self._state_and_decision()
        llm = ScriptedLLM(json.dumps({
            "family": "behavioral_mismatch",
            "owner_agent": "hlsc_repair_agent",
            "next_action": "fix it",
            "evidence_needed": "just look at the log",  # a string, not a list
        }))
        refined = refine_failure_analysis(decision, state, "software_equivalence", llm)
        self.assertIsNotNone(refined)
        self.assertEqual(refined.evidence_needed, decision.evidence_needed)  # fallback


class AuditMemoryTests(unittest.TestCase):
    def _repair_outcome(self, status: str = "applied_llm") -> RepairOutcome:
        change = RepairFileChange(
            path="src/hls_top.cpp",
            action="llm repair",
            before_sha256="a" * 64,
            after_sha256="b" * 64,
            diff="--- a\n+++ b\n-int s = 0;\n+long long s = 0;\n",
        )
        return RepairOutcome(
            iteration=1,
            stage="software_equivalence",
            family="numeric_bitwidth",
            owner_agent="hlsc_repair_agent",
            status=status,
            summary="Applied LLM repair to src/hls_top.cpp",
            target_files=("src/hls_top.cpp",),
            changes=(change,),
            evidence_excerpt="Mismatch ...",
            next_action="",
            repair_scope="",
        )

    def _config(self, tmp: Path) -> AgentConfig:
        return AgentConfig(top="accum", memory_dir=str(tmp))

    def test_unverified_runs_promote_nothing_and_touch_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            memory_root = Path(raw) / "never_created"
            config = AgentConfig(top="accum", memory_dir=str(memory_root))
            count = promote_repair_cards(
                Path(raw), config, [self._repair_outcome()], top="accum", verified=False
            )
            self.assertEqual(count, 0)
            self.assertFalse(memory_root.exists())  # no trace at all

    def test_only_applied_outcomes_with_changes_promote(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw))
            no_change = RepairOutcome(
                iteration=2, stage="csim", family="unknown", owner_agent="x",
                status="no_change", summary="", target_files=(), changes=(),
                evidence_excerpt="", next_action="", repair_scope="",
            )
            count = promote_repair_cards(
                Path(raw), config, [self._repair_outcome(), no_change], top="accum",
                verified=True, model="m",
            )
            self.assertEqual(count, 1)
            cards = load_cards(config)
            self.assertEqual(len(cards), 1)
            card = cards[0]
            self.assertEqual(card["family"], "numeric_bitwidth")
            self.assertEqual(card["kind"], "llm")
            self.assertIn("long long s", card["diff_excerpt"])
            self.assertEqual(card["verified_scope"], "host_equivalence")  # run_vitis off
            # provenance without leakage: no absolute paths, no full sources
            self.assertNotIn("/", card["project"])

    def test_retrieval_tiers_dedup_and_bound(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw))
            # two IDENTICAL software_equivalence cards (dup), plus csynth and cosim
            for index, stage in enumerate(("csynth", "software_equivalence", "software_equivalence", "cosim")):
                outcome = self._repair_outcome()
                object.__setattr__(outcome, "stage", stage)  # frozen dataclass, test-only
                promote_repair_cards(Path(raw), config, [outcome], top="t", verified=True)
            cards = relevant_cards(config, "numeric_bitwidth", "software_equivalence")
            self.assertEqual(len(cards), 2)
            # content-dedup: the duplicated exact card fills ONE slot, not both
            self.assertEqual(cards[0]["stage"], "software_equivalence")
            self.assertNotEqual(cards[1]["stage"], "software_equivalence")
            # same-stage tier: an unknown family (e.g. the analyst reclassified between
            # runs) still finds the stage-matching card instead of coming back empty
            drifted = relevant_cards(config, "family_the_analyst_invented", "cosim")
            self.assertEqual(len(drifted), 1)
            self.assertEqual(drifted[0]["stage"], "cosim")
            # no family match AND no stage match -> nothing
            self.assertEqual(relevant_cards(config, "no_such_family", "csim_nope"), [])

    def test_mechanical_cards_are_stored_but_never_prompted(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw))
            promote_repair_cards(
                Path(raw), config, [self._repair_outcome(status="applied")], top="t", verified=True
            )
            self.assertEqual(len(load_cards(config)), 1)
            self.assertEqual(relevant_cards(config, "numeric_bitwidth", "software_equivalence"), [])

    def test_disabled_memory_neither_writes_nor_reads(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw))
            config.use_repair_memory = False
            self.assertEqual(
                promote_repair_cards(Path(raw), config, [self._repair_outcome()], top="t", verified=True),
                0,
            )
            config.use_repair_memory = True
            promote_repair_cards(Path(raw), config, [self._repair_outcome()], top="t", verified=True)
            config.use_repair_memory = False
            self.assertEqual(relevant_cards(config, "numeric_bitwidth", "software_equivalence"), [])

    def test_non_utf8_store_degrades_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw))
            path = memory_path(config)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xfe\x00garbage\x80")
            self.assertEqual(load_cards(config), [])
            self.assertEqual(relevant_cards(config, "f", "s"), [])

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw))
            path = memory_path(config)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('not json\n{"kind": "llm", "family": "f", "stage": "s"}\n[1]\n', encoding="utf-8")
            self.assertEqual(len(load_cards(config)), 1)


class ContractPlannerTests(unittest.TestCase):
    def test_valid_proposals_survive_and_invalid_rows_drop(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AgentConfig(top="accum")
            analysis = _write_analysis(tmp, _ACCUM_C, "accum", config)
            llm = ScriptedLLM(
                json.dumps(
                    [
                        {"argument": "in", "length": 4, "direction": "input", "rationale": "loop reads in[0..n)"},
                        {"argument": "n", "range": [0, 4], "rationale": "guards the loop"},
                        {"argument": "ghost", "length": 8},  # unknown arg
                        {"argument": "out", "length": 0},  # non-positive
                        {"argument": "out", "direction": "sideways"},  # bad direction
                        {"argument": "out"},  # nothing proposed
                        "not an object",
                    ]
                )
            )
            proposals, rejected, error = propose_contract(analysis, llm)
            self.assertIsNone(error)
            self.assertEqual([p["argument"] for p in proposals], ["in", "n"])
            # rejections are RECORDED, so zero proposals is distinguishable from
            # "the model proposed nothing"
            self.assertTrue(rejected)
            path = write_proposals(tmp, proposals, "scripted", None, rejected=rejected)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(payload["applied"])
            self.assertEqual(len(payload["proposals"]), 2)
            self.assertEqual(payload["rejected"], rejected)

    def test_symbolic_length_salvages_direction_and_records_rejection(self):
        # Dogfooded for real: haiku proposed length "count" (a string) next to a valid
        # direction. The direction must survive; the length must be rejected WITH a
        # recorded reason pointing at a range proposal instead.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AgentConfig(top="accum")
            analysis = _write_analysis(tmp, _ACCUM_C, "accum", config)
            llm = ScriptedLLM(
                json.dumps([{"argument": "in", "direction": "input", "length": "count"}])
            )
            proposals, rejected, error = propose_contract(analysis, llm)
            self.assertIsNone(error)
            self.assertEqual(proposals, [{"argument": "in", "direction": "input", "rationale": ""}])
            self.assertEqual(len(rejected), 1)
            self.assertIn("range proposal", rejected[0])

    def test_unhashable_model_values_cannot_crash_validation(self):
        from c2hlsc_agent.contract_planner import _validated

        for entry in (
            {"argument": ["in"], "length": 4},
            {"argument": {"name": "in"}, "length": 4},
            {"argument": "in", "direction": ["input"], "length": 4},
        ):
            with self.subTest(entry=str(entry)[:40]):
                proposal, dropped = _validated(entry, {"in"})
                self.assertTrue(proposal is None or "direction" not in proposal)
                self.assertTrue(dropped)

    def test_prose_response_reports_error_not_crash(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AgentConfig(top="accum")
            analysis = _write_analysis(tmp, _ACCUM_C, "accum", config)
            proposals, rejected, error = propose_contract(analysis, ScriptedLLM("The bound should be n."))
            self.assertEqual(proposals, [])
            self.assertIsNotNone(error)


class StimulusAugmentTests(unittest.TestCase):
    def _analysis(self, tmp: Path, config: AgentConfig):
        return _write_analysis(tmp, _ACCUM_C, "accum", config)

    def test_validation_is_all_or_nothing_per_vector(self):
        with tempfile.TemporaryDirectory() as raw:
            analysis = self._analysis(Path(raw), _accum_config())
            accepted, rejections = validate_vectors(
                analysis,
                [
                    {"in": [1, 2, 3, 4], "n": 4},  # good
                    {"in": [1, 2, 3], "n": 4},  # wrong array length
                    {"in": [1, 2, 3, 4], "n": 9},  # out of declared range
                    {"in": [1, 2, "x", 4], "n": 2},  # non-numeric element
                    {"in": [1, 2, 3, 4]},  # scalar missing
                    {"in": [0, 0, 0, 0], "n": 0},  # good boundary
                ],
            )
            self.assertEqual(len(accepted), 2)
            self.assertEqual(len(rejections), 4)
            self.assertEqual(accepted[0], {"in": [1, 2, 3, 4], "n": 4})

    def test_overflow_and_storage_bounds_are_rejected(self):
        # json.loads happily parses 10**400 and NaN/Infinity; none of them may reach a
        # C++ table, and an int-typed arg must reject what its storage cannot hold.
        with tempfile.TemporaryDirectory() as raw:
            analysis = self._analysis(Path(raw), _accum_config())
            accepted, rejections = validate_vectors(
                analysis,
                [
                    {"in": [10**400, 0, 0, 0], "n": 1},   # float-overflow-class int
                    {"in": [2**40, 0, 0, 0], "n": 1},     # beyond int32 storage
                    {"in": [2**31 - 1, -(2**31), 0, 0], "n": 1},  # exact int32 bounds: fine
                ],
            )
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0]["in"][0], 2**31 - 1)
            self.assertEqual(len(rejections), 2)

    def test_cap_is_enforced(self):
        with tempfile.TemporaryDirectory() as raw:
            analysis = self._analysis(Path(raw), _accum_config())
            payload = [{"in": [i, 0, 0, 0], "n": 1} for i in range(12)]
            accepted, rejections = validate_vectors(analysis, payload)
            self.assertEqual(len(accepted), 8)
            self.assertEqual(len(rejections), 4)

    def test_unranged_length_like_scalar_stimulus_is_clamped_and_reported(self):
        # Dogfooded for real: `int count` with no declared range drove the GOLDEN C over
        # int[16] buffers and segfaulted the harness. The stimulus (not the comparison)
        # is clamped to the smallest matching array length, and the contract says so.
        with tempfile.TemporaryDirectory() as raw:
            config = AgentConfig(top="scale", num_tests=4)
            analysis = _write_analysis(
                Path(raw),
                "void scale(const int *src, int *dst, int count, int factor) {\n"
                "    for (int i = 0; i < count; i++) dst[i] = src[i] * factor;\n}\n",
                "scale",
                config,
            )
            tb = generate_testbench(analysis, config)
            self.assertIn("int count = bounded_scalar<int>(test_idx, rng, 0LL, 16LL)", tb)
            self.assertIn("stimulus clamped to [0, 16]", tb)
            # `factor` is not length-like: full random stimulus, untouched
            self.assertIn("int factor = random_value<int>(rng)", tb)

    def test_testbench_without_vectors_is_unchanged(self):
        with tempfile.TemporaryDirectory() as raw:
            config = _accum_config()
            analysis = self._analysis(Path(raw), config)
            tb = generate_testbench(analysis, config)
            self.assertNotIn("aug_vec_", tb)
            self.assertNotIn("llm-directed", tb)
            self.assertIn(f"test_idx < {config.num_tests}", tb)

    def test_testbench_embeds_vectors_after_deterministic_tests(self):
        with tempfile.TemporaryDirectory() as raw:
            config = _accum_config()
            config.augmented_vectors = [
                {"in": [1, 2, 3, 4], "n": 4},
                {"in": [-1, -2, -3, -4], "n": 2},
            ]
            analysis = self._analysis(Path(raw), config)
            tb = generate_testbench(analysis, config)
            self.assertIn("aug_vec_in[2][4]", tb)
            self.assertIn("aug_scalar_n[2]", tb)
            self.assertIn(f"test_idx < {config.num_tests + 2}", tb)
            self.assertIn("(2 llm-directed)", tb)
            # outputs stay sentinel-filled for every test: no aug table for `out`
            self.assertNotIn("aug_vec_out", tb)

    @unittest.skipUnless(shutil.which("g++"), "needs a host C++ compiler")
    def test_augmented_testbench_compiles_runs_and_still_catches_bugs(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = _accum_config()
            config.augmented_vectors = [{"in": [7, -3, 2, 100], "n": 3}]
            analysis = _write_analysis(tmp, _ACCUM_C, "accum", config)
            # a correct HLS copy passes all tests including the augmented one
            (tmp / "src").mkdir()
            (tmp / "src" / "hls_top.hpp").write_text(
                "#pragma once\nvoid accum(const int *in, int *out, int n);\n", encoding="utf-8"
            )
            (tmp / "src" / "hls_top.cpp").write_text(
                '#include "hls_top.hpp"\n' + _ACCUM_C, encoding="utf-8"
            )
            (tmp / "tb").mkdir()
            (tmp / "tb" / "testbench.cpp").write_text(
                generate_testbench(analysis, config), encoding="utf-8"
            )

            def run_tb() -> subprocess.CompletedProcess:
                exe = tmp / "tb_exe"
                build = subprocess.run(
                    ["g++", "-std=c++17", "-I", "src", "tb/testbench.cpp", "src/hls_top.cpp", "-o", str(exe)],
                    cwd=tmp, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(build.returncode, 0, build.stderr)
                return subprocess.run([str(exe)], cwd=tmp, capture_output=True, text=True, timeout=60)

            good = run_tb()
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
            self.assertIn("(1 llm-directed)", good.stdout)

            # sabotage the HLS copy so ONLY large inputs break: the augmented vector
            # (with 100 in the active range) must catch it
            (tmp / "src" / "hls_top.cpp").write_text(
                '#include "hls_top.hpp"\n'
                "void accum(const int *in, int *out, int n) {\n"
                "    int s = 0;\n"
                "    for (int i = 0; i < n; i++) { s += in[i]; out[i] = (s > 90) ? s + 1 : s; }\n"
                "}\n",
                encoding="utf-8",
            )
            bad = run_tb()
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("Mismatch", bad.stdout + bad.stderr)


if __name__ == "__main__":
    unittest.main()
