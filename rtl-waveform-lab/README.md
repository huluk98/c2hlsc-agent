# RTL waveform laboratory: one-cycle delayed adder

This project teaches an ordered core: specification and widths; clock edges and nonblocking state; synchronous reset; valid-only transactions; latency, bubbles, throughput, and reset flush; independent checking; lint; and generic RTL synthesis. The clean WaveDrom picture and Icarus Verilog VCD describe the same E0–E10 test.

The study homepage is `docs/study.html`. It combines the three-day route, a private on-device full-PDF shelf, opt-in loopback PDF text analysis, learner-confirmed source synchronization, one-click reading locators, deterministic code/exercise starters, a ready-to-fill output worksheet, verified edge table, signal explanations, saved progress, and a self-grading ten-question waveform quiz. The quiz is one checkpoint, not proof of generalized RTL or HLS competence.

Three focused days can build foundational competence with small, single-clock designs if you already have basic programming and algebra. It cannot cover the entire current RTL/HLS field. Use these four documents in order:

1. `docs/pdf_reading_map.md` — an ASCII-safe, just-in-time Read/Extract/Artifact/Gate workflow covering every stage.
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

Use that exact address. `make serve` runs the repository's loopback-only study server, which is required for the optional local PDF analyzer. A plain static server can display the shelf and lesson but cannot service the analyzer API. Selecting or opening a shelf PDF sends nothing anywhere; analysis begins only after you check the per-day consent box and click **Analyze selected PDF locally**.

`make verify` is the local acceptance gate. `make wave` runs the directed simulation first, then opens `build/wave.vcd` in GTKWave with the supplied signal layout.

## Make targets

- `make sim` runs the black-box E0–E10 scoreboard and produces `build/wave.vcd` plus `build/cycle_trace.csv`.
- `make exhaustive` streams all 65,536 unsigned operand pairs and also checks exact latency, throughput, bubbles, synchronous reset timing/priority, and flush behavior.
- `make mutations` proves oracle sensitivity by requiring six deliberately faulty DUTs to fail.
- `make pdf-analyzer-test` checks deterministic page ranking, fixed starter selection, the loopback API, one-use tokens, the static allowlist, and the supplied HLStrans integration route.
- `make lint` runs strict Verilator lint.
- `make synth` performs generic Yosys lowering, optimization, structural checking, and statistics; outputs are `build/synth.log` and `build/synth.json`.
- `make docs` compares the generated trace with the cycle table and study table, compares embedded and standalone WaveJSON, and checks quiz/link/JavaScript/Python contract structure. If a WaveDrom CLI exists it also creates `docs/expected_waveform.svg`.
- `make verify` requires environment, lint, directed simulation, exhaustive simulation, synthesis, mutation, PDF-analyzer tests, and documentation gates to pass.
- `make all` runs `make sim` and `make docs`.
- `make wave` opens the VCD with `docs/one_cycle_delayed_adder.gtkw`.
- `make serve` starts the custom loopback-only study server at `http://127.0.0.1:4173/docs/study.html`; it serves an allowlisted local tree and the explicit-opt-in PDF-analysis API.
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
- `docs/reading_coach.js`: tested source-sync state, exactly two readings plus two trusted video/guided runs per day, output worksheet generation, and the privacy-safe arbitrary-PDF Codex handoff prompt.
- `docs/pdf_shelf.js`: device-local PDF validation, IndexedDB storage, selection, viewing, opt-in local analysis, unconfirmed-candidate application, download, and explicit removal.
- `docs/generation_contract.md`: exact reusable input contract and acceptance evidence.
- `docs/ordered_exercises.md`: three-day inputs, outputs, and machine/manual mastery gates.
- `docs/fundamentals_field_guide.md`: ordered field map, HLS boundary, limitations, and primary sources.
- `docs/pdf_reading_map.md`: ASCII-safe, stage-by-stage read/understand/produce/pass workflow for the textbook, HLStrans paper, and exact-version vendor material.
- `docs/cycle_table.md`: every rising edge with exact expected values.
- `docs/waveform_walkthrough.md`: beginner tutorial.
- `docs/reading_quiz.md`: ten questions and a separate answer section.
- `docs/one_cycle_delayed_adder.gtkw`: GTKWave signal order and display setup.
- `scripts/pdf_study_analyzer.py`: bounded Poppler text extraction, deterministic day-specific page scoring, and fixed starter templates.
- `scripts/study_server.py`: loopback-only static server and one-use-token PDF-analysis API.
- `scripts/test_pdf_study_analyzer.py`: deterministic analyzer and starter-selection checks.
- `scripts/test_study_server.py`: loopback, token, static-serving, and real-PDF integration checks.

## Expected PASS line

```text
PASS: all 11 rising-edge checks succeeded; one-cycle latency, bubbles, consecutive transactions, 9-bit arithmetic, and reset flush verified.
```

The testbench exits nonzero with the failing edge and external mismatch. It never requires a particular internal register name or value, and it ignores `sum` during invalid non-reset cycles as the interface requires. Do not trust a VCD from a run that lacks the PASS line.

## Evidence boundary

Passing `make verify` proves the checked behavior of this exact valid-only, unsigned, one-cycle adder under the installed Icarus, Verilator, and Yosys versions. It does not prove placed-and-routed timing, setup/hold, CDC safety, power, FPGA-specific resources, formal completeness, AXI/ready-valid behavior, HLS scheduling, or C/RTL CoSim. Those require separate contracts and real tool reports; the field guide shows where they enter the workflow.

The main study page is offline-safe. The optional `docs/expected_waveform.html` helper loads pinned WaveDrom JavaScript from a public CDN and therefore needs network access and executes third-party code; prefer the checked JSON, local `make wave`, or a locally installed WaveDrom CLI when working offline or under a strict trust policy.

PDFs added through the study page are stored as full Blob data in browser IndexedDB for the exact `http://127.0.0.1:4173` origin. Nothing is transmitted automatically. If you explicitly consent and click the local-analysis button, the browser sends a temporary copy of only the selected PDF to the loopback study process on this computer. The server uses Poppler for bounded, text-only extraction, returns exactly two qualified candidates for the selected day or a failure, and deletes its temporary copy; it does not upload the PDF to the internet or to Codex.

The returned pages, snippets, scores, and confidence labels are heuristic evidence, not semantic proof. Preview both pages before applying them. Applying fills two **unconfirmed** locators; only your later identity and locator confirmation can unlock the source-synchronized worksheet. Code/exercise starters are fixed local templates selected from recognized concepts. PDF text is never executed or inserted into a template, starters are never run automatically, and every starter still requires specification, simulation, lint, and the named exercise gate. If extraction cannot find two qualified readings, or if you need semantic/visual analysis, attach the PDF explicitly in the Codex conversation.

Keep the original files. A different browser profile, `localhost` instead of `127.0.0.1`, a different port, private browsing, storage eviction, or clearing site data has a separate or empty shelf.
