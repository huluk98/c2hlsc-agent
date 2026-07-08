"""Tests for the strong-generator/verifier/repair upgrades:

- claude-cli LLM backend (subscription CLI, no API key)
- NL spec input (C+NL and NL-only golden-reference generation)
- best-of-N candidate generation and local host-equivalence selection
- remote Vitis over SSH (only vitis_hls leaves the machine)
- repair evidence tail-slicing, history-aware prompts, oscillation guard
- timeout PhaseResults keep their log evidence
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from c2hlsc_agent import llm as llm_module
from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.candidates import select_best_candidate
from c2hlsc_agent.cli import build_parser, run_convert
from c2hlsc_agent.config import AgentConfig, merge_cli_config
from c2hlsc_agent.convert import (
    generate_hls_source_candidates,
    generate_hls_sources,
    generate_reference_c,
)
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.hls_project import write_project
from c2hlsc_agent.hls_runner import run_software_equivalence, run_vitis
from c2hlsc_agent.hlsc_repair_agent import load_repair_audit, repair_project
from c2hlsc_agent.llm import (
    ClaudeCLIClient,
    build_llm_client,
    build_repair_prompt,
    resolve_backend,
)
from c2hlsc_agent.remote import RemoteVitis


VECTOR_ADD = """#include <stdint.h>

void vector_add(const int32_t *a, const int32_t *b, int32_t *out, int n) {
  for (int i = 0; i < n; ++i) {
    out[i] = a[i] + b[i];
  }
}
"""


class SeqLLM:
    """FakeLLM that returns one queued response per call and records prompts."""

    def __init__(self, responses: list[str], model: str = "fake-model") -> None:
        self.responses = list(responses)
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> str:
        self.calls.append((system, user))
        if not self.responses:
            raise AssertionError("SeqLLM ran out of queued responses")
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def _analysis(tmp: Path, config: AgentConfig, source_text: str = VECTOR_ADD, top: str = "vector_add"):
    source = tmp / "input.c"
    source.write_text(source_text, encoding="utf-8")
    config.input_files = [source]
    config.top = top
    return analyze_source(source, top, config)


def _cpp_response(body: str) -> str:
    return f"4. Vitis HLS annotated code\n```cpp\n{body}\n```\n"


CANDIDATE_B = """#include "hls_top.hpp"

