# Waveform-reading quiz

Try answering from the waveform before opening the answer section.

## Questions

1. At which labeled rising edges is synchronous reset active, and what happens to `out_valid`, `sum`, and pending state there?
2. Which edges accept T0, T1, and T2? What two conditions make an ordinary input transaction acceptable?
3. Which output edge and result belong to each of T0, T1, and T2?
4. Measured in complete clock cycles and nanoseconds, what is the input-to-output latency?
5. Why is `out_valid=0` at E4, and which earlier input edge caused that pipeline bubble?
6. What do E5 and E6 demonstrate about throughput, even though the latency is one cycle?
7. At E7, `sum` is still 510. Is 510 a new output result at E7, and how do you know?
8. Why must `sum` be nine bits wide for T2? What incorrect value would the low eight bits alone represent for 510?
9. What is the first edge after the second reset edge E9, and what observation there proves the old pending work was cleared?
10. The flush probe `1+2` is pending after E8. What happens when reset is asserted at E9, and does a valid result 3 ever appear?

## Answers

1. Reset is active at E0, E1, and E9. At each edge, `out_valid`, `sum`, `pending_valid`, and `pending_sum` become zero.
2. T0 is accepted at E2, T1 at E4, and T2 at E5. For an ordinary acceptance, `rst` must be 0 and `in_valid` must be 1 at the rising edge.
3. T0 produces 8 at E3, T1 produces 300 at E5, and T2 produces 510 at E6.
4. The latency is one complete 10 ns clock cycle: each accepted transaction appears at the following rising edge.
5. E4 is an output bubble because no input was accepted at E3 (`in_valid=0`). The empty E3 slot moves through the one-cycle pipeline to E4.
6. They show one-result-per-cycle throughput: T1 and T2 appear on consecutive edges because they were accepted on consecutive edges.
7. No. `out_valid=0` at E7, so the held 510 must be ignored regardless of how meaningful it looks.
8. `255+255=510`, which exceeds the 8-bit unsigned maximum of 255. Nine bits can represent 510. Keeping only the low eight bits would wrap to 254 (`510 mod 256`).
9. E10 is the first edge after E9. Its `out_valid=0` shows that no pending pre-reset transaction survived to become an output.
10. Synchronous reset has priority at E9, clearing the pending probe. No valid result 3 appears; `sum` is reset to 0 and remains invalid afterward.
