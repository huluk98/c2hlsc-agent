# CHStone run — c2hlsc-agent

Mode: `agent` · benchmarks: 4 · passed: **2/4** · 685.1s

> **Ladder coverage.** Only host software equivalence (rung 1 of 4) can run without vitis_hls. CSim, CSynth and C/RTL CoSim were NOT attempted and no claim is made about them.

| benchmark | rung reached | ok | failure family | seconds |
| --- | --- | :-: | --- | --: |
| `dfadd` | host_equivalence | PASS | - | 683.49 |
| `dfmul` | host_equivalence | PASS | - | 685.1 |
| `mips` | generated | FAIL | generated_hlsc_does_not_compile | 535.05 |
| `sha` | generated | FAIL | generated_hlsc_does_not_compile | 534.52 |

## Failure families

| family | n |
| --- | :-: |
| `generated_hlsc_does_not_compile` | 2 |
