# RTL and HLS fundamentals field guide

This is a dependency map, not a claim to contain every current paper, device flow, protocol, or verification method. In three days, target a reliable core mental model and a method for evaluating generated outputs.

## The ordered ladder

```text
Behavioral specification + independent oracle
→ Boolean algebra and truth tables
→ vectors, widths, signedness, and 0/1/X/Z semantics
→ combinational RTL and latch avoidance
→ registers, nonblocking updates, reset, and FSMs
→ transaction protocols, stalls, and pipelines
→ testbench, assertions, lint, and functional simulation
→ RTL synthesis and the timing/CDC boundary
→ defined reference C
→ bounded, fixed-width, tool-specific HLS C++
→ CSim → C synthesis/report → C/RTL CoSim
→ implementation, static timing analysis, and hardware validation
```

Skipping an earlier contract usually creates code that compiles while answering the wrong question.

## Core concepts you must be able to explain

### Combinational behavior

- Outputs are a function of current inputs; there is no intended stored state.
- `assign` or complete `always_comb` logic is appropriate. Blocking assignment is normally used inside combinational procedures.
- Every output must be assigned on every path. An omitted path can infer a latch.
- Boolean simplification can change structure without changing the truth table.

### Values and arithmetic

- SystemVerilog simulation has four logic values: `0`, `1`, unknown `X`, and high-impedance `Z`.
- Bit width and signedness are part of behavior. State sign/zero extension, truncation, wrap, saturation, and rounding explicitly.
- A correct mathematical formula can still be incorrect RTL if an intermediate expression is too narrow or changes signed interpretation.

### Sequential behavior

- A register samples at a clock edge and retains state between edges.
- In one clocked procedure, nonblocking right-hand sides use pre-edge values and scheduled left-hand sides become visible together afterward.
- Reset polarity, synchrony, priority, release assumptions, and in-flight transaction policy belong in the interface contract.
- An FSM needs a state set, transition relation, output rule, reset state, and illegal-state behavior.

### Transactions and pipelines

- Data is meaningful only under its validity contract.
- Latency is time/cycles from acceptance to completion. Throughput is completion rate. Initiation interval (II) is the minimum cycles between accepted starts in an HLS schedule.
- `valid` alone is not ready/valid. With backpressure, transfer occurs on `valid && ready`, and a stalled source holds payload stable.
- Reset may flush, retain, or replay in-flight work; the contract must choose one.

### Evidence

- Parsing, compiling, simulation, lint, synthesis, CoSim, implementation, and timing analysis are different gates.
- A testbench must observe the specified interface and use an independently written oracle. Mirroring the DUT algorithm or internal registers can reproduce the same mistake.
- Boundary sequences test extrema and protocol transitions; random tests explore combinations; exhaustive tests prove only their finite enumerated domain; mutation tests show whether the oracle notices representative faults.
- Functional correctness and quality of results (QoR) are separate. QoR comparisons require actual reports under the same tool, version, device, clock, constraints, and measurement method.

## HLS boundary

“HLS-C” is shorthand for a tool-specific synthesizable C/C++ subset, exact-width libraries, interfaces, and directives—not a universal standalone language. A useful generation task must define:

- C arithmetic and memory behavior with no undefined reference semantics;
- fixed widths and numeric policy;
- bounded loops, arrays, streams, pointers, and alias assumptions;
- top function, interface synthesis, device, clock, tool/version, and directives;
- tests and comparison policy for CSim and CoSim;
- latency, II, timing, and resource report fields.

## What this lab proves

`make verify` supplies strong local evidence for one unsigned, valid-only, single-clock delayed adder: strict lint, a reviewed four-state trace, exhaustive data inputs, temporal/reset cases, mutation sensitivity, and generic RTL synthesis.

It does not prove post-route setup/hold, clock constraints, metastability/CDC safety, FPGA-specific resource mapping, power, formal completeness, ready/valid behavior, AXI compliance, HLS scheduling, or C/RTL equivalence.

## Deliberately deferred after the sprint

- SystemVerilog Assertions and formal proof beyond introductory invariants.
- Full AXI protocols, arbiters, FIFOs, and memory systems.
- Multi-clock CDC implementation and reset-domain crossing.
- Constraints, place-and-route, timing closure, power, and board bring-up.
- Fixed/floating-point error analysis, saturation/rounding libraries, and DSP mapping.
- HLS dataflow deadlocks, memory banking, pragma search, design-space exploration, and cross-tool benchmarking.

## Primary references

- [IEEE 1800-2023 SystemVerilog standard](https://standards.ieee.org/ieee/1800/7743/)
- [Icarus Verilog documentation](https://steveicarus.github.io/iverilog/)
- [Verilator lint and command reference](https://verilator.org/guide/latest/exe_verilator.html)
- [Yosys generic synthesis flow](https://yosyshq.readthedocs.io/projects/yosys/en/latest/using_yosys/synthesis/synth.html)
- [AMD Vitis HLS 2026.1 C modeling and RTL implementation](https://docs.amd.com/r/en-US/ug1399-vitis-hls/C-Modeling-and-RTL-Implementation)
- [AMD Vitis HLS 2026.1 testbench guidance](https://docs.amd.com/r/en-US/ug1399-vitis-hls/Writing-a-Test-Bench)
- [AMD Vitis HLS 2026.1 C/RTL CoSim](https://docs.amd.com/r/en-US/ug1399-vitis-hls/Co-simulation)

Use current tool documentation for the exact version in a real project. Language standards and vendor flows evolve; this guide establishes evaluation questions, not a frozen substitute for primary sources.

## Precise paper-ingestion focus

When using the parent repository's paper key-point agent, a broad phrase such as “HLS generation” can blur claims and evidence. Use a focus input that forces comparable fields:

```text
Map each paper's source task and target artifact; supported C/HLS/RTL subset;
tool, version, device, and clock; dataset and case count; generation or repair
loop; correctness gates (parse, compile, CSim, synthesis, CoSim); independence
of the oracle; QoR metrics with units; baselines under matched budgets;
ablations; failure modes and limitations; artifacts and reproducibility.
Separate author claims from demonstrated evidence and mark every unreported field.
```

No focus string can guarantee complete ingestion of a research field. Maintain an evidence ledger with paper/version/date, exact supported claim, evaluation context, and unresolved conflict; refresh searches and vendor documentation for time-sensitive conclusions.
