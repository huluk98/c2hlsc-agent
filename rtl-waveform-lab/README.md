# RTL waveform laboratory: one-cycle delayed adder

This small project teaches clock edges, synchronous reset, input sampling, registered state, valid signals, pipeline latency, bubbles, consecutive transactions, unsigned bus arithmetic, and invalid data. The clean WaveDrom picture and the real Icarus Verilog VCD describe the same E0–E10 test.

The self-contained study homepage is `docs/study.html`. It combines the three-day reading plan, verified edge table, signal explanations, saved progress, and a self-grading ten-question quiz.

## Quick start

Run these commands from this directory:

```sh
cd "/Users/luke/Documents/C2HLSC agent/rtl-waveform-lab"
./scripts/check_environment.sh
make all
make wave
```

To use the study homepage in a browser, run `make serve` and open:

```text
http://127.0.0.1:4173/docs/study.html
```

`make wave` runs the simulation first, then opens `build/wave.vcd` in GTKWave with the supplied signal layout. If you already ran `make all`, the compile target is up to date but the simulation intentionally runs again to refresh the trace.

## Make targets

- `make sim` compiles SystemVerilog 2012 with `iverilog -g2012 -Wall`, runs it with `vvp`, executes the scoreboard, and produces `build/wave.vcd` plus `build/cycle_trace.csv`.
- `make docs` validates `docs/expected_waveform.json`. If a local WaveDrom CLI exists it also creates `docs/expected_waveform.svg`; otherwise open `docs/expected_waveform.html`, which renders the same WaveJSON using pinned browser scripts.
- `make all` runs `make sim` and `make docs`.
- `make wave` opens the VCD with `docs/one_cycle_delayed_adder.gtkw`.
- `make serve` starts the local study website at `http://127.0.0.1:4173/docs/study.html`.
- `make clean` removes generated simulator, VCD, CSV, and SVG files but keeps all source and hand-written documentation.

## What to inspect first

1. Locate rising edges E2 and E3: T0 is sampled at E2 and appears as `sum=8`, `out_valid=1` at E3.
2. Compare E3 and E4: `in_valid=0` at E3 creates the output bubble (`out_valid=0`) at E4. The held `sum=8` at E4 must be ignored.
3. Compare E4/E5 inputs with E5/E6 outputs: consecutive T1 and T2 inputs become consecutive results 300 and 510.

GTKWave shows real simulator times rather than textual E-labels. With the 10 ns clock, E0 is 5 ns, E1 is 15 ns, and each next label is another 10 ns. Use the cycle table to translate quickly.

## Files

- `rtl/one_cycle_delayed_adder.sv`: synthesizable DUT; no delays or initial blocks.
- `tb/tb_one_cycle_delayed_adder.sv`: falling-edge stimulus, independent scoreboard, VCD, and CSV trace generation.
- `docs/expected_waveform.json`: clean WaveDrom source of truth.
- `docs/expected_waveform.html`: browser-rendered WaveDrom view.
- `docs/study.html`: self-contained study homepage and interactive quiz.
- `docs/cycle_table.md`: every rising edge with exact expected values.
- `docs/waveform_walkthrough.md`: beginner tutorial.
- `docs/reading_quiz.md`: ten questions and a separate answer section.
- `docs/one_cycle_delayed_adder.gtkw`: GTKWave signal order and display setup.

## Expected PASS line

```text
PASS: all 11 rising-edge checks succeeded; one-cycle latency, bubbles, consecutive transactions, 9-bit arithmetic, and reset flush verified.
```

The testbench exits nonzero with edge number, sampled inputs, expected outputs, and actual outputs if any scoreboard comparison fails. Do not trust a VCD from a run that lacks the PASS line.
