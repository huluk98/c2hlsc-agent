# How to read this waveform

This tutorial assumes no prior RTL experience. Keep the [cycle table](cycle_table.md) nearby while viewing either the clean WaveDrom picture or the real VCD.

## 1. Find `clk`

`clk` is the clock: a signal that alternates regularly between 0 and 1. It is the timing reference for the whole design. Its period is 10 ns, so the pattern repeats every 10 ns.

## 2. Find its rising edges

A rising edge is the instant `clk` changes from 0 to 1. The design changes its registered state only on these edges. The test labels them E0, E1, E2, and so on. A register is a piece of state that remembers a value from one clock edge to the next.

## 3. Determine where reset is active

`rst` is an active-high, synchronous reset. Active-high means reset is requested by a value of 1. Synchronous means it takes effect only at a rising clock edge. It is high at E0, E1, and later at E9.

## 4. Initially, look only near rising edges

Ignore the stretches between edges on a first reading. For each rising edge, read `rst`, `in_valid`, `a`, and `b` immediately before the edge; then read registered outputs just after the edge. The testbench drives inputs on falling edges, giving them half a clock cycle to become stable, and checks outputs 1 ps after each rising edge so nonblocking RTL assignments have settled.

## 5. Decide whether an input was accepted

A transaction is one meaningful unit of data sent through an interface. Here, a transaction is the pair `(a,b)`. It is accepted at a rising edge only when `rst=0` and `in_valid=1`. A valid signal says whether nearby data has meaning. When `in_valid=0`, the numbers on `a` and `b` are ignored.

## 6. Name the useful transactions

The accepted pair `3+5` is T0, `200+100` is T1, and `255+255` is T2. Names make it easier to follow data than repeatedly referring to the numbers.

## 7. Follow each transaction by one cycle

Latency is the delay from accepting an input to producing its output. A pipeline is a design that holds work in stages so it can accept new work before earlier work is completely out. This pipeline has a latency of one complete clock cycle: T0 goes from E2 to E3, T1 from E4 to E5, and T2 from E5 to E6.

## 8. Check `out_valid` before reading `sum`

`out_valid=1` means `sum` is a meaningful result on that edge. If `out_valid=0`, ignore `sum`, even if it looks like a sensible number. The valid bit is part of the interface contract, not a decoration.

## 9. Understand the bubble

A bubble is an empty pipeline slot: a cycle in which there is no valid transaction. Because `in_valid=0` at E3, no input is accepted there. One cycle later, at E4, `out_valid=0`; that is the corresponding output bubble.

## 10. See consecutive throughput

Throughput is how often completed transactions can appear. T1 is accepted at E4 and T2 at E5, so they are consecutive inputs. Their outputs appear at E5 and E6, also consecutively. One-cycle latency does not prevent one-result-per-cycle throughput.

## 11. Notice the 9-bit result

An 8-bit unsigned number ranges from 0 to 255. The largest addition is `255+255=510`. Eight bits can hold only 0 through 255, while nine bits can hold 0 through 511, so `sum[8:0]` is required to avoid losing the carry bit.

## 12. A held `sum` can still be invalid

The design saves switching by changing `sum` only for a valid result (or reset). At E4 it still shows 8, and at E7/E8 it still shows 510. Those old values are not new results because `out_valid=0`; they must be ignored.

## 13. `X` is not zero

In a four-state RTL simulation, `X` means unknown: the simulator cannot determine whether a bit is 0 or 1. Zero is a definite known value. An `X` may reveal uninitialized state, competing drivers, or incomplete assignments. This DUT's reset establishes known zero state, while the WaveDrom transaction rows use an invalid marker only as a visual reminder that no transaction exists; that marker is not a DUT bus value.

## 14. RTL timing is not final FPGA timing

This waveform is a functional RTL simulation. Registered values update according to ideal event scheduling; it does not model routing delay, clock skew, register setup/hold margins, or the exact delays of a placed-and-routed FPGA. Timing analysis and implementation-level simulation address those physical effects later.

## Edge-by-edge reading

At E0 `rst=1`, so the synchronous reset clears the pending stage, drives `out_valid=0`, and makes `sum=0`.

At E1 `rst` is still 1, so the same known reset state is maintained.

At E2 reset is low and `in_valid=1`, so T0 (`3+5`) is accepted; `pending_valid` becomes 1 while `out_valid` remains 0.

At E3 `in_valid=0`, so the input pair `42,99` is ignored, while T0 leaves the pending stage as the valid output `sum=8`.

At E4 T1 (`200+100`) is accepted, but `out_valid=0` because the empty input slot at E3 has moved to the output; the held 8 must be ignored.

At E5 T2 (`255+255`) is accepted immediately after T1, and T1 appears as the valid output 300.

At E6 `in_valid=0`, so `17,34` is ignored, while T2 appears as the valid 9-bit output 510.

At E7 no transaction is accepted and `out_valid=0`; `sum` still displays 510, but that value is invalid now.

At E8 the flush-probe pair `1+2` is accepted into the pending stage; there is still no valid output, so the held 510 is ignored.

At E9 reset is asserted at the rising edge, so it discards the pending flush probe and clears the output and internal state to zero.

At E10 reset is low again; `out_valid` stays 0, proving that the probe accepted at E8 was flushed rather than emitted as 3.
