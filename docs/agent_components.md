# Agent components

Generated from `c2hlsc_agent.components`. Regenerate with:

```text
python -m c2hlsc_agent components --markdown > docs/agent_components.md
```

Each component binds one declared agent from `agent_loop.multi_agent_procedures()`
to the code that implements it today. `status` is `deterministic` when no model is
involved, and `llm_optional` when a model may propose and the deterministic path is
the floor and the fallback.

## Stages

| # | Stage | Purpose | Components |
| --- | --- | --- | --- |
| 1 | `plan` | Fix the must-preserve contract before any code is generated. | `contract_planner` |
| 2 | `generate` | Propose a synthesizable HLS-C translation unit (deterministic baseline, optional model candidate). | `hlsc_generator_agent` |
| 3 | `emit` | Materialize the project: sources, every testbench tier, TCLs, Makefile; refine the stimulus against coverage. | `shift_left_testbench_agent` |
| 4 | `verify` | Run the short-circuiting equivalence ladder; this is the only acceptance oracle. | `cosim_operator` |
| 5 | `triage` | Turn the earliest failure into a routed, owner-tagged repair intent. | `failure_analyst` |
| 6 | `repair` | Apply a minimal auditable patch, then rerun the ladder from the beginning. | `hlsc_repair_agent` |
| 7 | `record` | Persist reports, the repair audit, and the bounded-run ledger snapshot. | `audit_memory_agent` |
| 8 | `optimize` | Post-equivalence PPA work; gated on a full ladder pass and re-verified before acceptance. | `rtl_optimizer_agent` |

## Components at a glance

| Component | Stage | Status | Driven by | Gate |
| --- | --- | --- | --- | --- |
| `contract_planner` | `plan` | `deterministic` | `convert`, `repair`, `optimize` | error-severity diagnostics stop the run before verification unless --keep-going; missing pointer bounds default to length 16 with a warning |
| `hlsc_generator_agent` | `generate` | `llm_optional` | `convert` | extract_hls_source + is_plausible_translation_unit must accept the model's block (balanced braces, defines the top); otherwise the conservative copy is used |
| `shift_left_testbench_agent` | `emit` | `deterministic` | `convert`, `refine` | the oracle testbench must compile and drive golden C and HLS-C with identical stimuli; refinement stops when the coverage target is met, two rounds fail to improve it, or the round/vector budget is spent |
| `cosim_operator` | `verify` | `deterministic` | `convert`, `optimize` | software_equivalence -> trace_consistency -> csim -> csynth -> cosim, short-circuited: the first non-pass phase blocks the rest; a CoSim log failure marker downgrades a zero exit code to fail |
| `failure_analyst` | `triage` | `deterministic` | `convert`, `repair` | Routing only; it never edits the project. Its verdict decides which component runs next. |
| `hlsc_repair_agent` | `repair` | `llm_optional` | `convert`, `repair` | requires --auto-repair on convert; a repair that reproduces a previously seen project signature stops the loop (oscillation guard), and a no-change repair ends it |
| `audit_memory_agent` | `record` | `deterministic` | `convert`, `status` | Always runs, including on failure: a report that hides a failed phase is a bug. |
| `rtl_optimizer_agent` | `optimize` | `llm_optional` | `optimize` | runs only on a project that passed the ladder; a promoted winner must pass host equivalence, CSim, CSynth and CoSim again or the pre-QoR source is restored and the stale report deleted |

## `contract_planner`

