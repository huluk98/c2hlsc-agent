# CHStone run — c2hlsc-agent

Mode: `native` · arm: `native` · benchmarks: 12 · passed: **12/12** · 0.9s

## Configuration

| setting | value |
| --- | --- |
| `label` | `native` |
| `generator` | `deterministic` |
| `llm_backend` | `None` |
| `llm_model` | `None` |
| `staging` | `golden_c_tu` |
| `repair_rounds_allowed` | `0` |
| `max_iterations` | `2` |
| `auto_repair` | `False` |
| `relax_narrowing` | `True` |
| `mutation_check` | `True` |
| `keep_going` | `True` |

**Reachable** (candidate actually reached the oracle): 12/12; passed of reachable: 12. A benchmark blocked by the harness is reported as unreachable, not as a zero.

> **Ladder coverage.** Only host software equivalence (rung 1 of 4) can run without vitis_hls. CSim, CSynth and C/RTL CoSim were NOT attempted and no claim is made about them.

| benchmark | rung reached | ok | reachable | mutation check | stimuli | failure family | seconds |
| --- | --- | :-: | :-: | :-: | --: | --- | --: |
| `adpcm` | native_pass | PASS | yes | - | - | - | 0.44 |
| `aes` | native_pass | PASS | yes | - | - | - | 0.54 |
| `blowfish` | native_pass | PASS | yes | - | - | - | 0.44 |
| `dfadd` | native_pass | PASS | yes | - | - | - | 0.43 |
| `dfdiv` | native_pass | PASS | yes | - | - | - | 0.11 |
| `dfmul` | native_pass | PASS | yes | - | - | - | 0.11 |
| `dfsin` | native_pass | PASS | yes | - | - | - | 0.17 |
| `gsm` | native_pass | PASS | yes | - | - | - | 0.14 |
| `jpeg` | native_pass | PASS | yes | - | - | - | 0.34 |
| `mips` | native_pass | PASS | yes | - | - | - | 0.09 |
| `motion` | native_pass | PASS | yes | - | - | - | 0.09 |
| `sha` | native_pass | PASS | yes | - | - | - | 0.18 |
