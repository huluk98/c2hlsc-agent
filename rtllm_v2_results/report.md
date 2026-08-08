<!-- GENERATED FILE -- do not edit by hand.
     Source run: runs/confirm
     Regenerate: scripts/make_rtllm_v2_results.py --run runs/confirm -->

> **Provenance.** Every number below is this run's own output, copied verbatim from `runs/confirm/report.md`. Configuration: `plan=True`, `evidence_policy=logs`, `max_repair_rounds=2`, `samples=1`. Simulator: Icarus Verilog 12.0 (`iverilog -g2012` + `vvp`); no VCS or Vitis in this environment. The `claude-cli` backend is sandboxed: no file, shell or network tools, plan mode, scrubbed working directory -- the model cannot read the testbench or the reference RTL.
>
> See [`comparison.md`](comparison.md) for the like-for-like table against the shipped GPT archives, and the fairness caveats that table depends on.

# RTLLM v2.0 report

- mode: **llm** -- agent (backend=claude-cli, model=opus)
- benchmark: `/tmp/claude-0/-home-user-c2hlsc-agent/7de59a57-47a4-567b-93a3-a998f30576e9/scratchpad/rtllm`
- generated: 2026-08-05T07:24:54+00:00
- designs: 50 completed of 50 selected
- wall clock: 1099.5s

## Configuration

| setting | value |
| --- | --- |
| apply_shims | `True` |
| compile_timeout | `120` |
| evidence_policy | `logs` |
| llm_retries | `2` |
| max_repair_rounds | `2` |
| oracle_derived_evidence | `False` |
| plan | `True` |
| samples | `1` |
| sim_timeout | `30` |

> No oracle-derived evidence: every prompt was built from the natural-language description, the model's own contract, its own RTL, and tool output from its own runs. The golden RTL and the testbench source appear in no prompt.

## Headline

| metric | raw (all selected) | adjusted (sound oracle only) |
| --- | --- | --- |
| syntax pass (designs) | 50/50 (100.0%) | 43/43 (100.0%) |
| func pass, official oracle (designs) | 46/50 (92.0%) | 40/43 (93.0%) |
| func pass, strict (designs) | 46/50 (92.0%) | 40/43 (93.0%) |
| func pass (samples) | 46/50 (92.0%) | 40/43 (93.0%) |
| func pass, round 0 only (designs) | 34/50 (68.0%) | 31/43 (72.1%) |
| pass@1, with repair (up to 2 repair rounds) | 0.920 | 0.930 |
| pass@1, round 0 (single-shot, RTLLM-comparable) | 0.680 | 0.721 |

Official oracle = the benchmark's own rule (stdout contains `Pass`/`pass`). Strict additionally requires no failure banner, no timeout and no runaway output.
`pass@1, with repair` counts a sample as a success if ANY of its rounds passed, so it is the agent's score, not the base model's. Only `pass@1, round 0` is comparable to a published single-shot RTLLM pass@1.

## Designs

| design | category | syntax | func | repair rounds | failure family |
| --- | --- | --- | --- | --- | --- |
| accu | Arithmetic/Accumulator | 1/1 | 1/1 | 0 | - |
| adder_16bit | Arithmetic/Adder | 1/1 | 1/1 | 0 | - |
| adder_32bit | Arithmetic/Adder | 1/1 | 1/1 | 0 | - |
| adder_8bit | Arithmetic/Adder | 1/1 | 1/1 | 0 | - |
| adder_bcd | Arithmetic/Adder | 1/1 | 1/1 | 0 | - |
| adder_pipe_64bit | Arithmetic/Adder | 1/1 | 1/1 | 1 | - |
| comparator_3bit (vacuous oracle) | Arithmetic/Comparator | 1/1 | 1/1 | 0 | - |
| comparator_4bit (vacuous oracle) | Arithmetic/Comparator | 1/1 | 1/1 | 0 | - |
| div_16bit | Arithmetic/Divider | 1/1 | 1/1 | 0 | - |
| radix2_div (broken oracle) | Arithmetic/Divider | 1/1 | 1/1 | 1 | - |
| multi_16bit | Arithmetic/Multiplier | 1/1 | 1/1 | 0 | - |
| multi_8bit | Arithmetic/Multiplier | 1/1 | 1/1 | 0 | - |
| multi_booth_8bit | Arithmetic/Multiplier | 1/1 | 1/1 | 0 | - |
| multi_pipe_4bit | Arithmetic/Multiplier | 1/1 | 1/1 | 0 | - |
| multi_pipe_8bit | Arithmetic/Multiplier | 1/1 | 1/1 | 0 | - |
| fixed_point_adder | Arithmetic/Other | 1/1 | 1/1 | 0 | - |
| fixed_point_substractor | Arithmetic/Other | 1/1 | 1/1 | 1 | - |
| float_multi | Arithmetic/Other | 1/1 | 1/1 | 0 | - |
| sub_64bit | Arithmetic/Substractor | 1/1 | 1/1 | 0 | - |
| JC_counter | Control/Counter | 1/1 | 1/1 | 0 | - |
| counter_12 | Control/Counter | 1/1 | 1/1 | 0 | - |
| ring_counter (broken oracle) | Control/Counter | 1/1 | 0/1 | 2 | functional_mismatch |
| up_down_counter | Control/Counter | 1/1 | 1/1 | 0 | - |
| fsm | Control/Finite State Machine | 1/1 | 1/1 | 0 | - |
| sequence_detector (vacuous oracle) | Control/Finite State Machine | 1/1 | 1/1 | 1 | - |
| asyn_fifo | Memory/FIFO | 1/1 | 0/1 | 2 | functional_mismatch |
| LIFObuffer | Memory/LIFO | 1/1 | 1/1 | 0 | - |
| LFSR | Memory/Shifter | 1/1 | 1/1 | 1 | - |
| barrel_shifter | Memory/Shifter | 1/1 | 1/1 | 1 | - |
| right_shifter | Memory/Shifter | 1/1 | 1/1 | 0 | - |
| freq_div | Miscellaneous/Frequency divider | 1/1 | 1/1 | 0 | - |
| freq_divbyeven | Miscellaneous/Frequency divider | 1/1 | 1/1 | 1 | - |
| freq_divbyfrac | Miscellaneous/Frequency divider | 1/1 | 1/1 | 1 | - |
| freq_divbyodd | Miscellaneous/Frequency divider | 1/1 | 1/1 | 1 | - |
| calendar | Miscellaneous/Others | 1/1 | 1/1 | 0 | - |
| edge_detect | Miscellaneous/Others | 1/1 | 1/1 | 0 | - |
| parallel2serial | Miscellaneous/Others | 1/1 | 1/1 | 0 | - |
| pulse_detect | Miscellaneous/Others | 1/1 | 0/1 | 2 | functional_mismatch |
| serial2parallel | Miscellaneous/Others | 1/1 | 0/1 | 2 | timeout |
| synchronizer | Miscellaneous/Others | 1/1 | 1/1 | 0 | - |
| traffic_light | Miscellaneous/Others | 1/1 | 1/1 | 0 | - |
| width_8to16 | Miscellaneous/Others | 1/1 | 1/1 | 0 | - |
| RAM | Miscellaneous/RISC-V | 1/1 | 1/1 | 0 | - |
| ROM | Miscellaneous/RISC-V | 1/1 | 1/1 | 0 | - |
| alu | Miscellaneous/RISC-V | 1/1 | 1/1 | 1 | - |
| clkgenerator (broken oracle) | Miscellaneous/RISC-V | 1/1 | 1/1 | 1 | - |
| instr_reg | Miscellaneous/RISC-V | 1/1 | 1/1 | 0 | - |
| pe | Miscellaneous/RISC-V | 1/1 | 1/1 | 0 | - |
| signal_generator | Miscellaneous/Signal generation | 1/1 | 1/1 | 1 | - |
| square_wave (vacuous oracle) | Miscellaneous/Signal generation | 1/1 | 1/1 | 0 | - |

