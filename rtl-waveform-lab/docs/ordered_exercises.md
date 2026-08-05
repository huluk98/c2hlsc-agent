# Ordered three-day exercises and mastery gates

The realistic three-day outcome is foundational competence with small, single-clock RTL—not mastery of all digital design, FPGA implementation, or HLS research. Work in dependency order and do not mark a stage complete from reading alone.

Gate labels:

- **LOCAL** — runnable now with supplied repository code and commands.
- **MANUAL** — a precisely specified learner artifact reviewed against the stated oracle; no scaffold or hidden test is supplied.
- **EXTERNAL** — requires a separately installed/configured vendor tool and its real report.

Use `pdf_reading_map.md` to attach the supplied readings to these active exercises.

## Preflight — 30 to 45 minutes

Inputs: binary/hex conversion, basic algebra, simple programming, and a working shell.

Required outputs:

1. Convert `0b10110110` to hexadecimal and unsigned decimal.
2. Represent `-18` as eight-bit two's complement.
3. List all input rows for two Boolean variables.
4. Explain why a test returning exit status zero means “pass” by convention.
5. Run `./scripts/check_environment.sh` and identify each required tool.

Gate (**MANUAL + LOCAL**): all first four answers are correct and the environment check passes. Answers: `0xB6`, `182`, `1110_1110`, four rows, and exit zero denotes successful completion.

## Day 1 — behavior, Boolean logic, widths, combinational RTL

### Stage 1: specification → truth table → equation

Input: “Output `y` is 1 exactly when one of `a` and `b` is 1.”

Interface: `module xor2(input logic a, b, output logic y);`.

Required output:

- Truth table rows `00→0`, `01→1`, `10→1`, `11→0`.
- Equation `y = (~a & b) | (a & ~b)` and identification as XOR.
- Gate sketch and SystemVerilog `assign y = a ^ b;`.

Oracle: exhaust all four input combinations. Gate (**MANUAL**): 4/4 match; write the source and self-checking test in your exercise workspace.

### Stage 2: half-adder

Input: two one-bit unsigned operands `a`, `b`.

Interface: `module half_adder(input logic a, b, output logic sum, carry);`.

Required output: `sum = a ^ b`, `carry = a & b`, a four-row truth table, and an exhaustive self-checking test.

Gate (**MANUAL**): all rows pass and no width/latch lint warning exists. No repository scaffold is supplied for this learner-written module.

### Stage 3: mux and complete combinational assignment

Input: `y = d0` when `sel=0`; otherwise `y = d1`.

Interfaces: `mux2_assign` and `mux2_comb`, each with one-bit inputs `sel`, `d0`, `d1` and one-bit output `y`.

Required output: implement once with `assign` and once with `always_comb`. In the procedural version, use blocking `=` and assign `y` on every path. Remove one branch deliberately and observe the latch warning before fixing it.

Gate (**MANUAL**): both correct versions match on all eight `{sel,d0,d1}` combinations; the intentional incomplete version is rejected by lint.

### Stage 4: width/signedness worksheet

Before coding each expression, state operand widths, signedness, mathematical range, chosen result width, and overflow rule.

- Unsigned `8-bit + 8-bit`: full result needs 9 bits, range 0–510.
- Unsigned `8-bit * 8-bit`: full result needs 16 bits, range 0–65,025.
- Signed two's-complement `8-bit + 8-bit`: a 9-bit mathematical result covers -256–254; an 8-bit stored result wraps unless saturation is explicitly implemented.
- Right shift of signed versus unsigned data: declare whether arithmetic sign-fill or logical zero-fill is required.
- Mixed signed/unsigned expressions: cast or extend explicitly; do not rely on an unstated language rule.

Gate (**MANUAL**): predict boundary values before simulation and receive no Verilator width/signedness warning on your exercise source.

## Day 2 — state, clocks, reset, protocols, waveform

### Stage 5: register and nonblocking semantics

Input:

```systemverilog
always_ff @(posedge clk) begin
  q1 <= d;
  q2 <= q1;
end
```

Assume an active-high synchronous reset first establishes `q1=q2=0`. Reset is then low, and `d` is stable as 1, 0, and 1 immediately before edges E1, E2, and E3 respectively.

Required output: explain that `q2` receives the pre-edge `q1`, not the newly scheduled `d`; then give `(q1,q2)` after E1, E2, and E3. Oracle: `(1,0)`, `(0,1)`, `(1,0)`.

Gate (**MANUAL**): all three predicted states and the nonblocking explanation are correct before simulation.

