# Handoff: Evidence-Driven Generation Workflow with LeVeri-Style Testbench Generation

Status: proposed target architecture for the next implementation round.

Basis: the HLSC-agent paper ("Evidence-Driven LLM Agent for C-to-HLS-C Conversion
and Verification", Zhe Zhao) supplies the generation/repair spine — a closed
generation–verification–diagnosis–repair loop governed by a short-circuit Vitis
verifier, with role-specialized agents, a bounded repair budget, and strict
prompt-evidence isolation. On top of that spine we add the HLS-LeVeri-style
paired testbench generation that already exists in this repository
(`c2hlsc_agent/leveri_testgen.py`, policy `hls_leveri_shift_left_v1`).

Audience: whoever picks up the generation-workflow integration next. Everything
below is grounded in the current code at the cited locations; where the paper
and the repo disagree, the difference is stated explicitly.

---

## 0. The ordering decision: do we need the HLS-C before generating testbenches?

**Answer: No for generation, partially for execution.** Testbench *generation*
needs only the original C. Producing the HLS-side *trace output* (and therefore
the paired comparison) needs the first HLS-C candidate to exist.

Verified facts from the code:

- `generate_leveri_testbenches(analysis, config)`
  (`c2hlsc_agent/leveri_testgen.py:686`) consumes only the `AnalysisResult`
  produced by `analyze_source(input.c, top, config)`
  (`c2hlsc_agent/analyze.py:233`) — the interface contract of the **original
  C** (top name, argument types, directions, lengths, scalar ranges) — plus
  `AgentConfig` (`seed`, `num_tests`). It never reads HLS-C content.
- The **golden testbench** compiles the original C directly via
  `#include "../input.c"` with the top macro-renamed to `<top>_ref`
  (`leveri_testgen.py:688-692`). It can therefore be compiled and run
  immediately, before any HLS-C exists, producing `leveri_golden_trace.csv`.
  The KLEE symbolic driver likewise targets only the golden C
  (`leveri_testgen.py:361-402`).
- The **HLS testbench** references the HLS-C only through
  `#include "../src/hls_top.hpp"` and a call to the same top signature
  (`leveri_testgen.py:693, 702-707`). That is a *name-and-signature*
  dependency, not a content dependency: the testbench text is fully
  determined by the original C's contract. But compiling it — and hence
  producing `leveri_hls_trace.csv`, the dual-tier comparison
  (`tb/leveri_compare.py`), and the paired gcov run — requires
  `src/hls_top.{cpp,hpp}` on disk, i.e. the first candidate y(0).

Consequences for the combined workflow:

1. **Testbench generation is a shift-left stage.** Generate both testbenches
   (plus comparator, gcov/KLEE hooks, and manifest) from the contract, in
   parallel with — or before — HLS-C generation. This matches the paper's
   §III-B position: the harness is authored from the original C and its
   specification *before any HLS-C exists*, so it cannot encode an
   HLS-C-specific solution, and the golden reference is the original C
   executed at run time, never a stored expected-output table.
2. **Run the golden side immediately.** Compiling and executing the golden
   testbench before any LLM call is a free smoke test of contract extraction:
   if the golden trace cannot be produced, the contract (directions, lengths,
   ranges) is wrong, and no generation tokens should be spent yet.
3. **Gate the paired comparison on y(0).** The HLS trace, the dual-tier
   check, and paired coverage become available the moment the programmer
   emits the first candidate, and are re-run after every repair.
4. **The must-preserve contract q is the linchpin.** Both testbenches are
   rendered from one contract extraction, so the generated HLS-C must keep
   the exact C-level top signature (name, argument order, types, directions,
   lengths). The paper guarantees this through the planner's
   interface-lossless brief and the reviewer's contract audit; in this repo
   it is enforced by the generator/repair prompts ("keep the EXACT
   signature", `c2hlsc_agent/llm.py:382-390, 394-405`) and by the fact that
   the oracle testbench is never handed to the model or rewritten
   (`c2hlsc_agent/hlsc_repair_agent.py:213-216`). If the contract changes
   (e.g. a config override of a direction or length), regenerate **both**
   testbenches from the new contract — never hand-edit one side.

Ordering summary:

```
analyze_source(input.c)              # contract extraction (deterministic)
        |
        +--> generate_leveri_testbenches()   # BOTH TB sources, no HLS-C needed
        |         |
        |         +--> build & run golden TB  -> leveri_golden_trace.csv  (now)
        |         +--> KLEE driver on golden C                            (now)
        |
        +--> HLS-C generation (programmer)   # in parallel
                  |
                  v
        build & run HLS TB -> leveri_hls_trace.csv                (needs y(0))
        tb/leveri_compare.py: static tier + dynamic tier          (needs y(0))
        paired gcov coverage                                      (needs y(0))
```

---

## 1. Combined workflow, stage by stage

Each stage lists: what the paper's workflow prescribes, what this repository
already has, and the handoff action.

### Stage A — Contract extraction and brief (paper: planner)

- **Paper**: a planner agent projects a compact, lossy-but-interface-lossless
  brief b from (C source, harness context, contract q), with π_q(b) = q.
- **Repo today**: deterministic static analysis (`c2hlsc_agent/analyze.py`) —
  regex-based top extraction, argument parsing, pointer-direction inference,
  unsupported-construct diagnostics; unbounded pointers default to test
  length 16 with a `missing-pointer-bound` warning (`analyze.py:238-247`).
  The extracted contract plus diagnostics already feed the generator prompt
  (`llm.py:367-391`).
- **Handoff action**: keep the deterministic analyzer as the contract
  authority (auditable, free). Treat `AnalysisResult` + diagnostics as the
  brief. Any planner-LLM refinement is optional and must not be able to
  alter q.

### Stage B — Shift-left testbench generation (the LeVeri addition)

- **Paper**: a companion shift-left pipeline supplies a coverage-driven
  testbench per design; the agent sees only the harness and top signature,
  never golden outputs.
- **Repo today**: three deterministic template generators, no LLM involved:
  the host/CSim oracle testbench (`c2hlsc_agent/testgen.py` — golden C
  macro-included, directed patterns zeros/ones/minmax/alternating then
  seeded random, `num_tests=100`, `seed=1`, sentinel-prefilled outputs,
  1e-6 relative float tolerance); the LeVeri paired-trace bundle
  (`leveri_testgen.py` — synchronized stimulus, CSV traces with header+role
  rows, dual-tier comparator, gcov + KLEE hooks, manifest); and a standalone
  RTL testbench flow (`verilog_testgen.py`). `write_project` already emits
  the LeVeri bundle into every project (`c2hlsc_agent/hls_project.py:162,
  191-197`).
- **Gap**: the automated ladder never runs the LeVeri pair — `verify_project`
  invokes only `make test`; `leveri-test` is a Makefile-only target
  (`hls_project.py:105-108`). Coverage reports are produced but nothing
  consumes them.
- **Handoff action**: wire the LeVeri stage into the automated flow per the
  ordering in §0 — golden side at project setup, paired comparison after
  host software equivalence passes (it shares the host toolchain and costs
  seconds), before spending Vitis runs. Record its verdict in the phase
  state so the failure classifier can use it.

### Stage C — HLS-C generation (paper: programmer)

- **Paper**: the programmer generates y(0) from the brief only; pass@1, one
  candidate per round.
- **Repo today**: single-shot generation prompt containing the FULL original
  C, the interface contract lines, static-analyzer diagnostics, and hard
  output-format requirements (`llm.py:367-391`,
  `hlsc_generator.py:19-107`); structural output gate
  (`is_plausible_translation_unit`, `llm.py:618-625`) with a deterministic
  conservative fallback (`convert.py:58-91`); optional best-of-N candidates
  scored by host equivalence only (`candidates.py:74-119`).
  Note the documented-vs-actual gap: `llm.py:19-21` claims the original C is
  never sent to the model — true only for the repair and QoR prompts.
- **Handoff action**: keep the repo's structural gates and deterministic
  fallback (the paper has no equivalent and they are strictly protective).
  Decide explicitly whether the generation prompt keeps the full original C
  (repo practice) or moves to a brief-only prompt (paper practice); today's
  golden-oracle isolation argument does not require hiding the original C,
  since the original C *is* the specification here.

### Stage D — Verification ladder (paper: four-stage verifier as loop controller)

- **Paper**: compile → CSim → synthesis → CoSim on Vitis HLS, short-circuit
  on first failure, all-pass as the sole headline metric; CoSim watchdog.
- **Repo today**: `software_equivalence → csim → csynth → cosim`
  (`c2hlsc_agent/hls_runner.py:12`), short-circuit with `blocked` cascade;
  the same oracle testbench serves host equivalence and the Vitis TB; CoSim
  exit-0 is additionally gated on log failure markers
  (`cosim_verdict.py:21-44`); per-phase total-duration timeouts with
  process-group kill and partial-log preservation (`equivalence.py:96-122`);
  infra failures (missing/unreachable Vitis) classify as
  `toolchain_unavailable` and never trigger source mutation.
- **Handoff action**: keep the repo ladder. Host software equivalence is a
  *stronger* stage 1 than the paper's bare compile (it is a behavioral
  golden-C check), and the repo's own docs correctly note that Vitis CoSim
  alone proves RTL↔HLS-C, not RTL↔original-C (`README.md:205-209`). The
  LeVeri paired trace slots in as an additional cross-check between stages 1
  and 2 per Stage B.

### Stage E — Failure analysis and bounded repair (paper: failure analyst + programmer patch)

- **Paper**: an LLM failure analyst emits four structured fields (truncated
  error excerpt, failure-family label, named-symbol set, repair intent) plus
  the must-preserve contract; repair budget K=3; each round is stateless
  (only the latest compressed failure; cross-round history lives in an
  audit-only ledger); prompt evidence is limited to brief + failing code +
  failing stage + 80-line excerpt + radius-8 code window, with PMLC's three
  layers (log normalization, AST backward slice, selective instrumentation
  with dual-trace alignment) attached on mismatches.
- **Repo today**: failure analysis is a deterministic regex classifier
  (`agent_loop.py:147-273`) producing family / owner / next-action /
  evidence-needed / repair-scope — functionally the analyst's structured
  output, without an LLM. The repair prompt carries: failing stage, family,
  repair intent, scope, must-preserve signature, a last-3-attempts history
  with diff excerpts and a do-not-repeat instruction, a 4000-character log
  tail, and the ENTIRE current file (`llm.py:434-464`). Four deterministic
  mechanical repairs run before any LLM call
  (`hlsc_repair_agent.py:138-141`). Budgets: `--max-iterations` (default 1,
  repair opt-in via `--auto-repair`), plus persistent budgets
  (8 LLM calls, 8 Vitis runs, 4 h wall) and oscillation guards
  (source+failure fingerprints, project-signature cycle detection)
  in a crash-safe ledger (`run_control.py`).
- **Handoff actions**:
  1. Adopt the paper's budget as the standard run configuration:
     `--auto-repair --max-iterations 3` (K=3). Keep the repo's orthogonal
     budgets and oscillation guards — the paper has no equivalent and they
     prevent unbounded loops the K-cap alone does not.
  2. **PMLC via the LeVeri traces.** On a CoSim/host `behavioral_mismatch`,
     use the paired traces as the mismatch-evidence substrate: the first
     failing row/column reported by `tb/leveri_compare.py` is precisely the
     paper's L1 (failed outputs, first failing cycle, example values), and
     the paired golden/HLS traces are the L3 dual-trace alignment at
     function-call granularity. Feed that *structured divergence* — not the
     raw log — into the repair prompt. L2 (AST backward slicing) remains
     future work: the package is deliberately dependency-free
     (`pyproject.toml:11`) and has no AST tooling today.
  3. Evidence compression stays character-tail based (4000 chars) until
     there is measured need for the paper's 80-line/radius-8 shape; if
     adopted later, it belongs in `build_repair_prompt` (`llm.py:434-464`)
     with the current full-file inclusion replaced by a window around the
     symbols named in the mismatch evidence.

### Stage F — Review / audit (paper: reviewer)

- **Paper**: a reviewer agent performs a contract-and-sanity audit after each
  patch; it never rewrites programmer output.
- **Repo today**: no reviewer LLM. The equivalent controls are mechanical:
  the structural parse gate, the oracle testbench being unwritable by the
  model, oscillation rejection of previously seen candidates, and the full
  ladder re-running from the beginning after every patch
  (`cli.py:438-534`). Every repair is audited to `repair_audit.json` with
  before/after SHA-256 and diffs.
- **Handoff action**: keep the mechanical gates as the reviewer. An LLM
  reviewer is optional backlog; if added, it must remain audit-only
  (no rewriting), matching both the paper and `AGENTS.md`.

### Stage G — Repair memory (paper: self-evolving MoE RAG) — backlog

- **Paper**: typed queries (stage, family, symbols) hard-routed to
  family-specific card sub-pools; exact matching returns at most 3 hints or
  an empty hit; cards are promoted only after human audit.
- **Repo today**: not implemented; named as future work in
  `docs/functional_equivalent_rtl_agent.md:116-121, 166`. The raw material
  already exists: `repair_audit.json` records stage, family, evidence
  excerpt, and before/after diffs per attempt — the card schema's required
  fields — and `scripts/export_cosim_successes.py` curates passing cases.
- **Handoff action**: keep as backlog behind Stages B/E. When picked up,
  promotion into any retrieval pool must require human audit, matching the
  paper and the repo's evidence policies.

---

## 2. Invariants that must hold at every stage

1. The golden oracle is the original C **executed at run time** (macro-renamed
   self-inclusion) — never stored expected-output tables, never sent to the
   repair model, never rewritten.
2. One contract extraction feeds generation and both testbenches; a contract
   change regenerates all of them.
3. The verifier ladder short-circuits and re-runs from the beginning after
   every patch; a later stage never runs past an earlier failure.
4. Infrastructure failures (toolchain unavailable, ssh loss, timeouts of the
   transport) are classified as infra and never trigger source mutation.
5. Every loop is bounded (iterations, LLM calls, Vitis runs, wall clock) with
   durable checkpoints and repeated-state detection; budgets are immutable
   across resume.
6. Testbench and trace passes are evidence, not proof: acceptance is decided
   by the ladder (`leveri_testgen.py:29`).

---

## 3. Concrete integration checklist (ordered)

- [ ] Add a golden-trace smoke step at project setup: compile + run
      `tb/leveri_golden_tb.cpp` before generation; fail fast on contract
      extraction errors (Stage B / §0.2).
- [ ] Run the LeVeri paired comparison automatically after host software
      equivalence passes; record its result as a phase in
      `VerificationState` so `classify_failure` can route on it.
- [ ] On `behavioral_mismatch`, parse the comparator's first-divergence
      output (cycle, column, expected, actual) into structured evidence and
      include it in the repair prompt in place of raw log text (Stage E.2).
- [ ] Make `--auto-repair --max-iterations 3` the documented standard run
      configuration (README examples and configs), keeping existing budgets.
- [ ] Regenerate both LeVeri testbenches whenever the contract changes;
      forbid hand-edits to generated TB files (CI check or preflight).
- [ ] Backlog: L2 backward slicing (needs an AST dependency decision);
      repair-card store fed from `repair_audit.json` with human audit
      (Stage G).

---

## 4. Key references

| Concern | Location |
|---|---|
| Contract extraction | `c2hlsc_agent/analyze.py:233` (`analyze_source`) |
| LeVeri bundle generator | `c2hlsc_agent/leveri_testgen.py:686` |
| Golden self-inclusion pattern | `leveri_testgen.py:688-692`; `testgen.py:200-207` |
| Dual-tier comparator template | `leveri_testgen.py:280-358` |
| Verification ladder / short-circuit | `c2hlsc_agent/hls_runner.py:12, 66-184` |
| CoSim log verdict gate | `c2hlsc_agent/cosim_verdict.py:21-44` |
| Failure families / routing | `c2hlsc_agent/agent_loop.py:147-273` |
| Repair loop | `c2hlsc_agent/cli.py:438-534`; `hlsc_repair_agent.py:117-259` |
| Repair prompt construction | `c2hlsc_agent/llm.py:394-464` |
| Budgets / ledger / oscillation | `c2hlsc_agent/run_control.py` |
| Paper-alignment blueprint | `docs/functional_equivalent_rtl_agent.md` |

## 5. Deliberate divergences from the paper

Kept on purpose, with rationale:

- **Host-equivalence-first ladder** instead of a bare compile stage — a
  stronger stage-1 oracle against the original C.
- **Deterministic-first repairs and deterministic fallback generation** —
  auditable, zero-cost fixes before any model call; the pipeline runs
  end-to-end with no LLM at all.
- **Oscillation guards and persistent immutable budgets** — the paper bounds
  only K; this repo also detects A→B→A cycles and survives restarts.
- **Infra-vs-defect discrimination** (remote Vitis, license, timeout
  transport) — prevents mutating correct source over a transient fault.
- **Opt-in LLM with structural output gates** — model output can never reach
  `src/` unless it parses as a plausible translation unit defining the top.