## Failure families (samples)

| family | samples |
| --- | --- |
| functional_mismatch | 3 |
| timeout | 1 |

## Caveats

These selected designs have a **broken oracle**: the benchmark's own `verified_*.v` fails their testbench under this simulator, so no RTL can score. They are counted in `totals` and excluded from `adjusted`:

- `clkgenerator`: Upstream oracle bug: the benchmark's own verified_clkgenerator.v fails its own testbench under iverilog ('Test completed with 20 failures'), so no RTL can score.
- `radix2_div`: Upstream oracle bug: the benchmark's own verified_radix2_div.v fails its own testbench under iverilog (3 'Error: dividend=...' lines then '===========Failed==========='), so no RTL can score.
- `ring_counter`: Simulator-ordering bug: the testbench's two always @(posedge clk) blocks race, and iverilog runs the 'i = i + 1' block before the 'if (i == 9)' pass check, so the banner never prints -- the reference RTL matches all 10 expected values (no 'Failed at' line) yet the run ends silently at t=100 and scores 0.

These selected designs have a **vacuous oracle**: a module with the right ports and no logic at all passes them, so every agent -- including one that emits an empty module -- banks them for free. They are counted in `totals` and excluded from `adjusted` (but NOT from `adjusted_unpassable_only`). Re-measure with `--empty-baseline`:

- `comparator_3bit`: X-optimistic oracle: with all outputs undriven the testbench's combined check condition evaluates to X, the error counter stays 0 and the pass banner prints. An empty module scores a strict pass.
- `comparator_4bit`: X-optimistic oracle: same combined check as comparator_3bit. An empty module scores a strict pass.
- `sequence_detector`: X-optimistic oracle: 'if (!sequence_detected) error = error + 1' never fires while sequence_detected is X. An empty module scores a strict pass.
- `square_wave`: X-optimistic oracle: 'if (wave_out_tb == 1)' is X for an undriven output, so the consecutive-ones check never runs. An empty module scores a strict pass.

**Shimmed testbenches.** These designs run against a rewritten *copy* of `testbench.v` (SystemVerilog that iverilog rejects, translated to equivalent Verilog-2001; no check is weakened). Disable with `--no-shims` to see the unshimmed verdict:

- `asyn_fifo` (applied): Names the initial block that drives the write burst so the loop can be left early without SystemVerilog's 'break'. iverilog rejects 'break' ('sorry: break statements not supported'). Nothing follows the repeat inside that block, so disabling the enclosing named block is exactly 'leave the loop' and preserves the write sequence.
- `ring_counter` (applied): iverilog rejects the SystemVerilog array declaration initializer ('sorry: Assignment to an entire array or to an array slice is not yet supported'); the same ten values are assigned in order from an initial block, which still settles at time 0, well before the first posedge at t=5.

- Scores are testbench-bounded: they say the design passed the benchmark's stimulus, not that it is equivalent to the specification over all inputs.
- Run the same selection with `--reference` to get the oracle baseline; a design the reference cannot pass is a harness failure, not a model failure.
