# Rising-edge cycle table

The testbench changes inputs at falling edges, halfway between the rising edges shown here. “Pending” means captured inside the design and waiting to become an output. A dash in the output-transaction column means that `sum` is invalid and must be ignored.

| Edge | Simulation time | rst | in_valid | a | b | Input transaction accepted | Internal pending-valid state | out_valid after the edge | sum after the edge | Output transaction | Explanation |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|---|
| E0 | 5 ns | 1 | 0 | 0 | 0 | No | 0 | 0 | 0 | — | Reset edge: all valid state and `sum` are cleared. Ignore `sum` because valid is low. |
| E1 | 15 ns | 1 | 0 | 0 | 0 | No | 0 | 0 | 0 | — | Second reset edge; the cleared state is maintained. Ignore `sum`. |
| E2 | 25 ns | 0 | 1 | 3 | 5 | T0 | 1 | 0 | 0 | — | T0 is sampled. Its result is pending; the output is not valid yet. Ignore `sum`. |
| E3 | 35 ns | 0 | 0 | 42 | 99 | No (input bubble) | 0 | 1 | 8 | T0 | T0 emerges exactly one cycle after E2. The arbitrary 42 and 99 are not accepted. |
| E4 | 45 ns | 0 | 1 | 200 | 100 | T1 | 1 | 0 | 8 | — | T1 is sampled. The input bubble at E3 becomes an output bubble here; held value 8 is ignored. |
| E5 | 55 ns | 0 | 1 | 255 | 255 | T2 | 1 | 1 | 300 | T1 | T1 emerges, while T2 is accepted into the pending stage. |
| E6 | 65 ns | 0 | 0 | 17 | 34 | No (input bubble) | 0 | 1 | 510 | T2 | T2 emerges immediately after T1. The 9-bit result preserves 510. |
| E7 | 75 ns | 0 | 0 | 17 | 34 | No (input bubble) | 0 | 0 | 510 | — | The E6 input bubble reaches the output. Held value 510 is invalid and ignored. |
| E8 | 85 ns | 0 | 1 | 1 | 2 | Flush probe | 1 | 0 | 510 | — | A probe transaction is accepted to test reset flushing. Held value 510 is ignored. |
| E9 | 95 ns | 1 | 0 | 0 | 0 | No | 0 | 0 | 0 | — | Reset wins over the pending probe, clears it, and clears `sum`. No result 3 may emerge. |
| E10 | 105 ns | 0 | 0 | 0 | 0 | No | 0 | 0 | 0 | — | First edge after the second reset. `out_valid=0` proves the pending probe was flushed; ignore `sum`. |
