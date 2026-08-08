# CHStone run — c2hlsc-agent

Mode: `agent` · arm: `det_staged_r1` · benchmarks: 12 · passed: **6/12** · 5.8s

## Configuration

| setting | value |
| --- | --- |
| `label` | `det_staged_r1` |
| `generator` | `deterministic` |
| `llm_backend` | `None` |
| `llm_model` | `None` |
| `staging` | `golden_c_tu` |
| `repair_rounds_allowed` | `1` |
| `max_iterations` | `2` |
| `auto_repair` | `True` |
| `relax_narrowing` | `True` |
| `mutation_check` | `True` |
| `keep_going` | `True` |

**Reachable** (candidate actually reached the oracle): 12/12; passed of reachable: 6. A benchmark blocked by the harness is reported as unreachable, not as a zero.

> **Ladder coverage.** Only host software equivalence (rung 1 of 4) can run without vitis_hls. CSim, CSynth and C/RTL CoSim were NOT attempted and no claim is made about them.

| benchmark | rung reached | ok | failure family | seconds | reachable | mutation check | stimuli |
| --- | --- | :-: | --- | --: | :-: | :-: | --: |
| `adpcm` | host_equivalence | PASS | - | 2.7 | yes | red | 100 |
| `aes` | host_equivalence | PASS | - | 2.4 | yes | red | 100 |
| `blowfish` | generated | FAIL | candidate_includes_original_c | 1.7 | yes | - | - |
| `dfadd` | host_equivalence | PASS | - | 2.34 | yes | red | 100 |
| `dfdiv` | host_equivalence | PASS | - | 2.01 | yes | red | 100 |
| `dfmul` | host_equivalence | PASS | - | 2.02 | yes | red | 100 |
| `dfsin` | host_equivalence | PASS | - | 2.19 | yes | red | 100 |
| `gsm` | generated | FAIL | generated_hlsc_does_not_compile | 0.83 | yes | - | - |
| `jpeg` | generated | FAIL | generated_hlsc_does_not_compile | 0.9 | yes | - | - |
| `mips` | generated | FAIL | generated_hlsc_does_not_compile | 0.83 | yes | - | - |
| `motion` | generated | FAIL | candidate_includes_original_c | 1.46 | yes | - | - |
| `sha` | generated | FAIL | generated_hlsc_does_not_compile | 0.78 | yes | - | - |

## Failure families

| family | n |
| --- | :-: |
| `generated_hlsc_does_not_compile` | 4 |
| `candidate_includes_original_c` | 2 |
