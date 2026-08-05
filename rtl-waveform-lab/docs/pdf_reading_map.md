# Three-day PDF reading workflow

Reading supplies explanations and context. A completed artifact plus its named gate is the evidence that you understood the material.

## How to use this map

Use the same loop for every row:

1. Add the complete source PDF to the study page's private shelf.
2. Read only the section named for the current stage.
3. Close or minimize the source and write the extraction from memory.
4. Produce the required artifact in the [ordered exercise sheet](ordered_exercises.md).
5. Run or review the named MANUAL, LOCAL, or EXTERNAL gate.
6. Continue only when the artifact and gate agree.

Reading completion alone never unlocks the next stage.

## Source and page-number rules

- Textbook: *Digital Design and Computer Architecture*. The repository does not contain this book. Use an owned copy and record its title, authors, edition or copyright year, and ISBN if available.
- Match a textbook section by title first, printed page second, and PDF viewer page last. Covers and front matter can shift the viewer page number.
- The inherited printed-page ranges are navigation aids, not broad cover-to-cover assignments: Chapter 1 pages 3-21, Chapter 2 pages 51-90, and Chapter 3 pages 103-146.
- HLS paper file-picker path (not a site URL): `../../papers/2507.04315v3.pdf`, Qingyun Zou et al., *HLStrans: Dataset for C-to-HLS Hardware Code Synthesis*, arXiv:2507.04315v3, 4 December 2025, 27 pages.
- Vendor documentation: use documentation that exactly matches the installed HLS tool version. The field guide links AMD Vitis HLS 2026.1 as a reference path, not as proof that the tool is installed.

## What the shelf reading coach does

- Known filenames: a filename only suggests HLStrans or the mapped digital-design textbook. It never confirms source identity.
- PDF synchronization: select the actual source type, copy the title/version/page convention from the opened PDF, and confirm two section locators plus their positive PDF viewer-page numbers for the current day.
- One-click chapters: after both locators are confirmed, each reading card gets a button that requests its exact page in the selected browser PDF viewer.
- Output worksheet: synchronization unlocks a copyable worksheet tied to the selected PDF record, identity revision, day, two readings, and two runs.
- Current study block: choose Day 1, Day 2, or Day 3 so textbook guidance stays aligned with the active stages.
- Two plus two contract: every day shows exactly two targeted reading cards and two usable video or guided-run cards. Every card names its stage, artifact, gate, source, and availability.
- Video rule: videos and guided runs prepare an artifact; watching alone never passes a gate.
- Opt-in local detection: after explicit consent and a button click, the loopback study process can rank pages for only the selected PDF and current day.
- Unfamiliar PDFs: use local detection as a text-only first pass. If it cannot find two qualified readings, or if semantic/visual inspection is needed, attach the PDF explicitly to Codex with the structured request.
- Privacy boundary: nothing is transmitted automatically. The opted-in temporary copy stays on this computer at `127.0.0.1`, is deleted after analysis, and is never uploaded to the internet or Codex.
- Safety rule: the UI calls this learner-confirmed synchronization, not parsing or independent verification. Changing the saved source identity or edition increments its revision and forces locator reconfirmation.

## Opt-in local PDF detection

Run `make serve` and use the exact `http://127.0.0.1:4173/docs/study.html` address. A plain static server cannot provide this API.

1. Select one shelf PDF and the current Day 1, Day 2, or Day 3 block.
2. Read the consent text, check the local-analysis box, and click **Analyze selected PDF locally**. Loading, selecting, viewing, or changing days never sends a PDF automatically.
3. The browser sends a temporary copy of only that selected Blob to the loopback study process. The server enforces size, page, text, and time limits; runs local Poppler text extraction; and deletes the temporary copy on success or failure. It makes no internet request.
4. Success returns exactly two distinct, qualified page candidates for the selected day. Each candidate includes a viewer page, heading hint, matched concepts, bounded snippet, deterministic score, confidence, stage, artifact, and gate. If two qualified candidates are not available, analysis fails instead of inventing one.
5. Treat every result as a text-only keyword/concept heuristic. It can miss diagrams, tables, equations, scanned pages, and context. Preview both candidate pages in the PDF viewer before applying them.
6. **Use both as unconfirmed locators** only fills the two locator fields. Both confirmation boxes remain clear. Save and confirm them only after the opened PDF content agrees; until then, the worksheet remains pending.
7. A recognized concept may select a fixed local RTL or exercise starter. PDF text never becomes code, no PDF content is executed, and no starter runs automatically. A starter is scaffolding, not a correctness result; complete its contract and pass the exercise's MANUAL, LOCAL, or EXTERNAL gate.

