# Rosetta agent-rung run — c2hlsc-agent

Mode: `agent` · generator: `llm` · apps: 5 · passed: **0/5** · 1445.8s

> **Ladder coverage.** Only host software equivalence (rung 1 of 4) can run without Xilinx tooling. HLS synthesis, SDAccel and SDSoC were NOT attempted and no claim is made about them. 'passed' means the generated HLS-C matched the original src/sw kernel on the generated stimulus -- not that anything was synthesized.

| app | top | rung reached | ok | failure family | seconds |
| --- | --- | --- | :-: | --- | --: |
| `3d-rendering` | `rendering_sw` | generated | FAIL | multidim_array_arg_unsupported | 788.84 |
| `digit-recognition` | `DigitRec_sw` | generated | FAIL | top_signature_misparsed | 646.79 |
| `face-detection` | `face_detect_sw` | generated | FAIL | top_signature_misparsed | 777.19 |
| `optical-flow` | `optical_flow_sw` | generated | FAIL | top_signature_misparsed | 798.97 |
| `spam-filter` | `SgdLR_sw` | generated | FAIL | top_signature_misparsed | 440.97 |

## Failure families (first blocking wall per app)

| family | n |
| --- | :-: |
| `top_signature_misparsed` | 4 |
| `multidim_array_arg_unsupported` | 1 |

## Every wall the compiler reported

Apps commonly stop on more than one independent limitation, so fixing only the first would not move them. Read these counts as a floor: an error in `src/hls_top.hpp` aborts that translation unit, which can mask further walls in the same app.

| wall | n |
| --- | :-: |
| `top_signature_misparsed` | 4 |
| `struct_arg_stimulus_unsupported` | 2 |
| `multidim_array_arg_unsupported` | 1 |
| `generated_header_missing_app_types` | 1 |

## Diagnostics, quoted

- `3d-rendering` — multidim_array_arg_unsupported: tb/testbench.cpp:109:40: error: cannot convert ‘bit8*’ {aka ‘unsigned char*’} to ‘bit8 (*)[256]’ {aka ‘unsigned char (*)[256]’}
- `3d-rendering` — struct_arg_stimulus_unsupported: tb/testbench.cpp:53:29: error: no matching function for call to ‘Triangle_3D::Triangle_3D(int)’
- `3d-rendering` — generated_header_missing_app_types: src/hls_top.hpp:6:6: error: variable or field ‘rendering_sw’ declared void
- `digit-recognition` — top_signature_misparsed: converter emitted return type 'sw top function void' for a top declared 'void'; tb/../src/hls_top.hpp:6:1: error: ‘sw’ does not name a type; did you mean ‘SW’?
- `face-detection` — top_signature_misparsed: converter emitted return type 'level function void' for a top declared 'void'; tb/../src/hls_top.hpp:6:1: error: ‘level’ does not name a type
- `optical-flow` — top_signature_misparsed: converter emitted return type 'level sw function void' for a top declared 'void'; tb/../src/hls_top.hpp:6:1: error: ‘level’ does not name a type
- `optical-flow` — struct_arg_stimulus_unsupported: tb/testbench.cpp:56:29: error: no matching function for call to ‘velocity_t::velocity_t(int)’
- `spam-filter` — top_signature_misparsed: converter emitted return type 'level function void' for a top declared 'void'; tb/../src/hls_top.hpp:6:1: error: ‘level’ does not name a type