void vector_add(const int32_t *a, const int32_t *b, int32_t *out, int n) {
#pragma HLS PIPELINE
  for (int i = 0; i < n; ++i) {
    out[i] = a[i] + b[i];
  }
}
"""


class ClaudeCLIBackendTests(unittest.TestCase):
    def test_auto_prefers_claude_cli_when_on_path(self):
        config = AgentConfig(use_llm=True)
        with mock.patch.object(llm_module.shutil, "which", return_value="/usr/local/bin/claude"):
            self.assertEqual(resolve_backend(config), "claude-cli")
            client = build_llm_client(config)
        self.assertIsInstance(client, ClaudeCLIClient)
        self.assertEqual(client.model, "opus")

    def test_auto_skips_cli_when_absent(self):
        config = AgentConfig(use_llm=True, llm_base_url="http://localhost:11434/v1")
        with mock.patch.object(llm_module.shutil, "which", return_value=None):
            self.assertEqual(resolve_backend(config), "openai")

    def test_explicit_base_url_wins_over_cli(self):
        # An explicitly configured endpoint is an explicit choice; the CLI must not hijack it.
        config = AgentConfig(use_llm=True, llm_base_url="http://localhost:11434/v1")
        with mock.patch.object(llm_module.shutil, "which", return_value="/usr/local/bin/claude"):
            self.assertEqual(resolve_backend(config), "openai")

    def test_cli_available_uses_shlex_for_quoted_path(self):
        config = AgentConfig(use_llm=True, llm_cli_cmd='"/opt/my tools/claude" --flag')

        def which(cmd):
            return "/opt/my tools/claude" if cmd == "/opt/my tools/claude" else None

        with mock.patch.object(llm_module.shutil, "which", side_effect=which):
            self.assertTrue(llm_module._cli_available(config))

    def test_openai_without_key_returns_none(self):
        config = AgentConfig(use_llm=True, llm_backend="openai")  # default cloud URL, no key
        with mock.patch.object(llm_module, "_env", return_value=None):
            self.assertIsNone(build_llm_client(config))
            self.assertIsNotNone(llm_module.missing_llm_reason(config))

    def test_cli_client_pipes_prompt_and_returns_stdout(self):
        client = ClaudeCLIClient(model="opus", cli_cmd="claude")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ANSWER", stderr="")
        with mock.patch.object(llm_module.subprocess, "run", return_value=completed) as run:
            result = client.complete("SYS", "USER")
        self.assertEqual(result, "ANSWER")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["claude", "-p", "--model"])
        self.assertEqual(argv[3], "opus")
        self.assertIn("SYS", run.call_args.kwargs["input"])
        self.assertIn("USER", run.call_args.kwargs["input"])

    def test_cli_client_raises_on_nonzero_exit(self):
        client = ClaudeCLIClient()
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with mock.patch.object(llm_module.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError):
                client.complete("SYS", "USER")


class NlSpecTests(unittest.TestCase):
    def test_generator_prompt_includes_nl_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True, nl_spec="prioritize a fully pipelined II=1 loop")
            analysis = _analysis(Path(tmp), config)
            llm = SeqLLM([_cpp_response(CANDIDATE_B)])
            generated = generate_hls_sources(analysis, config, llm=llm)
        self.assertIn("PIPELINE", generated.source)
        _system, user = llm.calls[0]
        self.assertIn("prioritize a fully pipelined II=1 loop", user)
        self.assertIn("Design intent from the user", user)

    def test_generate_reference_c_accepts_matching_top(self):
        response = "```c\n#include <stdint.h>\nint32_t acc_sum(const int32_t *a, int n) {\n  int32_t s = 0;\n  for (int i = 0; i < 8; ++i) s += a[i];\n  return s;\n}\n```"
        llm = SeqLLM([response])
        code = generate_reference_c("sum 8 int32 values", "acc_sum", llm)
        self.assertIsNotNone(code)
        self.assertIn("acc_sum", code)

    def test_generate_reference_c_rejects_wrong_top(self):
        response = "```c\nint other(void) { return 1; }\n```"
        llm = SeqLLM([response])
        self.assertIsNone(generate_reference_c("spec", "acc_sum", llm))

    def test_nl_only_convert_flow(self):
        reference = (
            "```c\n#include <stdint.h>\n"
            "int32_t acc_sum(const int32_t *a, int32_t n) {\n"
            "  int32_t s = 0;\n  for (int i = 0; i < 8; ++i) s += a[i];\n  return s;\n}\n```"
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            args = build_parser().parse_args(
                [
                    "convert",
                    "--spec",
                    "sum the first 8 elements",
                    "--top",
                    "acc_sum",
                    "--out",
                    str(out_dir),
                    "--no-run-vitis",
                ]
            )
            passing = VerificationState()
            passing.add_phase(PhaseResult("software_equivalence", "pass"))
            with mock.patch("c2hlsc_agent.cli.build_llm_client", return_value=SeqLLM([reference])), mock.patch(
                "c2hlsc_agent.cli.verify_project", return_value=passing
            ):
                rc = run_convert(args)
            self.assertEqual(rc, 0)
            self.assertTrue((out_dir / "nl_reference.c").exists())
            self.assertTrue((out_dir / "input.c").exists())
            self.assertIn("acc_sum", (out_dir / "input.c").read_text(encoding="utf-8"))

    def test_nl_only_requires_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = build_parser().parse_args(
                ["convert", "--spec", "spec", "--top", "f", "--out", str(Path(tmp) / "out"), "--no-run-vitis"]
            )
            with mock.patch("c2hlsc_agent.cli.build_llm_client", return_value=None):
                with self.assertRaises(SystemExit):
                    run_convert(args)

    def test_c_plus_nl_auto_enables_llm(self):
        # --spec with --input (C+NL) must auto-enable the LLM path so it is not ignored.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.c"
            src.write_text(VECTOR_ADD, encoding="utf-8")
            out_dir = Path(tmp) / "out"
            args = build_parser().parse_args(
                ["convert", "--input", str(src), "--top", "vector_add", "--spec", "pipeline it",
                 "--out", str(out_dir), "--no-run-vitis"]
            )
            passing = VerificationState()
            passing.add_phase(PhaseResult("software_equivalence", "pass"))
            llm = SeqLLM([_cpp_response(CANDIDATE_B)])
            with mock.patch("c2hlsc_agent.cli.build_llm_client", return_value=llm), mock.patch(
                "c2hlsc_agent.cli.verify_project", return_value=passing
            ):
                rc = run_convert(args)
            self.assertEqual(rc, 0)
            # the LLM generator actually ran (spec reached the prompt)
            self.assertTrue(llm.calls)
            self.assertIn("pipeline it", llm.calls[0][1])

    def test_no_llm_with_spec_stays_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.c"
            src.write_text(VECTOR_ADD, encoding="utf-8")
            out_dir = Path(tmp) / "out"
            args = build_parser().parse_args(
                ["convert", "--input", str(src), "--top", "vector_add", "--spec", "x",
                 "--no-llm", "--out", str(out_dir), "--no-run-vitis"]
            )
            passing = VerificationState()
            passing.add_phase(PhaseResult("software_equivalence", "pass"))
            with mock.patch("c2hlsc_agent.cli.build_llm_client", return_value=None) as build, mock.patch(
                "c2hlsc_agent.cli.verify_project", return_value=passing
            ):
                rc = run_convert(args)
            self.assertEqual(rc, 0)


class CandidateSelectionTests(unittest.TestCase):
    def test_candidates_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True, llm_candidates=3)
            analysis = _analysis(Path(tmp), config)
            llm = SeqLLM([_cpp_response(CANDIDATE_B), _cpp_response(CANDIDATE_B), _cpp_response(CANDIDATE_B)])
            candidates = generate_hls_source_candidates(analysis, config, llm, 3)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(llm.calls), 3)

    def test_selection_prefers_passing_candidate(self):
        variant = CANDIDATE_B.replace("#pragma HLS PIPELINE", "#pragma HLS PIPELINE II=1")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            config = AgentConfig(use_llm=True, llm_candidates=2)
            analysis = _analysis(Path(tmp), config)
            llm = SeqLLM([_cpp_response(CANDIDATE_B), _cpp_response(variant)])
            results = [
                PhaseResult("software_equivalence", "fail", stdout="Mismatch test=0 arg=out index=1 expected=3 actual=4 seed=1"),
                PhaseResult("software_equivalence", "pass"),
            ]
            with mock.patch("c2hlsc_agent.candidates.run_software_equivalence", side_effect=results):
                winner, scores = select_best_candidate(out_dir, analysis, config, llm)
        self.assertIsNotNone(winner)
        self.assertIn("II=1", winner.source)
        self.assertEqual(len(scores), 2)
        self.assertTrue(scores[1].passed)

    def test_selection_prefers_candidate_that_got_further(self):
        # Both candidates run but mismatch (testbench exits at first mismatch → 1 line each);
        # the one whose first failure is at a LATER test index stayed correct longer and wins.
        variant = CANDIDATE_B.replace("#pragma HLS PIPELINE", "#pragma HLS UNROLL")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            config = AgentConfig(use_llm=True, llm_candidates=2)
            analysis = _analysis(Path(tmp), config)
            llm = SeqLLM([_cpp_response(CANDIDATE_B), _cpp_response(variant)])
            results = [
                PhaseResult("software_equivalence", "fail", stdout="Mismatch test=0 arg=out index=1 expected=3 actual=4 seed=1"),
                PhaseResult("software_equivalence", "fail", stdout="Mismatch test=57 arg=out index=1 expected=3 actual=4 seed=1"),
            ]
            with mock.patch("c2hlsc_agent.candidates.run_software_equivalence", side_effect=results):
                winner, scores = select_best_candidate(out_dir, analysis, config, llm)
        self.assertIsNotNone(winner)
        self.assertIn("UNROLL", winner.source)  # candidate 2 failed at test 57, later than test 0
        self.assertEqual(scores[1].first_failure_index, 57)

    def test_selection_prefers_fewest_mismatches_over_compile_failure(self):
        variant = CANDIDATE_B.replace("#pragma HLS PIPELINE", "#pragma HLS UNROLL")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            config = AgentConfig(use_llm=True, llm_candidates=2)
            analysis = _analysis(Path(tmp), config)
            llm = SeqLLM([_cpp_response(CANDIDATE_B), _cpp_response(variant)])
            results = [
                PhaseResult("software_equivalence", "fail", stdout="compiler exploded"),  # no mismatch lines
                PhaseResult("software_equivalence", "fail", stdout="Mismatch test=0 arg=out index=1 expected=3 actual=4 seed=1"),
            ]
            with mock.patch("c2hlsc_agent.candidates.run_software_equivalence", side_effect=results):
                winner, _scores = select_best_candidate(out_dir, analysis, config, llm)
        self.assertIsNotNone(winner)
        self.assertIn("UNROLL", winner.source)


class RemoteVitisTests(unittest.TestCase):
    def test_remote_dir_tilde_is_normalized(self):
        remote = RemoteVitis(host="u@h", remote_dir="~/c2hlsc_runs")
        rdir = remote.remote_project_dir(Path("/x/build/proj"))
        self.assertRegex(rdir, r"^c2hlsc_runs/proj-[0-9a-f]{8}$")
        remote = RemoteVitis(host="u@h", remote_dir="/scratch/runs")
        self.assertRegex(remote.remote_project_dir(Path("/x/build/proj")), r"^/scratch/runs/proj-[0-9a-f]{8}$")

    def test_remote_dir_disambiguates_same_basename(self):
        remote = RemoteVitis(host="u@h")
        a = remote.remote_project_dir(Path("/designs/fir/out"))
        b = remote.remote_project_dir(Path("/designs/aes/out"))
        self.assertNotEqual(a, b)  # same basename 'out' must not collide on the remote

    def test_phase_script_uses_setup_and_timeout(self):
        remote = RemoteVitis(host="u@h", setup="source /tools/x/settings64.sh")
        script = remote.phase_script(Path("/x/build/proj"), "cosim", 600)
        self.assertIn("cd c2hlsc_runs/proj-", script)
        self.assertIn("source /tools/x/settings64.sh && ", script)
        self.assertIn("timeout -k 30s 600s vitis_hls -f run_cosim.tcl", script)

    def test_phase_script_probes_settings_when_no_setup(self):
        remote = RemoteVitis(host="u@h")
        script = remote.phase_script(Path("/x/build/proj"), "csim", 600)
        self.assertIn("command -v vitis_hls", script)
        self.assertIn("settings64.sh", script)
        # a probe miss must emit the marker classify_log_family maps to toolchain_unavailable
        self.assertIn("vitis_hls not found", script)

    def test_from_config_reads_env_fallback(self):
        config = AgentConfig()
        with mock.patch.dict(os.environ, {"C2HLSC_VITIS_SSH": "luke@linux-box"}):
            remote = RemoteVitis.from_config(config)
        self.assertIsNotNone(remote)
        self.assertEqual(remote.host, "luke@linux-box")
        self.assertIsNone(RemoteVitis.from_config(AgentConfig()))

    def test_run_vitis_remote_ladder_pushes_runs_and_pulls(self):
        remote = mock.Mock()
        remote.host = "u@h"
        remote.push.return_value = PhaseResult("vitis_push", "pass")
        remote.run_phase.side_effect = [
            PhaseResult("csim", "pass"),
            PhaseResult("csynth", "pass"),
            PhaseResult("cosim", "pass"),
        ]
        remote.pull.return_value = PhaseResult("vitis_pull", "pass")
        with tempfile.TemporaryDirectory() as tmp:
            phases = run_vitis(Path(tmp), True, remote=remote)
        self.assertEqual({name: p.status for name, p in phases.items()}, {"csim": "pass", "csynth": "pass", "cosim": "pass"})
        remote.push.assert_called_once()
        remote.pull.assert_called_once()

    def test_run_vitis_remote_push_failure_blocks_ladder(self):
        remote = mock.Mock()
        remote.host = "u@h"
        remote.push.return_value = PhaseResult("vitis_push", "fail", stderr="ssh: no route", summary="ssh: no route")
        with tempfile.TemporaryDirectory() as tmp:
            phases = run_vitis(Path(tmp), True, remote=remote)
        self.assertEqual(phases["csim"].status, "fail")
        self.assertIn("u@h", phases["csim"].summary)
        remote.run_phase.assert_not_called()

    def test_remote_infra_failure_classifies_as_blocked_not_repaired(self):
        # A push/transport failure must NOT trigger source mutation in the repair loop.
        from c2hlsc_agent.agent_loop import classify_failure, classify_log_family

        remote = mock.Mock()
        remote.host = "box"
        remote.push.return_value = PhaseResult("vitis_push", "fail", summary="ssh: connect to host box port 22: Connection refused")
        with tempfile.TemporaryDirectory() as tmp:
            phases = run_vitis(Path(tmp), True, remote=remote)
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        for p in phases.values():
            state.add_phase(p)
        # despite the "port 22" text, this is toolchain_unavailable (blocked), not interface_contract
        self.assertEqual(classify_log_family("csim", phases["csim"].summary), "toolchain_unavailable")
        decision = classify_failure(state, True, False)
        self.assertEqual(decision.status, "blocked")

    def test_vitis_ssh_flag_implies_run_vitis(self):
        args = build_parser().parse_args(
            ["convert", "--input", "x.c", "--top", "f", "--out", "o", "--vitis-ssh", "luke@box"]
        )
        config = merge_cli_config(AgentConfig(), args)
        self.assertTrue(config.run_vitis)
        self.assertEqual(config.vitis_ssh_host, "luke@box")

    def test_vitis_ssh_env_var_implies_run_vitis(self):
        args = build_parser().parse_args(["convert", "--input", "x.c", "--top", "f", "--out", "o"])
        with mock.patch.dict(os.environ, {"C2HLSC_VITIS_SSH": "luke@box"}):
            config = merge_cli_config(AgentConfig(), args)
        self.assertEqual(config.vitis_ssh_host, "luke@box")
        self.assertTrue(config.run_vitis)

    def test_no_run_vitis_overrides_env_var(self):
        args = build_parser().parse_args(
            ["convert", "--input", "x.c", "--top", "f", "--out", "o", "--no-run-vitis"]
        )
        with mock.patch.dict(os.environ, {"C2HLSC_VITIS_SSH": "luke@box"}):
            config = merge_cli_config(AgentConfig(), args)
        self.assertFalse(config.run_vitis)

    def test_run_phase_relabels_timeout_and_ssh_failures(self):
        remote = RemoteVitis(host="u@h")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "c2hlsc_agent.remote.run_command",
                return_value=PhaseResult("cosim", "fail", returncode=124, stderr="killed"),
            ):
                timed_out = remote.run_phase(Path(tmp), "cosim", 600)
            self.assertIn("timed out", timed_out.summary)
            with mock.patch(
                "c2hlsc_agent.remote.run_command",
                return_value=PhaseResult("csim", "fail", returncode=255, stderr="ssh: Connection refused"),
            ):
                ssh_failed = remote.run_phase(Path(tmp), "csim", 600)
        self.assertIn("remote vitis unavailable", ssh_failed.summary)

    def test_push_excludes_local_phase_logs_and_pull_leaves_root_logs(self):
        remote = RemoteVitis(host="u@h")
        captured = {}

        def fake_run_command(command, cwd, phase, timeout=120):
            captured[phase] = command
            return PhaseResult(phase, "pass")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("c2hlsc_agent.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")), mock.patch(
                "c2hlsc_agent.remote.run_command", side_effect=fake_run_command
            ):
                remote.push(Path(tmp))
                remote.pull(Path(tmp))
        # push excludes local runner logs so stale copies never reach the remote
        self.assertIn("*.log", captured["vitis_push"])
        # pull only brings back project-internal logs, never the root <phase>.log
        pull_cmd = captured["vitis_pull"]
        self.assertIn("c2hlsc_project/**/*.log", pull_cmd)
        self.assertNotIn("*.log", pull_cmd[: pull_cmd.index("c2hlsc_project/**/*.log")])


class MinimalYamlTests(unittest.TestCase):
    def test_quoted_hash_in_value_survives(self):
        from c2hlsc_agent.config import _minimal_yaml

        data = _minimal_yaml('nl_spec: "count the # of set bits"  # comment\ntop: popcount')
        self.assertEqual(data["nl_spec"], "count the # of set bits")
        self.assertEqual(data["top"], "popcount")

    def test_trailing_comment_still_stripped(self):
        from c2hlsc_agent.config import _minimal_yaml

        self.assertEqual(_minimal_yaml("top: foo  # trailing")["top"], "foo")


class RepairUpgradeTests(unittest.TestCase):
    def test_repair_evidence_is_tail_sliced(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig()
            analysis = _analysis(Path(tmp), config)
            decision = mock.Mock(family="cosim_failure", next_action="fix", repair_scope="dut")
            evidence = "BANNER " * 2000 + "THE_ACTUAL_ERROR"
            _system, user = build_repair_prompt(analysis, decision, "cosim", evidence, "src/hls_top.cpp", "void f() {}")
        self.assertIn("THE_ACTUAL_ERROR", user)
        # 4000-char tail slice: only ~570 of the 2000 leading BANNER tokens can survive
        self.assertLess(user.count("BANNER"), 700)

    def _seeded_project(self, tmp: Path, config: AgentConfig):
        analysis = _analysis(tmp, config)
        out_dir = tmp / "out"
        generated = generate_hls_sources(analysis, config)
        write_project(out_dir, analysis, generated, config)
        return analysis, out_dir

    def _failing_state(self) -> VerificationState:
        state = VerificationState()
        state.add_phase(
            PhaseResult(
                "software_equivalence",
                "fail",
                stdout="Mismatch test=0 arg=out index=1 expected=3 actual=4 seed=1",
            )
        )
        return state

    def test_llm_repair_feeds_history_and_rejects_oscillation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True)
            analysis, out_dir = self._seeded_project(Path(tmp), config)
            original = (out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8")
            state = self._failing_state()

            first = repair_project(out_dir, analysis, config, state, 1, llm=SeqLLM([f"```cpp\n{CANDIDATE_B}```"]))
            self.assertEqual(first.status, "applied_llm")
            self.assertIn("PIPELINE", (out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8"))

            echo_original = SeqLLM([f"```cpp\n{original}```"])
            second = repair_project(out_dir, analysis, config, state, 2, llm=echo_original)
            self.assertEqual(second.status, "oscillation_rejected")
            self.assertFalse(second.changed)
            # the failed strategy is summarized in the prompt for the second attempt
            _system, user = echo_original.calls[0]
            self.assertIn("Previous repair attempts", user)
            self.assertIn("iteration 1", user)
            # file was NOT reverted to the original
            self.assertIn("PIPELINE", (out_dir / "src" / "hls_top.cpp").read_text(encoding="utf-8"))
            audit = load_repair_audit(out_dir)
            self.assertEqual([o.status for o in audit], ["applied_llm", "oscillation_rejected"])

    def test_repair_prompt_includes_nl_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(use_llm=True, nl_spec="keep latency under 100 cycles")
            analysis, out_dir = self._seeded_project(Path(tmp), config)
            llm = SeqLLM([f"```cpp\n{CANDIDATE_B}```"])
            repair_project(out_dir, analysis, config, self._failing_state(), 1, llm=llm)
        _system, user = llm.calls[0]
        self.assertIn("keep latency under 100 cycles", user)


class TimeoutEvidenceTests(unittest.TestCase):
    def test_software_equivalence_timeout_keeps_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            def fake_run_command(command, cwd, phase, timeout=120):
                (cwd / f"{phase}.log").write_text("partial output before hang", encoding="utf-8")
                raise subprocess.TimeoutExpired(command, timeout, output="partial output before hang")

            with mock.patch("c2hlsc_agent.hls_runner.run_command", side_effect=fake_run_command):
                result = run_software_equivalence(project)
        self.assertEqual(result.status, "fail")
        self.assertIn("timed out", result.summary)
        self.assertIsNotNone(result.log_path)
        self.assertIn("partial output", result.stdout)


if __name__ == "__main__":
    unittest.main()