Local detection does not confirm a title, edition, arXiv version, page-number convention, generated design, or research claim. Those remain learner-confirmed or externally evidenced steps.

The two run cards are selected from these reviewed routes:

| Day | Run 1 | Run 2 |
|---|---|---|
| Day 1 | MIT OpenCourseWare: Combinational Devices | MIT OpenCourseWare: Sum of Products |
| Day 2 | MIT OpenCourseWare: Sequential Circuit Timing | MIT OpenCourseWare: Finite State Machines topic videos |
| Day 3 | AMD: Vitis HLS Overview | AMD 2026.1 Vitis HLS getting-started guided run |

Open the selected PDF in the shelf, choose the current day, and use the links on the two run cards. The Day 3 guided run satisfies no EXTERNAL gate unless the matching tool is actually run and its outputs are preserved.

## Route at a glance

| Day | Exercise stages | Core transition | Evidence before moving on |
|---|---|---|---|
| Day 1 | Preflight and Stages 1-4 | Behavior -> truth table -> equation -> gates -> combinational RTL | Completed preflight plus XOR, half-adder, mux, and width artifacts |
| Day 2 | Stages 5-8 | Edge -> old state -> next state -> transaction timing | State predictions, transition tables, verified waveform trace, and ready/valid classification |
| Day 3 | Stages 9-12 | Contract -> independent oracle -> synthesis -> HLS evidence | Oracle results, synthesis evidence, literature ledger, and a filled capstone contract |

## Day 1 - Preflight and Stages 1-4

| Stage | Read just in time | Extract from memory | Required artifact | Gate |
|---|---|---|---|---|
| Preflight | Chapter 1 Section 1.4; environment guide | Binary/hex conversion, two's complement, Boolean row count, tool purpose | Four written answers plus tool inventory | MANUAL + LOCAL |
| Stage 1 | Chapter 1 Section 1.5; Chapter 2 Sections 2.1-2.4; Chapter 4 Sections 4.1-4.2 | Specification -> truth table -> equation -> gates -> HDL | XOR module, equation, gate sketch, self-checking 4/4 table | MANUAL |
| Stage 2 | Chapter 2 gate/Boolean material; Chapter 5 Section 5.2.1 | Sum, carry, and why two one-bit inputs need two output bits | Half-adder, truth table, and exhaustive test | MANUAL |
| Stage 3 | Chapter 2 Section 2.8.1; Chapter 4 Sections 4.2 and 4.5, especially 4.5.4 | Mux selection, complete assignment paths, latch cause, blocking assignment | `assign` mux, `always_comb` mux, deliberate latch, corrected version | MANUAL |
| Stage 4 | Chapter 1 Section 1.4.6; Chapter 4 Section 4.2.7; relevant Chapter 5 arithmetic sections | Width, signedness, extension, truncation, overflow, shift behavior | Boundary-value width/sign worksheet | MANUAL |

Day 1 stop rule: do not proceed because the HDL compiled once. Exhaust the small input space, remove the deliberate latch, and explain every chosen width.

## Day 2 - Stages 5-8

| Stage | Read just in time | Extract from memory | Required artifact | Gate |
|---|---|---|---|---|
| Stage 5 | Chapter 3 Sections 3.2-3.3; Chapter 4 Section 4.4 and revisit 4.5.4 | Pre-edge right-hand sides, scheduled updates, committed state | Exact `(q1,q2)` prediction for E1-E3 plus nonblocking explanation | MANUAL |
| Stage 6 | Chapter 3 Section 3.4; Chapter 4 Section 4.6; Chapter 5 Section 5.4.1 | Register versus next-state logic, reset priority, FSM encoding, reserved-state recovery | Modulo-4 counter plus Moore-controller reset/transition/output tables and RTL | MANUAL |
| Stage 7 | [Waveform walkthrough](waveform_walkthrough.md), [cycle table](cycle_table.md), and the filled [generation contract](generation_contract.md) | Acceptance, latency, bubbles, throughput, held-invalid data, width, reset flush | Predicted E0-E10 trace and written explanation | LOCAL: `make sim` plus quiz >= 8/10 |
| Stage 8 | Transaction section in the [fundamentals field guide](fundamentals_field_guide.md) plus the exact five-edge exercise trace | Transfer, stall, bubble, and payload-stability obligation | E0-E4 ready/valid classification | MANUAL |

