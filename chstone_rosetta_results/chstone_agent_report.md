# CHStone run — c2hlsc-agent

Mode: `agent` · benchmarks: 12 · passed: **0/12** · 1.4s

> **Ladder coverage.** Only host software equivalence (rung 1 of 4) can run without vitis_hls. CSim, CSynth and C/RTL CoSim were NOT attempted and no claim is made about them.

| benchmark | rung reached | ok | failure family | seconds |
| --- | --- | :-: | --- | --: |
| `adpcm` | generated | FAIL | generated_hlsc_does_not_compile | 1.36 |
| `aes` | generated | FAIL | generated_hlsc_does_not_compile | 0.46 |
| `blowfish` | generated | FAIL | generated_hlsc_does_not_compile | 0.49 |
| `dfadd` | generated | FAIL | generated_hlsc_does_not_compile | 0.45 |
| `dfdiv` | generated | FAIL | generated_hlsc_does_not_compile | 0.27 |
| `dfmul` | generated | FAIL | generated_hlsc_does_not_compile | 0.32 |
| `dfsin` | generated | FAIL | generated_hlsc_does_not_compile | 0.28 |
| `gsm` | generated | FAIL | generated_hlsc_does_not_compile | 0.28 |
| `jpeg` | generated | FAIL | generated_hlsc_does_not_compile | 0.31 |
| `mips` | generated | FAIL | generated_hlsc_does_not_compile | 0.4 |
| `motion` | generated | FAIL | generated_hlsc_does_not_compile | 0.26 |
| `sha` | generated | FAIL | generated_hlsc_does_not_compile | 0.27 |

## Failure families

| family | n |
| --- | :-: |
| `generated_hlsc_does_not_compile` | 12 |
