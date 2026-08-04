# CHStone run — c2hlsc-agent

Mode: `agent` · benchmarks: 12 · passed: **0/12** · 3.0s

> **Ladder coverage.** Only host software equivalence (rung 1 of 4) can run without vitis_hls. CSim, CSynth and C/RTL CoSim were NOT attempted and no claim is made about them.

| benchmark | rung reached | ok | failure family | seconds |
| --- | --- | :-: | --- | --: |
| `adpcm` | generated | FAIL | generated_hlsc_does_not_compile | 1.41 |
| `aes` | generated | FAIL | generated_hlsc_does_not_compile | 1.03 |
| `blowfish` | generated | FAIL | generated_hlsc_does_not_compile | 0.98 |
| `dfadd` | generated | FAIL | generated_hlsc_does_not_compile | 1.04 |
| `dfdiv` | generated | FAIL | generated_hlsc_does_not_compile | 0.83 |
| `dfmul` | generated | FAIL | generated_hlsc_does_not_compile | 0.84 |
| `dfsin` | generated | FAIL | generated_hlsc_does_not_compile | 0.94 |
| `gsm` | generated | FAIL | generated_hlsc_does_not_compile | 0.79 |
| `jpeg` | generated | FAIL | generated_hlsc_does_not_compile | 0.83 |
| `mips` | generated | FAIL | generated_hlsc_does_not_compile | 0.82 |
| `motion` | generated | FAIL | generated_hlsc_does_not_compile | 0.89 |
| `sha` | generated | FAIL | generated_hlsc_does_not_compile | 0.77 |

## Failure families

| family | n |
| --- | :-: |
| `generated_hlsc_does_not_compile` | 12 |
