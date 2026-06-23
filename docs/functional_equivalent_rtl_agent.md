# Functional Equivalent RTL Multi-Agent Blueprint

This blueprint combines two complementary ideas from the provided papers:

- The Shift-Left verification paper contributes the testbench and coverage loop:
  dual-tier consistency checking, coverage-driven stimulus refinement, and separation
  of testbench bugs from design bugs.
- The HLSC-agent paper contributes the C-to-HLS-C generation and repair loop:
  four-stage Vitis verification, PMLC mismatch localization, compact repair evidence,
  and repair-success memory.

The result should be a loop of cooperating agents, not one monolithic "generate RTL"
agent.

## Definition of Done

The practical signoff target is:

```text
original C + synchronized stimuli
  == host-equivalent HLS-C
  == Vitis CSim equivalent HLS-C
  == synthesized RTL passing C/RTL CoSim
```

This is bounded, testbench-driven functional equivalence under the declared interface
contract. It is stronger than "Vitis produced Verilog" but weaker than a universal formal
proof over all possible inputs. If the input space is finite and small, add exhaustive
stimuli; otherwise keep coverage metrics and counterexamples in the report.

## Agent Roles

1. Contract planner
   - Inputs: original C/C++, top function, user config.
   - Outputs: must-preserve signature, pointer bounds, scalar ranges, interface mode,
     legal input domain, unsupported construct diagnostics.
   - Failure ownership: missing bounds, ambiguous pointer directions, unsupported C.

2. Shift-left testbench agent
   - Inputs: original C and contract.
   - Outputs: golden-C oracle harness, generated stimuli, trace schema, coverage plan.
   - Procedures:
     - Prefer a single shared testbench that calls a macro-renamed golden C function and
       the HLS-C function with cloned inputs. This avoids dual-testbench asymmetry.
     - If separate C and HLS-C testbenches are required, enforce static consistency over
       input stimulus, CFG shape, and def-use/DDG structure.
     - Add directed cases first: zero, all ones, min/max, alternating bits, length
       boundaries, alias-risk boundaries, and user-provided semantic corners.
     - Add coverage refinement later with gcov plus KLEE or a constraint-solver shim.
   - Failure ownership: insufficient coverage, input trace mismatch, testbench compile
     errors, incorrect argument metadata.

3. HLS-C generator agent
   - Inputs: original C, diagnostics, contract, testbench expectations.
   - Outputs: `hls_top.hpp`, `hls_top.cpp`, transformation ledger, pragma ledger.
   - Procedures:
     - Preserve behavior before optimizing.
     - Replace non-synthesizable constructs with fixed-size, caller-managed structures.
     - Make bitwidth and signedness changes explicit in the ledger.
     - Add interface pragmas only from config or a documented contract.
   - Failure ownership: host mismatch, CSim mismatch, non-synthesizable code.

4. Cosim operator
   - Inputs: generated project and Vitis settings.
   - Outputs: host equivalence log, CSim log, CSynth log, CoSim log, phase status.
   - Procedures:
     - Run stages in short-circuit order: host software equivalence, CSim, CSynth, CoSim.
     - Treat Vitis as the loop controller, not only a pass/fail oracle.
     - Use watchdogs for CoSim: total timeout, stdout-silence timeout, subprocess
       liveness.
     - Preserve full logs as audit-only artifacts; pass compact excerpts to repair.
   - Failure ownership: missing Vitis, bad TCL, deadlocks, phase scheduling.

5. Failure analyst
   - Inputs: earliest failing stage, compact logs, local code window, mismatch outputs.
   - Outputs: failure family, named symbols, repair intent, evidence pack.
   - Failure taxonomy:
     - `static_source_rejected`
     - `host_behavior_mismatch`
     - `testbench_or_c_semantics`
     - `interface_contract`
     - `memory_pointer`
     - `numeric_bitwidth`
     - `loop_scheduling`
     - `non_synthesizable_construct`
     - `rtl_cosim_mismatch`
     - `timeout_or_deadlock`
     - `toolchain_unavailable`
   - PMLC for CSim/CoSim mismatch:
     - L1: normalize log into first failing test/cycle, failed outputs, examples.
     - L2: slice backward from failed outputs to suspect assignments/control sites.
     - L3: instrument suspect variables in golden C and HLS-C and align the first
       divergent value.

6. HLS-C repair agent
   - Inputs: current candidate, failure analysis, contract, optional repair cards.
   - Outputs: minimal patch, patch rationale, updated ledger.
   - Procedures:
     - Repair only the current candidate with the latest compact evidence.
     - Do not accumulate long cross-round history in the repair prompt.
     - Rerun the full verifier from the beginning after every patch.

7. RTL optimizer agent
   - Inputs: four-stage passing HLS-C, Vitis synthesis reports, optimization policy.
   - Outputs: optimized HLS-C candidates, QoR delta, accepted/rejected decisions.
   - Gate:
     - This agent is disabled until functional equivalence is signed off.
   - Procedures:
     - Try one optimization family at a time: pipeline, unroll, array partition,
       dataflow, interface choice, bitwidth narrowing.
     - Accept only if host equivalence, CSim, CSynth, and CoSim all pass again.
     - Record QoR deltas and rollback rejected changes.

8. Audit memory agent
   - Inputs: artifacts, patches, failure analyses, human audit outcome.
   - Outputs: audit ledger, repair-success cards, retrieval blind-spot notes.
   - Procedures:
     - Keep full logs, traces, hidden labels, and manual fixes out of prompt-facing
       evidence.
     - Promote a repair card only after a complete failure-to-pass chain is audited.
     - Route retrieval by failing stage, failure family, and named symbols.

## Main Loop

```text
plan contract
generate/update testbench
generate/update HLS-C
run host equivalence
if fail: analyze -> repair owner -> rerun
run Vitis CSim
if fail: analyze -> repair owner -> rerun
run Vitis CSynth
if fail: analyze -> repair owner -> rerun
run Vitis CoSim
if fail: PMLC -> repair owner -> rerun
if pass: lock functional equivalence
run optimizer candidates
for each accepted candidate: rerun full verifier
write report and audit memory
```

## Corrections to the Initial Mental Model

- Your agent breakdown is directionally right: testbench agent, HLS-C generator, CoSim
  operator, RTL optimizer.
- The testbench agent should not merely emit a testbench. It owns coverage, stimulus
  synchronization, and the distinction between testbench bugs and design bugs.
- The CoSim operator should not only invoke Vitis. It should short-circuit by stage,
  classify failures, extract compact evidence, and apply watchdogs.
- The RTL optimizer should be post-equivalence. If it runs before the design is
  behaviorally locked, it can hide semantic bugs behind PPA edits.
- "Functional equivalent RTL" is not guaranteed by CoSim alone. It requires original-C
  oracle comparison before synthesis and CoSim after synthesis, under the same contract
  and stimuli.

## Near-Term Implementation Gaps

- Add a real repair backend for `hlsc_repair_agent`; the current package classifies the
  owner/action but does not yet patch failed candidates.
- Add coverage collection with `gcov` and optional KLEE-derived vectors.
- Add PMLC instrumentation for CSim/CoSim mismatches.
- Add an optimizer queue that snapshots candidates and rolls back rejected QoR changes.
- Add a structured audit ledger and repair-card store.
