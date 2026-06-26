from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig, ArgumentConfig
from c2hlsc_agent.convert import generate_hls_sources
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.hlsc_repair_agent import load_repair_audit, repair_project
from c2hlsc_agent.hls_project import write_project


class HlscRepairAgentTests(unittest.TestCase):
    def _write_project(self, code: str, top: str, cfg: AgentConfig | None = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        input_path = root / "input.c"
        input_path.write_text(code, encoding="utf-8")
        cfg = cfg or AgentConfig(top=top)
        cfg.input_files = [input_path]
        cfg.top = top
        analysis = analyze_source(input_path, top, cfg)
        generated = generate_hls_sources(analysis, cfg)
        project = write_project(root / "project", analysis, generated, cfg)
        return project, analysis, cfg

    def test_adds_missing_standard_include_and_records_audit(self):
        project, analysis, cfg = self._write_project(
            """
            #include <stddef.h>
            size_t bump(size_t n) {
              return n + 1;
            }
            """,
            "bump",
        )
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "fail", stderr="error: 'size_t' has not been declared"))

        outcome = repair_project(project.root, analysis, cfg, state, iteration=1)

        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.status, "applied")
        self.assertIn("#include <stddef.h>", (project.root / "src" / "hls_top.hpp").read_text(encoding="utf-8"))
        self.assertEqual(outcome.target_files, ("src/hls_top.hpp",))
        [audit] = load_repair_audit(project.root)
        self.assertEqual(audit.status, "applied")
        self.assertIn("stddef", audit.changes[0].diff)

    def test_includes_original_source_when_helper_definition_is_missing(self):
        project, analysis, cfg = self._write_project(
            """
            static int helper(int x) {
              return x + 1;
            }

            int use_helper(int x) {
              return helper(x);
            }
            """,
            "use_helper",
        )
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "fail", stderr="error: 'helper' was not declared in this scope"))

        outcome = repair_project(project.root, analysis, cfg, state, iteration=1)

        self.assertTrue(outcome.changed)
        source = (project.root / "src" / "hls_top.cpp").read_text(encoding="utf-8")
        self.assertIn("C2HLSC_REPAIR_INCLUDE_ORIGINAL_SUPPORT", source)
        self.assertIn("#define use_helper use_helper_c2hlsc_repair_reference", source)
        self.assertEqual(outcome.target_files, ("src/hls_top.cpp",))

    def test_removes_generated_interface_pragmas_after_interface_failure(self):
        cfg = AgentConfig(
            top="vector_add",
            run_vitis=True,
            interface_mode="m_axi",
            arguments={
                "a": ArgumentConfig(direction="input", length=4),
                "out": ArgumentConfig(direction="output", length=4),
                "n": ArgumentConfig(range=(0, 4)),
            },
        )
        project, analysis, cfg = self._write_project(
            """
            #include <stdint.h>
            void vector_add(const int32_t *a, int32_t *out, int n) {
              for (int i = 0; i < n; ++i) out[i] = a[i] + 1;
            }
            """,
            "vector_add",
            cfg,
        )
        self.assertIn("#pragma HLS INTERFACE", (project.root / "src" / "hls_top.cpp").read_text(encoding="utf-8"))
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "pass"))
        state.add_phase(PhaseResult("csim", "pass"))
        state.add_phase(PhaseResult("csynth", "fail", stderr="ERROR: invalid interface port bundle"))

        outcome = repair_project(project.root, analysis, cfg, state, iteration=1)

        self.assertTrue(outcome.changed)
        source = (project.root / "src" / "hls_top.cpp").read_text(encoding="utf-8")
        self.assertNotIn("#pragma HLS INTERFACE", source)
        self.assertIn("removed generated INTERFACE pragmas", source)
        audit_json = json.loads((project.root / "repair_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit_json[0]["stage"], "csynth")
        self.assertEqual(audit_json[0]["family"], "interface_contract")


if __name__ == "__main__":
    unittest.main()
