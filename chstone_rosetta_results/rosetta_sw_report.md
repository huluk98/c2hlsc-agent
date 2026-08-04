# Rosetta software-path run — c2hlsc-agent

apps: 5 · built: 5 · ran: 5 · judged: 3 · **passed: 1/3** · 6.3s

> **Coverage.** Software path only. No HLS synthesis, SDAccel or SDSoC step was attempted and no claim is made about them. 'passed' is out of the apps with a shipped golden output, not out of all apps.

| app | built | ran | oracle | verdict | measured | expected | seconds |
| --- | :-: | :-: | --- | :-: | :-: | :-: | --: |
| `3d-rendering` | Y | Y | golden_file | FAIL | - | - | 1.75 |
| `digit-recognition` | Y | Y | golden_file | FAIL | 1870/2000 | 1878/2000 | 2.03 |
| `face-detection` | Y | Y | golden_file | PASS | - | - | 1.82 |
| `optical-flow` | Y | Y | no_trustworthy_oracle | - | - | - | 4.57 |
| `spam-filter` | Y | Y | no_trustworthy_oracle | - | - | - | 2.04 |

## No trustworthy oracle

These apps ship no `outputs_golden.txt`. They are excluded from the denominator; an exit code proves only that the program did not crash:

- `optical-flow`
- `spam-filter`
