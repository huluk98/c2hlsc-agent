# Shift-Left HLS Verification: Action Highlights

Source analyzed locally: arXiv:2606.17128v1, *Shift-Left High-Level Synthesis
Verification via Knowledge-Augmented LLM Agent* (11 pages).

## What the paper actually establishes

- **Page 3 - scope boundary:** the method verifies aligned Golden-C and HLS-C
  test environments before synthesis. It is a bounded, test-based consistency
  method, not a proof of full program equivalence and not a C-to-HLS-C repair
  system.
- **Page 4, Figure 2 - required loop:** construct paired testbenches; check
  statement/branch/call coverage; check static input/CFG/DDG consistency; run
  dynamic simulation; compare cycle-level input and output traces. Diagnose four
  distinct outcomes: insufficient stimulus, static testbench inconsistency,
  dynamic input mismatch, or target-design behavior mismatch.
- **Pages 5-6, Algorithm 2 - controller behavior:** coverage is the primary
  entry gate, refinement is bounded by `Nmax`, and the same test vector must feed
  both programs. A target-design mismatch is reported with the first failing
  cycle and counterexample rather than silently being treated as a testbench bug.
- **Page 6 - reusable knowledge:** the proposed graph is not just a run ledger.
  It separates coverage priors from semantic alignment rules and retrieves them
  across previously verified program/testbench pairs.
- **Pages 8-9, Tables I-III - evidence:** the complete KG+Agent system reports
  98.03% average coverage and 94.39% dynamic consistency with GPT-5-mini; the
  Gemini-3-pro configuration reports 98.26% and 95.33%. Agent-only is lower at
  95.64% coverage and 86.92% dynamic consistency. These are testbench-level
  metrics, not synthesis QoR or formal equivalence results.
- **Page 10 - explicit remaining work:** the authors name RTL-level equivalence,
  multi-module verification, and reinforcement-learning-based adaptive scheduling
  as future work.

## What is already present in `c2hlsc-agent`

- Paired Golden-C/HLS-C trace generation with synchronized stimuli.
- A trace comparator that distinguishes stimulus mismatches from output behavior
  mismatches in human-readable errors.
- `gcov` evidence and bounded relational KLEE evidence.
- A fail-closed CSim -> CSynth -> C/RTL CoSim ladder with Vitis evidence checks.
- Bounded repair iterations, a repair audit, oscillation rejection, and reusable
  repair cards.
- A deterministic `verification_knowledge_graph.json` run/evidence ledger.
- Local Bambu/Verilator correctness checks and separate post-synthesis PPA work.

## Work to do next

### P0 - make the paper's verification contract real

1. **Add a structural testbench-consistency artifact.** Parse both generated
   testbenches and record input-stimulus, CFG, and DDG alignment independently.
   Emit scores, thresholds, hashes, and PASS/FAIL in a versioned JSON report.
   Header/role equality alone is not the page-4 static gate.
2. **Turn coverage evidence into a bounded controller.** Parse statement, branch,
   and call coverage; compare them with configured thresholds; feed uncovered
   paths to KLEE; append KLEE vectors to one shared stimulus manifest; regenerate
   both testbenches; stop after a declared maximum iteration count.
3. **Make failure classes machine-readable.** Emit distinct reason codes for
   insufficient coverage, static testbench mismatch, dynamic input mismatch, and
   target-design mismatch. Only the first three may rewrite testbench artifacts;
   a target-design mismatch must preserve the counterexample and enter the HLS-C
   repair path.
4. **Pin the loop with negative tests.** Include deliberately desynchronized
   stimuli, CFG/DDG drift, an uncovered branch, and a true Golden-C/HLS-C output
   mismatch. Assert the earliest failing layer and that downstream Vitis stages do
   not run after a failed shift-left gate.

### P1 - produce evidence the paper does not provide

5. **Separate the run ledger from cross-project retrieval.** Keep the current
   provenance graph, but add a small validated-card store for coverage vectors and
   semantic alignment rules. Retrieve only cards whose toolchain, contract, and
   prior verification hashes match.
6. **Run an aligned benchmark, not the raw `passk/` dump.** Finish all intended
   benchmark rows, remove absolute workstation paths, retain compact reports and
   provenance, and publish per-stage attrition: parse/compile, host equivalence,
   shift-left, CSim, CSynth, CoSim, and post-synthesis PPA.
7. **Report independent oracles and hidden tests.** Measure statement/branch/call
   coverage, mutation kills, boundary vectors, and held-out failures separately.
   High coverage or a KLEE bounded PASS must never be described as proof.
8. **Re-run accepted designs through native Vitis.** Compare QoR only when part,
   clock, tool version, directives, and implementation flow match. Keep local
   Bambu results labeled as local correctness evidence, not Vitis QoR.

### P2 - paper follow-on work

9. Add RTL-level equivalence or an independently generated RTL testbench after
   CoSim, preserving Golden-C as the oracle.
10. Extend contracts and trace schemas to multi-module/stateful designs.
11. Consider adaptive scheduling only after P0/P1 data exists; a learned scheduler
    needs replayable phase costs and outcomes, not guessed rewards.

## Required acceptance evidence

- Focused unit tests for each new failure class and artifact schema.
- Full Python test suite and `git diff --check`.
- A generated sample project whose reports show the complete shift-left ladder.
- Native Vitis CSim/CSynth/CoSim evidence where available; otherwise mark it
  `SKIP` with the exact missing runner/tool/license reason.
- `git-harness check --scope staged --stage commit` before committing and
  `git-harness check --scope all --stage push` before any later push.

## Bottom line

The current repository already implements most of the execution ladder. The
highest-value missing piece is the **closed coverage/static-consistency refinement
controller** from pages 4-6, followed by a clean benchmark that proves its effect.
Do not spend the next iteration adding another model or another UI surface.
