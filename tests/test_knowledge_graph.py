import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.config import AgentConfig
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.knowledge_graph import (
    FILENAME,
    SCHEMA,
    refresh_knowledge_graph,
    write_knowledge_graph,
)


class KnowledgeGraphTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "demo_project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "tb").mkdir()
        (self.project / "src" / "hls_top.cpp").write_text("void kernel() {}\n", encoding="utf-8")
        (self.project / "tb" / "klee_driver.cpp").write_text("// harness\n", encoding="utf-8")
        self.analysis = SimpleNamespace(
            function=SimpleNamespace(
                name="kernel",
                return_type="int",
                signature="int kernel(const int *in, int *out)",
                body="out[0] = in[0];",
                definition="int kernel(const int *in, int *out) { out[0] = in[0]; }",
                args=[
                    SimpleNamespace(
                        name="in",
                        raw="const int *in",
                        c_type="const int",
                        pointer_depth=1,
                        is_const=True,
                        direction="input",
                        length=8,
                        scalar_range=(-4, 4),
                        interface="m_axi",
                    ),
                    SimpleNamespace(
                        name="out",
                        raw="int *out",
                        c_type="int",
                        pointer_depth=1,
                        is_const=False,
                        direction="output",
                        length=8,
                        scalar_range=None,
                        interface=None,
                    ),
                ],
            )
        )
        self.config = AgentConfig(top="kernel", num_tests=32, seed=7)

    def tearDown(self):
        self.temporary.cleanup()

    def _read(self):
        return json.loads((self.project / FILENAME).read_text(encoding="utf-8"))

    def test_core_structure_models_design_contract_generated_files_and_shift_left_phases(self):
        path = write_knowledge_graph(self.project, self.analysis, self.config)

        self.assertEqual(path, self.project / FILENAME)
        graph = self._read()
        self.assertEqual(graph["schema"], SCHEMA)
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["function:kernel"]["properties"]["return_type"], "int")
        self.assertEqual(nodes["argument:in"]["properties"]["direction"], "input")
        self.assertEqual(nodes["argument:in"]["properties"]["scalar_range"], [-4, 4])
        self.assertIn("artifact:src/hls_top.cpp", nodes)
        self.assertIn("artifact:tb/klee_driver.cpp", nodes)
        self.assertEqual(nodes["phase:symbolic_klee"]["kind"], "verification_phase")
        self.assertEqual(nodes["phase:coverage_gcov"]["kind"], "verification_phase")
        edge_types = {(edge["source"], edge["type"], edge["target"]) for edge in graph["edges"]}
        self.assertIn(("design:project", "HAS_TOP", "function:kernel"), edge_types)
        self.assertIn(("function:kernel", "HAS_ARGUMENT", "argument:out"), edge_types)
        node_ids = set(nodes)
        self.assertTrue(
            all(edge["source"] in node_ids and edge["target"] in node_ids for edge in graph["edges"])
        )

    def test_phase_statuses_are_normalised_and_unreported_phases_are_explicitly_skipped(self):
        state = VerificationState()
        state.add_phase(
            PhaseResult(
                "software_equivalence",
                "pass",
                metadata={"evidence_origin": "operator_assumption"},
            )
        )
        state.add_phase(PhaseResult("csim", "fail"))
        state.add_phase(PhaseResult("csynth", "blocked"))
        state.add_phase(PhaseResult("cosim", "skipped"))

        write_knowledge_graph(self.project, self.analysis, self.config, state=state)

        nodes = {node["id"]: node for node in self._read()["nodes"]}
        self.assertEqual(nodes["phase:software_equivalence"]["properties"]["status"], "PASS")
        self.assertEqual(
            nodes["phase:software_equivalence"]["properties"]["evidence_origin"],
            "operator_assumption",
        )
        self.assertEqual(nodes["phase:csim"]["properties"]["status"], "FAIL")
        self.assertEqual(nodes["phase:csynth"]["properties"]["status"], "BLOCKED")
        self.assertEqual(nodes["phase:cosim"]["properties"]["status"], "SKIP")
        self.assertEqual(nodes["phase:symbolic_klee"]["properties"]["status"], "SKIP")
        self.assertEqual(
            nodes["phase:symbolic_klee"]["properties"]["requested_scope"],
            "golden_hlsc_relational",
        )
        self.assertNotIn("scope", nodes["phase:symbolic_klee"]["properties"])

    def test_relational_klee_verdict_is_structured_without_raw_counterexample_data(self):
        secret = "PRIVATE_KTEST_PAYLOAD_481209"
        state = VerificationState()
        state.add_phase(
            PhaseResult(
                "symbolic_klee",
                "fail",
                metadata={
                    "schema": "c2hlsc-klee-report-v1",
                    "scope": "golden_hlsc_relational",
                    "outcome": "counterexample",
                    "failure_kind": "relational_counterexample",
                    "completed_paths": 4,
                    "generated_tests": 2,
                    "timed_out": False,
                    "bounded_lengths": {"in": 8, "out": 8},
                    "scalar_ranges": {"n": [0, 8]},
                    "assumptions": {
                        "pointer_alias_model": "distinct_pointer_arguments",
                        "hidden_state_model": "no_mutable_hidden_state",
                        "comparison": "return_and_complete_pointer_post_state",
                    },
                    "counterexample_names": [
                        "C2HLSC_RELATIONAL_MISMATCH:out",
                        "C2HLSC_RELATIONAL_MISMATCH:return",
                    ],
                    "counterexamples": [{"raw": secret}],
                    "commands": [secret],
                },
            )
        )

        write_knowledge_graph(self.project, self.analysis, self.config, state=state)

        node = {
            item["id"]: item for item in self._read()["nodes"]
        }["phase:symbolic_klee"]
        properties = node["properties"]
        self.assertEqual(properties["status"], "FAIL")
        self.assertEqual(properties["scope"], "golden_hlsc_relational")
        self.assertEqual(properties["outcome"], "counterexample")
        self.assertEqual(properties["counterexample_count"], 2)
        self.assertEqual(properties["bounded_lengths"], {"in": 8, "out": 8})
        self.assertEqual(properties["scalar_ranges"], {"n": [0, 8]})
        self.assertEqual(
            properties["assumptions"]["hidden_state_model"],
            "no_mutable_hidden_state",
        )
        self.assertEqual(
            properties["counterexample_names"],
            [
                "C2HLSC_RELATIONAL_MISMATCH:out",
                "C2HLSC_RELATIONAL_MISMATCH:return",
            ],
        )
        self.assertNotIn(secret, (self.project / FILENAME).read_text(encoding="utf-8"))

    def test_repairs_and_evidence_are_linked_without_embedding_evidence(self):
        log = self.project / "csynth.log"
        log.write_text("synthesis details stay in this file", encoding="utf-8")
        state = VerificationState()
        state.add_phase(PhaseResult("csynth", "fail", log_path=log))
        repair = SimpleNamespace(
            iteration=2,
            stage="csynth",
            family="scheduling",
            owner_agent="hlsc_repair_agent",
            status="applied",
            target_files=("src/hls_top.cpp",),
            changes=(SimpleNamespace(path="src/hls_top.cpp"),),
            summary="private repair prose",
            evidence_excerpt="private log excerpt",
        )

        write_knowledge_graph(
            self.project,
            self.analysis,
            self.config,
            state=state,
            repair_history=(repair,),
        )

        graph = self._read()
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["repair:2:0"]["properties"]["outcome"], "applied")
        self.assertIn("artifact:csynth.log", nodes)
        links = {(edge["source"], edge["type"], edge["target"]) for edge in graph["edges"]}
        self.assertIn(("repair:2:0", "ADDRESSES", "phase:csynth"), links)
        self.assertIn(("repair:2:0", "MODIFIED", "artifact:src/hls_top.cpp"), links)
        self.assertIn(("phase:csynth", "PRODUCED_EVIDENCE", "artifact:csynth.log"), links)
        text = (self.project / FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("private repair prose", text)
        self.assertNotIn("private log excerpt", text)
        self.assertNotIn("synthesis details stay in this file", text)

    def test_output_is_byte_deterministic(self):
        write_knowledge_graph(self.project, self.analysis, self.config)
        first = (self.project / FILENAME).read_bytes()
        write_knowledge_graph(self.project, self.analysis, self.config)
        second = (self.project / FILENAME).read_bytes()
        self.assertEqual(first, second)

    def test_refresh_adds_late_reports_without_analysis_or_source_reads(self):
        write_knowledge_graph(self.project, self.analysis, self.config)
        (self.project / "conversion_report.json").write_text('{"status":"pass"}\n', encoding="utf-8")
        coverage = self.project / "coverage"
        coverage.mkdir()
        (coverage / "klee_report.json").write_text('{"status":"skipped"}\n', encoding="utf-8")

        refresh_knowledge_graph(self.project)

        graph = self._read()
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["artifact:conversion_report.json"]["kind"], "report_artifact")
        self.assertEqual(nodes["artifact:coverage/klee_report.json"]["kind"], "evidence_artifact")
        links = {(edge["source"], edge["type"], edge["target"]) for edge in graph["edges"]}
        self.assertIn(
            ("phase:symbolic_klee", "PRODUCED_EVIDENCE", "artifact:coverage/klee_report.json"),
            links,
        )

    def test_golden_source_and_runtime_evidence_do_not_leak_into_graph(self):
        secret = "GOLDEN_BODY_CANARY_617290"
        self.analysis.function.body = f"/* {secret} */ return 0;"
        self.analysis.function.definition = f"int kernel() {{ /* {secret} */ return 0; }}"
        self.analysis.function.args[0].raw = f"const int *in /* {secret} */"
        self.config.nl_spec = secret
        state = VerificationState()
        state.add_phase(PhaseResult("software_equivalence", "fail", stdout=secret, summary=secret))

        write_knowledge_graph(self.project, self.analysis, self.config, state=state)

        self.assertNotIn(secret, (self.project / FILENAME).read_text(encoding="utf-8"))

    def test_analyzed_type_comments_do_not_leak_into_graph(self):
        secret = "KG_SOURCE_CANARY_941027"
        source = self.project / "input.c"
        source.write_text(
            f"int kernel(int /*{secret}*/ value) {{ return value; }}\n",
            encoding="utf-8",
        )
        analysis = analyze_source(source, "kernel", AgentConfig(top="kernel"))

        write_knowledge_graph(self.project, analysis, AgentConfig(top="kernel"))

        graph_text = (self.project / FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(secret, graph_text)
        nodes = {node["id"]: node for node in self._read()["nodes"]}
        self.assertEqual(nodes["argument:value"]["properties"]["c_type"], "int")

    def test_refresh_updates_late_phase_status_with_its_evidence(self):
        state = VerificationState()
        state.add_phase(PhaseResult("ppa", "skipped"))
        write_knowledge_graph(self.project, self.analysis, self.config, state=state)
        (self.project / "ppa_report.json").write_text('{"status":"pass"}\n', encoding="utf-8")

        refresh_knowledge_graph(self.project, phase_updates={"ppa": "pass"})

        nodes = {node["id"]: node for node in self._read()["nodes"]}
        self.assertEqual(nodes["phase:ppa"]["properties"]["status"], "PASS")
        links = {(edge["source"], edge["type"], edge["target"]) for edge in self._read()["edges"]}
        self.assertIn(("phase:ppa", "PRODUCED_EVIDENCE", "artifact:ppa_report.json"), links)

    def test_refresh_updates_relational_klee_status_and_metadata_together(self):
        write_knowledge_graph(self.project, self.analysis, self.config)

        refresh_knowledge_graph(
            self.project,
            phase_updates={
                "symbolic_klee": {
                    "status": "pass",
                    "metadata": {
                        "schema": "c2hlsc-klee-report-v1",
                        "scope": "golden_hlsc_relational",
                        "outcome": "no_counterexample",
                        "completed_paths": 7,
                        "generated_tests": 3,
                        "counterexample_names": [],
                    },
                }
            },
        )

        node = {
            item["id"]: item for item in self._read()["nodes"]
        }["phase:symbolic_klee"]
        self.assertEqual(node["properties"]["status"], "PASS")
        self.assertEqual(node["properties"]["outcome"], "no_counterexample")
        self.assertEqual(node["properties"]["completed_paths"], 7)
        self.assertEqual(node["properties"]["counterexample_count"], 0)


if __name__ == "__main__":
    unittest.main()
