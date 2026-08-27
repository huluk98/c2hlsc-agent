#!/usr/bin/env python3
"""Dogfood: failure_analyst + LLM repair + audit_memory, against the REAL claude CLI.

Run from anywhere: python3 scripts/dogfood_live_agents.py
Needs g++, make, and a logged-in `claude` CLI; makes ~6-10 real haiku calls (a few minutes).
Evidence from past runs: docs/agent_dogfood_evidence.md.

Round 1: deterministic project, sabotaged design, repair loop with the analyst on.
Round 2: fresh project, same sabotage; the promoted card from round 1 must appear in
round 2's repair prompt. Exit 0 iff every assertion holds; prints a JSON verdict.
"""
import json, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from c2hlsc_agent.analyze import analyze_source
from c2hlsc_agent.audit_memory import load_cards, promote_repair_cards, relevant_cards
from c2hlsc_agent.config import load_config
from c2hlsc_agent.convert import generate_hls_sources
from c2hlsc_agent.hls_project import write_project
from c2hlsc_agent.hls_runner import run_software_equivalence, verify_project
from c2hlsc_agent.hlsc_repair_agent import repair_project
from c2hlsc_agent.llm import ClaudeCLIClient
from c2hlsc_agent.report import final_status

HERE = Path(__file__).resolve().parents[1] / "examples" / "agent_dogfood"
SABOTAGE = (
    '#include "hls_top.hpp"\n'
    "void accum(const int *in, int *out, int n) {\n"
    "    int s = 0;\n"
    "    for (int i = 0; i < n; i++) { s += in[i]; out[i] = s + (i == 2 ? 1 : 0); }\n"
    "}\n"
)

class Capture:
    def __init__(self, inner):
        self.inner, self.prompts, self.model = inner, [], inner.model
    def complete(self, system, user, **kw):
        self.prompts.append((system, user))
        return self.inner.complete(system, user, **kw)

def build_and_sabotage(out: Path, config):
    analysis = analyze_source(config.input_files[0], config.top, config)
    generated = generate_hls_sources(analysis, config)
    write_project(out, analysis, generated, config)
    (out / "src" / "hls_top.cpp").write_text(SABOTAGE, encoding="utf-8")
    return analysis

def repair_round(out: Path, config, analysis, llm):
    for iteration in range(1, 4):
        state = verify_project(out, False)
        if final_status(state, False, False) == "pass":
            return True, iteration - 1
        outcome = repair_project(out, analysis, config, state, iteration, llm=llm)
        if not outcome.changed:
            return False, iteration
    state = verify_project(out, False)
    return final_status(state, False, False) == "pass", 3

def main():
    verdict = {"scenario": "analyst+repair+memory", "checks": {}}
    ok = True
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        config = load_config(HERE / "accum.yaml")
        config.use_llm = True
        config.memory_dir = str(tmp / "memory")

        # ---- round 1: repair with analyst, then promote ----
        out1 = tmp / "p1"
        analysis = build_and_sabotage(out1, config)
        llm1 = Capture(ClaudeCLIClient(model="haiku"))
        passed, repairs_used = repair_round(out1, config, analysis, llm1)
        verdict["checks"]["round1_repaired_to_pass"] = passed
        ok &= passed
        analyst_prompts = [u for s, u in llm1.prompts if "Preliminary regex classification" in u]
        repair_prompts1 = [u for s, u in llm1.prompts if "Current `src/hls_top.cpp` to repair" in u]
        verdict["checks"]["analyst_was_consulted"] = bool(analyst_prompts)
        verdict["checks"]["repair_prompt_sent"] = bool(repair_prompts1)
        ok &= bool(analyst_prompts) and bool(repair_prompts1)
        verdict["checks"]["round1_memory_empty_in_prompt"] = all(
            "Audited repairs that fixed similar failures" not in u for u in repair_prompts1
        )
        ok &= verdict["checks"]["round1_memory_empty_in_prompt"]

        from c2hlsc_agent.hlsc_repair_agent import load_repair_audit
        promoted = promote_repair_cards(
            out1, config, load_repair_audit(out1), top="accum", verified=passed,
            model="haiku",
        )
        verdict["checks"]["cards_promoted"] = promoted >= 1
        ok &= promoted >= 1
        verdict["cards"] = [
            {k: card[k] for k in ("family", "stage", "kind")} for card in load_cards(config)
        ]

        # ---- round 2: fresh project, same failure; memory must reach the prompt ----
        out2 = tmp / "p2"
        analysis2 = build_and_sabotage(out2, config)
        llm2 = Capture(ClaudeCLIClient(model="haiku"))
        passed2, _ = repair_round(out2, config, analysis2, llm2)
        repair_prompts2 = [u for s, u in llm2.prompts if "Current `src/hls_top.cpp` to repair" in u]
        used_memory = any("Audited repairs that fixed similar failures" in u for u in repair_prompts2)
        verdict["checks"]["round2_repaired_to_pass"] = passed2
        verdict["checks"]["round2_prompt_carried_memory_card"] = used_memory
        ok &= passed2 and used_memory

    verdict["passed"] = ok
    print(json.dumps(verdict, indent=2))
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