Chapter 3 is not being used as a ready/valid protocol reference. Stage 8 is intentionally grounded in the explicit transfer rule and trace; use a matching vendor protocol document only when a later design requires one.

## Day 3 - Stages 9-12

| Stage | Read just in time | Extract from memory | Required artifact | Gate |
|---|---|---|---|---|
| Stage 9 | Chapter 4 Section 4.8; evidence section in the [field guide](fundamentals_field_guide.md); oracle section in the [generation contract](generation_contract.md) | Black-box behavior, oracle independence, data versus temporal coverage, mutation purpose | Written explanation plus exhaustive and mutation evidence | LOCAL: `make exhaustive` and `make mutations` |
| Stage 10 | Chapter 4 Section 4.1.3; evidence boundary in the field guide; implementation/QoR fields in the contract; Yosys log/docs | Simulation versus synthesis, inferred cells, and unproven timing/CDC/power claims | Annotated lint and synthesis evidence | LOCAL: `make lint` and `make synth` |
| Stage 11A | HLStrans PDF pages 1-2; HLS boundary and ingestion focus in the field guide | Source task, target artifact, dataset triple, five transformations, annotations, claims, missing evidence | One evidence-ledger row | MANUAL |
| Stage 11B | Installed-version HLS documentation; HLS extension in `generation_contract.md` | Defined C subset, interfaces, tool/part/clock/directives, distinct validation gates | Reference-C/HLS contract plus real compile, CSim, synthesis/report, and CoSim records | EXTERNAL |
| Stage 12 | Completed contracts, ledgers, and prior artifacts; no new broad reading | Integrated specification, implementation, verification, and evidence reasoning | Capstone contract, RTL, independent oracle, tests, lint, synthesis, warnings | MANUAL, plus EXTERNAL only when a real vendor flow is used |

Day 3 local stop rule: `make verify` must pass, but it must not be relabeled as device timing, CDC, power, or vendor HLS evidence.

### Stage 11A - HLStrans paper extraction

Read PDF pages 1-2 of `2507.04315v3.pdf`. The PDF identifies itself as under review for ICLR 2026.

Extract in your own words:

1. Why C-to-HLS is not direct textual translation.
2. The five transformation categories: code restructuring, pragma insertion, data type adaptation, function replacement, and HLS-compliant repair.
3. The three items in each claimed dataset entry: original C kernel, optimized HLS implementation, and validation testbench.
4. The latency and resource annotations the authors say are obtained through synthesis.
5. Why a paired testbench is necessary but does not prove independence, coverage, or that a new candidate passes.
6. Which statements on pages 1-2 are author claims and which methods/results evidence must still be inspected.

Use the focus prompt and row template in the [fundamentals field guide](fundamentals_field_guide.md) to create one evidence-ledger row. Pages 1-2 do not prove a new generated program correct, a C/RTL CoSim pass, or a fair QoR comparison.

### Stage 11B - actual HLS execution

Use the exact installed tool documentation for C modeling, self-checking tests, synthesis reports, and C/RTL CoSim. Record tool name/version, device/part, clock, directives, commands, tests, and report paths in the [generation contract](generation_contract.md).

Only preserved results from the real tool can satisfy this EXTERNAL gate. If the tool is absent, mark the gate unavailable instead of inventing evidence.

### Stage 12 - vendor documentation and capstone

Fill a capstone contract, then return RTL, a separate oracle, directed boundary tests, randomized sequence tests, lint results, synthesis evidence, and unresolved warnings. Use vendor documentation only for the tool and target actually being run.

## Definition of done

You are finished with this map when you can explain each concept without the source open, point to the artifact you produced, name the gate that checked it, and distinguish local RTL evidence from literature claims and external HLS or implementation evidence.