- **Role:** Planner
- **Stage:** `plan` — Fix the must-preserve contract before any code is generated.
- **Status:** `deterministic`
- **Owns:** Extract the top function, interface contract, legal input domain, Vitis part/clock, and unsupported C constructs.
- **Inputs:** original C/C++, user config, top-function name
- **Outputs:** must-preserve contract, argument metadata, static diagnostics
- **Implemented by:** `c2hlsc_agent.analyze.analyze_source`, `c2hlsc_agent.analyze._infer_pointer_directions`, `c2hlsc_agent.analyze._unsupported`, `c2hlsc_agent.config.load_config`, `c2hlsc_agent.config.merge_cli_config`
- **Driven by CLI:** `convert`, `repair`, `optimize`
- **Reads:** `input.c`
- **Writes:** _none_
- **Budgets:** _none_
- **Gate:** error-severity diagnostics stop the run before verification unless --keep-going; missing pointer bounds default to length 16 with a warning
- **Stop condition:** All pointer bounds, scalar ranges, directions, and top-level contracts are explicit or conservatively defaulted.
- **LLM seam:** Replace or augment the regex direction/bound inference with a model pass that emits the same ArgumentConfig shape; the verifier still gates whatever it proposes.
- **Invariants:**
  - The analysed C file is the golden oracle; it is never rewritten by any component.
  - An unbounded pointer parameter must surface a diagnostic, never a silent guess.

## `hlsc_generator_agent`

- **Role:** C-to-HLS-C generator
- **Stage:** `generate` — Propose a synthesizable HLS-C translation unit (deterministic baseline, optional model candidate).
- **Status:** `llm_optional`
- **Owns:** Emit synthesizable HLS-C while preserving functional behavior and the external contract; follow hlsc_generator_vitis_beginner_v1 for beginner-facing generation and keep testbench generation separate.
- **Inputs:** original C/C++, static diagnostics, must-preserve contract, testbench expectations
- **Outputs:** hls_top.hpp, hls_top.cpp, beginner-facing HLS analysis, transformation ledger, interface pragma ledger
- **Implemented by:** `c2hlsc_agent.convert.generate_hls_sources`, `c2hlsc_agent.convert._generate_conservative_sources`, `c2hlsc_agent.convert.generate_hls_source_candidates`, `c2hlsc_agent.candidates.select_best_candidate`, `c2hlsc_agent.llm.build_generator_user_prompt`, `c2hlsc_agent.llm.extract_hls_source`, `c2hlsc_agent.hlsc_generator.HLSC_GENERATOR_SYSTEM_PROMPT`
- **Driven by CLI:** `convert`
- **Reads:** `input.c`
- **Writes:** `.candidates/cand_*/`, `candidate_scores.json`
- **Budgets:** `llm_calls`
- **Gate:** extract_hls_source + is_plausible_translation_unit must accept the model's block (balanced braces, defines the top); otherwise the conservative copy is used
- **Stop condition:** Candidate HLS-C is host-compilable and contains only justified, equivalence-preserving transformations.
- **LLM seam:** Already live: swap the prompt/policy in hlsc_generator, or the client in llm.build_llm_client. Best-of-N scoring lives in candidates.select_best_candidate and uses local host equivalence only.
- **Invariants:**
  - The original C is never given to the model as a reference implementation to copy.
  - Model output is a proposal only; acceptance comes from the verifier, never from the model.
  - Every fallback reason is recorded in the transformation ledger, never swallowed.

## `shift_left_testbench_agent`

