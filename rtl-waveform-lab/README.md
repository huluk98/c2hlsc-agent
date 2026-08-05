# RTL waveform laboratory: one-cycle delayed adder

This project teaches an ordered core: specification and widths; clock edges and nonblocking state; synchronous reset; valid-only transactions; latency, bubbles, throughput, and reset flush; independent checking; lint; and generic RTL synthesis. The clean WaveDrom picture and Icarus Verilog VCD describe the same E0–E10 test.

The study homepage is `docs/study.html`. It combines the three-day route, a private on-device full-PDF shelf, verified edge table, signal explanations, saved progress, and a self-grading ten-question waveform quiz. The quiz is one checkpoint, not proof of generalized RTL or HLS competence.

Three focused days can build foundational competence with small, single-clock designs if you already have basic programming and algebra. It cannot cover the entire current RTL/HLS field. Use these four documents in order:

1. `docs/pdf_reading_map.md` — the supplied textbook/paper readings mapped to specific exercise artifacts.
2. `docs/fundamentals_field_guide.md` — dependency map, vocabulary, evidence ladder, scope, and version-aware primary references.
3. `docs/ordered_exercises.md` — specified inputs, required outputs, and clearly labeled local/manual/external gates.
4. `docs/generation_contract.md` — reusable precise prompt plus the filled black-box contract for this DUT.

## Quick start

Run these commands from this directory:

```sh
cd "/Users/luke/Documents/C2HLSC agent/rtl-waveform-lab"
./scripts/check_environment.sh
make verify
make wave
```

To use the study homepage in a browser, run `make serve` and open:

```text
http://127.0.0.1:4173/docs/study.html
```

`make verify` is the local acceptance gate. `make wave` runs the directed simulation first, then opens `build/wave.vcd` in GTKWave with the supplied signal layout.

## Make targets

- `make sim` runs the black-box E0–E10 scoreboard and produces `build/wave.vcd` plus `build/cycle_trace.csv`.
- `make exhaustive` streams all 65,536 unsigned operand pairs and also checks exact latency, throughput, bubbles, synchronous reset timing/priority, and flush behavior.
- `make mutations` proves oracle sensitivity by requiring six deliberately faulty DUTs to fail.
- `make lint` runs strict Verilator lint.
- `make synth` performs generic Yosys lowering, optimization, structural checking, and statistics; outputs are `build/synth.log` and `build/synth.json`.
- `make docs` compares the generated trace with the cycle table and study table, compares embedded and standalone WaveJSON, and checks quiz/link/JavaScript structure. If a WaveDrom CLI exists it also creates `docs/expected_waveform.svg`.
- `make verify` requires environment, lint, directed simulation, exhaustive simulation, synthesis, mutation, and documentation gates to pass.
- `make all` runs `make sim` and `make docs`.
- `make wave` opens the VCD with `docs/one_cycle_delayed_adder.gtkw`.
- `make serve` starts the local study website at `http://127.0.0.1:4173/docs/study.html`.
- `make clean` removes generated simulator, trace, synthesis, mutation-log, and SVG files but keeps all source and hand-written documentation.

## What to inspect first

1. Locate rising edges E2 and E3: T0 is sampled at E2 and appears as `sum=8`, `out_valid=1` at E3.
2. Compare E3 and E4: `in_valid=0` at E3 creates the output bubble (`out_valid=0`) at E4. The held `sum=8` at E4 must be ignored.
3. Compare E4/E5 inputs with E5/E6 outputs: consecutive T1 and T2 inputs become consecutive results 300 and 510.

GTKWave shows real simulator times rather than textual E-labels. With the 10 ns clock, E0 is 5 ns, E1 is 15 ns, and each next label is another 10 ns. Use the cycle table to translate quickly.

## Files

- `rtl/one_cycle_delayed_adder.sv`: synthesizable DUT; no delays or initial blocks.
- `tb/tb_one_cycle_delayed_adder.sv`: falling-edge stimulus, independent scoreboard, VCD, and CSV trace generation.
- `docs/expected_waveform.json`: standalone clean WaveDrom data checked against the trace.
- `docs/expected_waveform.html`: browser-rendered WaveDrom view.
- `docs/study.html`: waveform-centered study homepage and interactive quiz.
- `docs/pdf_shelf.js`: device-local PDF validation, IndexedDB storage, selection, viewing, download, and explicit removal.
- `docs/generation_contract.md`: exact reusable input contract and acceptance evidence.
- `docs/ordered_exercises.md`: three-day inputs, outputs, and machine/manual mastery gates.
- `docs/fundamentals_field_guide.md`: ordered field map, HLS boundary, limitations, and primary sources.
- `docs/pdf_reading_map.md`: verified HLS-paper reading plus the original textbook section map and edition caveat.
- `docs/cycle_table.md`: every rising edge with exact expected values.
- `docs/waveform_walkthrough.md`: beginner tutorial.
- `docs/reading_quiz.md`: ten questions and a separate answer section.
- `docs/one_cycle_delayed_adder.gtkw`: GTKWave signal order and display setup.

## Expected PASS line

```text
PASS: all 11 rising-edge checks succeeded; one-cycle latency, bubbles, consecutive transactions, 9-bit arithmetic, and reset flush verified.
```

The testbench exits nonzero with the failing edge and external mismatch. It never requires a particular internal register name or value, and it ignores `sum` during invalid non-reset cycles as the interface requires. Do not trust a VCD from a run that lacks the PASS line.

## Evidence boundary

Passing `make verify` proves the checked behavior of this exact valid-only, unsigned, one-cycle adder under the installed Icarus, Verilator, and Yosys versions. It does not prove placed-and-routed timing, setup/hold, CDC safety, power, FPGA-specific resources, formal completeness, AXI/ready-valid behavior, HLS scheduling, or C/RTL CoSim. Those require separate contracts and real tool reports; the field guide shows where they enter the workflow.

The main study page is offline-safe. The optional `docs/expected_waveform.html` helper loads pinned WaveDrom JavaScript from a public CDN and therefore needs network access and executes third-party code; prefer the checked JSON, local `make wave`, or a locally installed WaveDrom CLI when working offline or under a strict trust policy.

PDFs added through the study page are stored as full Blob data in browser IndexedDB for the exact `http://127.0.0.1:4173` origin; their metadata and bytes are not sent to this Python server. Keep the original files. A different browser profile, `localhost` instead of `127.0.0.1`, a different port, private browsing, storage eviction, or clearing site data has a separate or empty shelf.
