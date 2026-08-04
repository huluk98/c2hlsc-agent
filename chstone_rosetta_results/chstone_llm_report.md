# CHStone run — c2hlsc-agent

Mode: `agent` · benchmarks: 12 · passed: **6/12** · 3060.3s

> **Ladder coverage.** Only host software equivalence (rung 1 of 4) can run without vitis_hls. CSim, CSynth and C/RTL CoSim were NOT attempted and no claim is made about them.

| benchmark | rung reached | ok | failure family | seconds |
| --- | --- | :-: | --- | --: |
| `adpcm` | generated | FAIL | original_c_not_valid_cpp | 1177.05 |
| `aes` | host_equivalence | PASS | - | 1042.61 |
| `blowfish` | generated | FAIL | original_c_not_valid_cpp | 1003.38 |
| `dfadd` | host_equivalence | PASS | - | 1082.43 |
| `dfdiv` | host_equivalence | PASS | - | 971.4 |
| `dfmul` | host_equivalence | PASS | - | 791.72 |
| `dfsin` | host_equivalence | PASS | - | 516.63 |
| `gsm` | host_equivalence | PASS | - | 1233.9 |
| `jpeg` | generated | FAIL | original_c_not_valid_cpp | 456.83 |
| `mips` | generated | FAIL | golden_candidate_symbol_collision | 670.93 |
| `motion` | generated | FAIL | original_c_not_valid_cpp | 1085.52 |
| `sha` | generated | FAIL | generated_hlsc_does_not_compile | 599.04 |

## Failure families

| family | n |
| --- | :-: |
| `original_c_not_valid_cpp` | 4 |
| `golden_candidate_symbol_collision` | 1 |
| `generated_hlsc_does_not_compile` | 1 |