- **Role:** Testbench and coverage agent
- **Stage:** `emit` — Materialize the project: sources, every testbench tier, TCLs, Makefile; refine the stimulus against coverage.
- **Status:** `deterministic`
- **Owns:** Build a golden-C oracle harness and high-coverage stimuli before synthesis; follow hls_leveri_shift_left_v1 for paired trace generation and dual-tier consistency checks.
- **Inputs:** original C/C++, must-preserve contract, argument metadata
- **Outputs:** host testbench, paired golden/HLS trace testbenches, standalone RTL self-checking testbench, directed/random stimuli, gcov/KLEE coverage artifacts, coverage plan, input/output trace schema
- **Implemented by:** `c2hlsc_agent.hls_project.write_project`, `c2hlsc_agent.testgen.generate_testbench`, `c2hlsc_agent.leveri_testgen.generate_leveri_testbenches`, `c2hlsc_agent.verilog_testgen.generate_verilog_testbenches`, `c2hlsc_agent.stimulus.render_helpers`, `c2hlsc_agent.coverage_refine.refine_project`, `c2hlsc_agent.hls_project.render_makefile`, `c2hlsc_agent.hls_project.render_host_build`, `c2hlsc_agent.hls_project.render_run_csim`, `c2hlsc_agent.hls_project.render_run_csynth`, `c2hlsc_agent.hls_project.render_run_cosim`
- **Driven by CLI:** `convert`, `refine`
- **Reads:** `input.c`, `coverage/gcov_report.json`, `coverage/klee-out/*.ktest`
- **Writes:** `src/hls_top.hpp`, `src/hls_top.cpp`, `tb/testbench.cpp`, `tb/leveri_golden_tb.cpp`, `tb/leveri_hls_tb.cpp`, `tb/leveri_compare.py`, `tb/run_gcov.py`, `tb/klee_driver.cpp`, `tb/run_klee.py`, `tb/leveri_manifest.json`, `tb/stimulus_contract.json`, `tb/rtl_vectors_tb.cpp`, `tb/gen_rtl_tb.py`, `tb/run_rtl_sim.py`, `tb/rtl_tb_manifest.json`, `tb/host_build.py`, `coverage_refinement.json`, `run_hls.tcl`, `run_csim.tcl`, `run_csynth.tcl`, `run_cosim.tcl`, `Makefile`, `run_all.sh`, `run_all.py`
- **Budgets:** _none_
- **Gate:** the oracle testbench must compile and drive golden C and HLS-C with identical stimuli; refinement stops when the coverage target is met, two rounds fail to improve it, or the round/vector budget is spent
- **Stop condition:** Host testbench compiles, feeds identical inputs to golden C and HLS-C, and reaches the configured coverage target.
- **LLM seam:** Coverage-driven refinement is live: KLEE counterexamples become permanent directed cases. The next increment is model-proposed directed stimuli on top of that; keep the deterministic harness as the floor so a model can never weaken the oracle.
- **Invariants:**
  - The golden side calls the ORIGINAL C, macro-renamed to <top>_ref; it is never the generated code.
  - Stimulus is seeded (mt19937_64) so a mismatch is reproducible from the report.
  - Both paired harnesses run ONE schedule; the static tier proves that rather than assuming it.
  - Refinement only ADDS test cases: it never rewrites src/hls_top.cpp, so a repaired or optimized design survives a refinement round untouched.
  - No repair component may ever rewrite a testbench file from model output.

## `cosim_operator`

- **Role:** Vitis operator
- **Stage:** `verify` — Run the short-circuiting equivalence ladder; this is the only acceptance oracle.
- **Status:** `deterministic`
- **Owns:** Run the verifier as the loop controller, short-circuiting on the first failing stage.
- **Inputs:** HLS project, run_hls.tcl, testbench, toolchain settings
- **Outputs:** software equivalence log, CSim log, CSynth log, CoSim log, phase status
- **Implemented by:** `c2hlsc_agent.hls_runner.verify_project`, `c2hlsc_agent.hls_runner.run_software_equivalence`, `c2hlsc_agent.hls_runner.run_trace_consistency`, `c2hlsc_agent.hls_runner.run_vitis`, `c2hlsc_agent.hls_runner._gate_cosim_on_log`, `c2hlsc_agent.cosim_verdict.evaluate_cosim_verdict`, `c2hlsc_agent.equivalence.run_command`, `c2hlsc_agent.remote.RemoteVitis.run_phase`
- **Driven by CLI:** `convert`, `optimize`
- **Reads:** `Makefile`, `run_csim.tcl`, `run_csynth.tcl`, `run_cosim.tcl`, `src/hls_top.cpp`, `tb/testbench.cpp`, `tb/leveri_golden_tb.cpp`, `tb/leveri_hls_tb.cpp`
- **Writes:** `software_equivalence.log`, `trace_consistency.log`, `csim.log`, `csynth.log`, `cosim.log`, `c2hlsc_project/`
- **Budgets:** `vitis_runs`, `wall_seconds`
- **Gate:** software_equivalence -> trace_consistency -> csim -> csynth -> cosim, short-circuited: the first non-pass phase blocks the rest; a CoSim log failure marker downgrades a zero exit code to fail
- **Stop condition:** Compile, CSim, synthesis, and C/RTL CoSim pass, or the earliest failure is classified with compact evidence.
- **LLM seam:** None by design. The operator is the acceptance oracle; keeping it deterministic is what makes every model-proposed change checkable.
- **Invariants:**
  - A skipped or unrequested phase is never reported as pass.
  - The shift-left trace tier runs on every verification, so a paired-trace divergence fails the run instead of sitting in an advisory report nobody reads.
  - Vitis exiting 0 is not sufficient for CoSim: the log verdict is checked too.
  - A remote sync failure is reported as toolchain_unavailable (blocked), never as a code defect.

