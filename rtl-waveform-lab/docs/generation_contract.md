# Precise generation contract

Use this before asking a person or model to generate RTL or HLS-C. A plausible-looking source file is not evidence of correctness; the request and returned evidence must make the design falsifiable.

## Reusable input template

Copy this block and replace every bracketed field. Do not leave width, signedness, timing, or invalid-cycle behavior implicit.

```text
ROLE
Generate a small, synthesizable [SystemVerilog RTL / HLS C++] design. Treat this
contract as authoritative. List any contradiction instead of guessing.

1. FUNCTIONAL CONTRACT
- Operation: [mathematical/algorithmic definition]
- Inputs: [name, type, bit width, signedness, legal range for every input]
- Outputs: [name, type, bit width, signedness, legal range for every output]
- Arithmetic: [exact intermediate widths; overflow, saturation, rounding]
- Corner cases: [zero, extrema, ties, empty/full, invalid values]

2. TEMPORAL AND INTERFACE CONTRACT
- Clock/reset: [edge, reset polarity, synchronous/asynchronous, priority]
- Acceptance event: [for example, rising edge with valid && ready]
- Output event: [validity rule and payload-stability rule]
- Latency: [fixed N cycles or precise variable-latency rule]
- Throughput/target II: [transactions per cycle]
- Stalls/backpressure: [supported behavior, or explicitly none]
- Reset effect: [state/output clearing and in-flight transaction policy]
- Invalid cycles: [which output values are don't-care/ignored]

3. IMPLEMENTATION CONTRACT
- Top name and exact port/function signature: [signature]
- Allowed state/architecture: [requirements only; avoid overspecifying internals]
- Synthesizable subset: no DUT delays, test-only system tasks, or initial stimulus.
- Tool target: [tool and version; device/part; clock constraint if applicable]
- HLS only: [fixed-width types, loop bounds, array bounds, interfaces,
  permitted pragmas/libraries, forbidden constructs]

4. INDEPENDENT VERIFICATION ORACLE
- Reference rule: [equation or separately implemented reference function]
- Directed cases: [boundary and protocol sequences]
- Exhaustive/random cases: [domain or seed/count]
- Assertions: [temporal invariants]
- Pass criteria: [compile/lint/sim/synthesis/CSim/CoSim requirements]
- Failure criteria: any mismatch, unknown control, latch, multidriver, unsupported
  construct, warning designated fatal by this contract, or missing required report
  fails. Return and classify every other warning instead of silently ignoring it.

5. QOR CONTRACT (SEPARATE FROM CORRECTNESS)
- Target latency/II/clock: [numbers and units]
- Resource budgets: [LUT, FF, BRAM, DSP]
- Evidence source: [named synthesis/implementation report]
- Comparison controls: [same device, tool/version, clock, directives, test data]

6. DELIVERABLES
- Synthesizable source and separate self-checking testbench.
- Reproduction commands, tool versions, logs, and machine-readable reports.
- A contract-to-code mapping and explicit unresolved assumptions/warnings.
- Do not claim QoR from source inspection or functional simulation.
```

## Filled contract for this lab

### External behavior

- Top module: `one_cycle_delayed_adder`.
- Inputs: `clk`, active-high synchronous `rst`, `in_valid`, and unsigned `a[7:0]`, `b[7:0]`.
- Outputs: `out_valid` and unsigned `sum[8:0]`.
- When `rst=0`, an input transaction is accepted at every rising edge with `in_valid=1`. There is no `ready`, stall, or backpressure signal.
- An accepted pair `(a,b)` produces `{1'b0,a}+{1'b0,b}` with `out_valid=1` exactly one rising edge later.
- The result range is 0–510. Both operands are explicitly zero-extended to nine bits before addition; no carry may be truncated.
- Consecutive accepted inputs must produce consecutive valid outputs: latency is one cycle and maximum throughput is one result per cycle.
- An edge with `in_valid=0` creates a bubble (`out_valid=0`) exactly one cycle later. When `out_valid=0`, `sum` is ignored and may legally hold an old value.
- `rst` has priority at its rising edge: it drives `out_valid=0`, requires `sum=0`, and discards all in-flight work. Reset-high inputs are not accepted.
- Registered outputs are judged after nonblocking assignments settle. Every right-hand side in one `always_ff` edge observes pre-edge state; source-code line order does not create sequential software steps.

### Architecture freedom

The contract does not require internal signals named `pending_valid` or `pending_sum`. A candidate may use any synthesizable internal structure that produces the same port behavior. Testbenches must not inspect hidden DUT state.

### Acceptance evidence

Run:

```sh
make verify
```

The gate requires all of the following:

1. Strict Verilator lint passes.
2. The reviewed E0–E10 directed sequence passes in a four-state Icarus simulation.
3. A black-box test streams all 65,536 unsigned operand pairs and checks reset, flush, bubbles, exact latency, and one-per-cycle throughput.
4. Six deliberately faulty implementations—truncated arithmetic, wrong operator, wrong latency, missing reset flush, asynchronous reset, and reset that loses priority when input is valid—are rejected by the same black-box oracle.
5. Yosys parses, lowers, checks, and synthesizes the RTL without structural errors, producing `build/synth.log` and `build/synth.json`.
6. The generated trace, study table, WaveJSON syntax, quiz count, links, and inline JavaScript pass consistency checks.

Passing proves this module against this stated contract with the named local tools. It does not prove post-place-and-route timing, CDC safety, power, FPGA-specific resource use, or HLS C/RTL equivalence.

## HLS-specific extension

For C-to-HLS-C work, add the exact C reference semantics and the HLS tool boundary:

- Eliminate C/C++ undefined behavior before using the program as an oracle.
- State fixed-width types, loop and memory bounds, pointer alias assumptions, and interface mapping.
- Name the HLS tool/version, target part, clock, top function, and directives.
- Require distinct gates: host compile, reference tests, CSim, C synthesis/report, and C/RTL CoSim. A failure at one gate cannot be relabeled as success at another.
- Read latency, initiation interval, clock estimate, and resource estimates from the produced report. Do not infer them from source text.

Correctness evidence and QoR evidence answer different questions. Correctness asks whether results and protocol match the oracle; QoR asks how the synthesized design performs under a controlled tool/device/clock setup.