### Stage 6: reset and finite-state machine

Implement a modulo-4 counter and a Moore controller. The controller uses a two-bit state register with `IDLE=2'b00`, `ACTIVE=2'b01`, and reserved encodings `10` and `11`. Active-high synchronous reset enters `IDLE`; input `start` moves `IDLE→ACTIVE`; input `stop` moves `ACTIVE→IDLE`; otherwise state holds; output `busy=1` only in `ACTIVE`; either reserved state recovers to `IDLE` on the next edge. Write reset priority and the transition/output table before RTL. Compare synchronous reset with asynchronous assertion conceptually; do not mix the contracts.

Gate (**MANUAL**): counter reset/wrap, every controller transition, output, and both reserved-state recoveries match the written table.

### Stage 7: valid-only pipeline

First predict E0–E10 without opening the table. Then study this lab and run `make sim`.

Required output: identify acceptance edges, output edges, bubbles, held-invalid data, latency, throughput, nine-bit arithmetic, and reset flush. Explain that this toy always accepts `in_valid=1`; it is not a ready/valid interface.

Gate (**LOCAL**): `make sim` passes and the waveform quiz score is at least 8/10. This gate certifies waveform reading only.

### Stage 8: ready/valid boundary

Input: a transfer occurs only when `valid && ready` is 1 at the active edge.

Five-edge trace, shown as `(valid,ready,data)` before E0–E4: `(0,1,—)`, `(1,0,8'h2A)`, `(1,0,8'h2A)`, `(1,1,8'h2A)`, `(0,1,—)`.

Required output: identify the bubble, stall, and transfer edges; state the source's stability obligation while stalled. Oracle: E0/E4 are bubbles, E1/E2 are stalled with stable payload, and the only transfer is E3.

Gate (**MANUAL**): all classifications and held-payload cycles match the oracle. Implementation of a full AXI interface is deferred.

## Day 3 — verification, synthesis, HLS bridge

### Stage 9: independent oracle

Read `generation_contract.md`, then run `make exhaustive` and `make mutations`.

Required output: explain why checking only public ports permits equivalent architectures, why all operand pairs do not by themselves cover temporal behavior, and why mutation rejection tests the sensitivity of the oracle.

Gate (**LOCAL**): all 65,536 pairs pass and every seeded fault fails.

### Stage 10: RTL synthesis boundary

Run `make lint` and `make synth`; inspect `build/synth.log`.

Required output: identify inferred registers and addition logic, and explain why generic synthesis does not prove device timing, placement/routing, CDC correctness, or power.

Gate (**LOCAL**): lint and Yosys structural checks pass with no unintended latch or multidriver.

### Stage 11: C → HLS-C → RTL evidence ladder

#### Stage 11A: literature comprehension

Input: HLStrans PDF pages 1-2 plus the paper-ingestion focus and evidence-ledger template in `fundamentals_field_guide.md`.

Required output: one evidence-ledger row that separates the source task, target artifact, dataset-entry contents, five transformation categories, synthesis-derived annotations, author claims, evidence visible on pages 1-2, and evidence still unverified.

Gate (**MANUAL**): the row is source-faithful and does not treat a paired testbench, synthesis annotation, or author performance claim as proof that a new candidate is correct or fairly compared.

#### Stage 11B: actual HLS execution

Input: a defined, tested reference-C function plus the HLS fields in `generation_contract.md` and documentation matching the installed HLS tool version.

Required output:

```text
Reference C: algorithmic oracle with defined arithmetic and memory behavior
HLS C++: bounded/fixed-width implementation plus tool-specific interfaces/directives
CSim: software-level functional check
C synthesis: scheduled/bound RTL and latency/II/resource estimates
C/RTL CoSim: generated RTL checked against transaction-level C behavior
Implementation/STA: device mapping and timing evidence
```

Gate (**EXTERNAL**): for a real HLS tool run, preserve the exact command, version, part, clock, directives, tests, report, and CoSim result. Literature comprehension cannot satisfy this gate, and this repository does not fabricate the evidence when Vitis HLS is absent.

### Stage 12: capstone

Generate a new small module from a filled contract: for example, a saturating 8-bit signed accumulator with valid/ready backpressure. Return the contract, RTL, separate oracle, directed boundaries, randomized sequence test, lint result, synthesis report, and unresolved warnings.

Gate (**MANUAL**, or **EXTERNAL** when a course/CI supplies hidden tests): the published contract tests pass, seeded faults are rejected, and you can explain each state transition and report metric. This repository currently supplies no capstone scaffold or hidden tests.