## `failure_analyst`

- **Role:** Evidence and localization agent
- **Stage:** `triage` — Turn the earliest failure into a routed, owner-tagged repair intent.
- **Status:** `deterministic`
- **Owns:** Classify failures and compress logs into repair evidence without leaking audit-only artifacts.
- **Inputs:** earliest failing stage, truncated logs, local code window, mismatch traces when available
- **Outputs:** failure family, named symbols, repair intent, PMLC evidence for mismatches
- **Implemented by:** `c2hlsc_agent.agent_loop.classify_failure`, `c2hlsc_agent.agent_loop.classify_log_family`, `c2hlsc_agent.hls_runner.earliest_failing_phase`, `c2hlsc_agent.equivalence.parse_mismatches`
- **Driven by CLI:** `convert`, `repair`
- **Reads:** `software_equivalence.log`, `csim.log`, `csynth.log`, `cosim.log`
- **Writes:** _none_
- **Budgets:** _none_
- **Gate:** Routing only; it never edits the project. Its verdict decides which component runs next.
- **Stop condition:** The repair agent receives only the current candidate, minimal evidence, and the must-preserve contract.
- **LLM seam:** The cleanest first live agent: replace the regex triage with a model that returns the same FailureAnalysis dataclass, and add PMLC slicing for CoSim mismatches. Zero risk — the output shape is already validated and the verifier still decides.
- **Invariants:**
  - Full logs stay audit-only; only compact excerpts reach a repair prompt.
  - A blocked family (missing toolchain) must not be escalated into a source repair.

## `hlsc_repair_agent`

- **Role:** Minimal patch agent
- **Stage:** `repair` — Apply a minimal auditable patch, then rerun the ladder from the beginning.
- **Status:** `llm_optional`
- **Owns:** Repair the current HLS-C/testbench candidate using stage-specific evidence.
- **Inputs:** current candidate, failure analysis, must-preserve contract, retrieved repair cards
- **Outputs:** patched candidate, patch rationale, updated transformation ledger
- **Implemented by:** `c2hlsc_agent.hlsc_repair_agent.repair_project`, `c2hlsc_agent.hlsc_repair_agent._repair_missing_standard_includes`, `c2hlsc_agent.hlsc_repair_agent._repair_restrict_for_cpp`, `c2hlsc_agent.hlsc_repair_agent._repair_missing_original_support`, `c2hlsc_agent.hlsc_repair_agent._repair_invalid_interface_pragmas`, `c2hlsc_agent.hlsc_repair_agent._llm_repair`, `c2hlsc_agent.llm.build_repair_prompt`
- **Driven by CLI:** `convert`, `repair`
- **Reads:** `src/hls_top.cpp`, `src/hls_top.hpp`, `repair_audit.json`
- **Writes:** `src/hls_top.cpp`, `src/hls_top.hpp`, `repair_audit.json`
- **Budgets:** `attempts`, `llm_calls`, `wall_seconds`
- **Gate:** requires --auto-repair on convert; a repair that reproduces a previously seen project signature stops the loop (oscillation guard), and a no-change repair ends it
- **Stop condition:** A minimal patch is produced and the full verifier is rerun from the beginning.
- **LLM seam:** Already live. The next increment is evidence localization: feed PMLC slices from failure_analyst instead of a raw log tail, and retrieve audited repair cards from audit_memory_agent.
- **Invariants:**
  - Only src/hls_top.cpp is model-writable. input.c and every tb/ file are off limits.
  - Each change records a before/after sha256 and a unified diff in repair_audit.json.
  - After any patch the verifier reruns from software_equivalence, never from the failing phase.

