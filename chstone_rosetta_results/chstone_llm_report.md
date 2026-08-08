# CHStone run — c2hlsc-agent

Mode: `agent` · arm: `llm_staged_r1` · benchmarks: 12 · passed: **8/12** · 2346.3s

## Configuration

| setting | value |
| --- | --- |
| `label` | `llm_staged_r1` |
| `generator` | `llm` |
| `llm_backend` | `claude-cli` |
| `llm_model` | `opus` |
| `staging` | `golden_c_tu` |
| `repair_rounds_allowed` | `1` |
| `max_iterations` | `2` |
| `auto_repair` | `True` |
| `relax_narrowing` | `True` |
| `mutation_check` | `True` |
| `keep_going` | `True` |

**Reachable** (candidate actually reached the oracle): 12/12; passed of reachable: 8. A benchmark blocked by the harness is reported as unreachable, not as a zero.

> **Ladder coverage.** Only host software equivalence (rung 1 of 4) can run without vitis_hls. CSim, CSynth and C/RTL CoSim were NOT attempted and no claim is made about them.

| benchmark | rung reached | ok | reachable | mutation check | stimuli | failure family | seconds |
| --- | --- | :-: | :-: | :-: | --: | --- | --: |
| `adpcm` | host_equivalence | PASS | yes | red | 100 | - | 645.19 |
| `aes` | host_equivalence | PASS | yes | red | 100 | - | 403.88 |
| `blowfish` | generated | FAIL | yes | - | - | candidate_includes_original_c | 902.66 |
| `dfadd` | host_equivalence | PASS | yes | red | 100 | - | 688.67 |
| `dfdiv` | host_equivalence | PASS | yes | red | 100 | - | 669.95 |
| `dfmul` | host_equivalence | PASS | yes | red | 100 | - | 604.49 |
| `dfsin` | host_equivalence | PASS | yes | red | 100 | - | 1160.8 |
| `gsm` | host_equivalence | PASS | yes | red | 100 | - | 682.1 |
| `jpeg` | generated | FAIL | yes | - | - | generated_hlsc_does_not_compile | 369.08 |
| `mips` | generated | FAIL | yes | - | - | generated_hlsc_does_not_compile | 566.68 |
| `motion` | generated | FAIL | yes | - | - | candidate_includes_original_c | 903.37 |
| `sha` | host_equivalence | PASS | yes | red | 100 | - | 377.25 |

## Failure families

| family | n |
| --- | :-: |
| `candidate_includes_original_c` | 2 |
| `generated_hlsc_does_not_compile` | 2 |
