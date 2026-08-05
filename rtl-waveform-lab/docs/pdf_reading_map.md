# PDF reading map for the three-day sprint

Active exercises remain primary. Use the PDFs to supply explanation and context, then produce the listed artifact before moving on.

Use the study page's private PDF shelf to add your complete textbook and paper files. The shelf stores the full documents on this browser/device and lets you read, open, or download the selected source beside the lesson.

## Digital-design textbook map

The original study plan names *Digital Design and Computer Architecture* and uses the printed-page references below. The textbook PDF itself is not present in this repository, so these inherited page references could not be visually reconciled against a particular edition or PDF-page offset. Match the section titles in your owned copy; do not rely only on the viewer's page number.

| Exercise stage | Reading from the original plan | What to extract |
|---|---|---|
| Preflight and Stages 1–4 | Chapter 1 §§1.1–1.5, printed pp. 3–21; Chapter 2 §§2.1–2.9, printed pp. 51–90 | Abstraction levels, number systems, gates, Boolean algebra, combinational blocks, adders, muxes, and propagation delay |
| Stages 5–8 | Chapter 3, printed pp. 103–146 | Sequential logic, registers, reset, FSMs, timing vocabulary, and old-state/new-state reasoning |
| Stages 9–10 | Chapter 4 §§4.1–4.2, 4.4–4.6, 4.8; Chapter 5 §§5.2 and 5.4 | HDL structure, combinational versus sequential coding, testbenches, synthesis boundary, datapath, and control |

Stop/go rule: after each row, complete its exercise outputs and gate in `ordered_exercises.md`. Reading completion alone does not unlock the next stage.

## Supplied C-to-HLS paper map

The locally supplied paper inspected for this route is:

- `../../papers/2507.04315v3.pdf` — Qingyun Zou et al., *HLStrans: Dataset for C-to-HLS Hardware Code Synthesis*, arXiv:2507.04315v3, 4 December 2025, 27 pages. The PDF labels itself “under review as a conference paper at ICLR 2026.”

Read PDF pages 1–2 for Stage 11. Extract, in your own words:

1. Why C-to-HLS is not direct textual translation.
2. The five transformation categories named by the paper.
3. What each dataset entry contains.
4. Which annotations come from synthesis.
5. Why paired testbenches matter to a correctness claim.
6. Which statements are author claims versus evaluation evidence you would still need to inspect later.

Then fill the paper-ingestion fields in `fundamentals_field_guide.md`. Pages 1–2 establish the task and dataset claim; they do not establish that any new generated program is correct, that C/RTL CoSim passed, or that a QoR comparison is fair.

## Vendor documentation map

For an actual HLS run, substitute the documentation matching the installed tool version. The current reference path in this lab points to AMD Vitis HLS 2026.1 material for C modeling, self-checking testbenches, and C/RTL CoSim. Record the tool version, device, clock, directives, and report path in the generation contract.