## `audit_memory_agent`

- **Role:** Evidence memory agent
- **Stage:** `record` — Persist reports, the repair audit, and the bounded-run ledger snapshot.
- **Status:** `deterministic`
- **Owns:** Persist reproducible artifacts and promote only audited repair successes into retrieval memory.
- **Inputs:** logs, reports, patches, failure analyses, human audit decision
- **Outputs:** audit ledger, repair-success cards, retrieval blind-spot notes
- **Implemented by:** `c2hlsc_agent.report.write_reports`, `c2hlsc_agent.report.final_status`, `c2hlsc_agent.hlsc_repair_agent.load_repair_audit`, `c2hlsc_agent.run_control.RunLedger`, `c2hlsc_agent.run_control.RunController.snapshot`
- **Driven by CLI:** `convert`, `status`
- **Reads:** `repair_audit.json`, `run_ledger.jsonl`
- **Writes:** `conversion_report.md`, `conversion_report.json`, `run_ledger.jsonl`
- **Budgets:** _none_
- **Gate:** Always runs, including on failure: a report that hides a failed phase is a bug.
- **Stop condition:** No reference HLS, hidden labels, or manual fixes enter prompt-facing memory.
- **LLM seam:** Promote only audited failure-to-pass chains from repair_audit.json into retrieval memory, keyed by failing stage + failure family + named symbols. Reference HLS and hidden labels must never enter prompt-facing memory.
- **Invariants:**
  - Prompts, model responses, API keys, and endpoints are never written to the ledger.
  - run_control status (running/passed/failed/blocked/exhausted/cancelled) is reported separately from the verification status and neither may be described as the other.

## `rtl_optimizer_agent`

- **Role:** Post-equivalence optimizer
- **Stage:** `optimize` — Post-equivalence PPA work; gated on a full ladder pass and re-verified before acceptance.
- **Status:** `llm_optional`
- **Owns:** Improve PPA only after functional equivalence is locked.
- **Inputs:** four-stage passing HLS-C, Vitis reports, optimization policy
- **Outputs:** pragma candidates, optimized HLS-C, QoR delta report
- **Implemented by:** `c2hlsc_agent.qor_optimizer.optimize_project`, `c2hlsc_agent.qor_optimizer._pipeline_innermost_loops`, `c2hlsc_agent.qor_optimizer._llm_candidate_source`, `c2hlsc_agent.qor.parse_csynth_xml`, `c2hlsc_agent.qor.evaluate_targets`, `c2hlsc_agent.qor.objective_score`, `c2hlsc_agent.local_ppa.run_local_ppa`
- **Driven by CLI:** `optimize`
- **Reads:** `src/hls_top.cpp`, `c2hlsc_project/solution1/syn/report/csynth.xml`
- **Writes:** `qor_report.json`, `qor_report.md`, `qor_table.tex`, `src/hls_top.cpp.pre_qor`, `.qor/cand_*/`
- **Budgets:** `llm_calls`, `vitis_runs`, `wall_seconds`
- **Gate:** runs only on a project that passed the ladder; a promoted winner must pass host equivalence, CSim, CSynth and CoSim again or the pre-QoR source is restored and the stale report deleted
- **Stop condition:** Every optimization candidate reruns host equivalence, CSim, synthesis, and CoSim before acceptance.
- **LLM seam:** Already live for candidate proposal. Extensions: one optimization family per round (pipeline, unroll, array partition, dataflow, interface, bitwidth) and a candidate queue with explicit rollback records.
- **Invariants:**
  - Optimization never becomes its own oracle: acceptance is a full re-verification.
  - No latency/area/timing/power claim without a fresh report from a named tool, part and clock.
  - A toolchain outage is reported as an infrastructure problem, not as a QoR verdict.
