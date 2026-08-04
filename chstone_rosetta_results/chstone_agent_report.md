# CHStone run — c2hlsc-agent

Mode: `agent` · benchmarks: 12 · passed: **0/12** · 3.9s

> **Ladder coverage.** Only host software equivalence (rung 1 of 4) can run without vitis_hls. CSim, CSynth and C/RTL CoSim were NOT attempted and no claim is made about them.

| benchmark | rung reached | ok | failure family | seconds |
| --- | --- | :-: | --- | --: |
| `adpcm` | generated | FAIL | original_c_not_valid_cpp | 2.01 |
| `aes` | generated | FAIL | golden_candidate_symbol_collision | 1.29 |
| `blowfish` | generated | FAIL | original_c_not_valid_cpp | 1.05 |
| `dfadd` | generated | FAIL | golden_candidate_symbol_collision | 1.18 |
| `dfdiv` | generated | FAIL | golden_candidate_symbol_collision | 1.17 |
| `dfmul` | generated | FAIL | golden_candidate_symbol_collision | 1.13 |
| `dfsin` | generated | FAIL | golden_candidate_symbol_collision | 1.17 |
| `gsm` | generated | FAIL | generated_hlsc_does_not_compile | 0.99 |
| `jpeg` | generated | FAIL | original_c_not_valid_cpp | 0.91 |
| `mips` | generated | FAIL | generated_hlsc_does_not_compile | 0.96 |
| `motion` | generated | FAIL | original_c_not_valid_cpp | 1.13 |
| `sha` | generated | FAIL | generated_hlsc_does_not_compile | 0.9 |

## Failure families

| family | n |
| --- | :-: |
| `golden_candidate_symbol_collision` | 5 |
| `original_c_not_valid_cpp` | 4 |
| `generated_hlsc_does_not_compile` | 3 |
