# Session handoff — workflow review

> **Temporary working state.** Delete this file before merging the branch. It exists so a
> restarted session can resume without re-deriving context.

**Branch:** `claude/agent-workflow-review-owfc84`
**Base at time of writing:** `bba56bd`

---

## 1. Where we are

Two deliverables are committed on this branch, both docs-only, no behaviour change:

| File | What it is |
|---|---|
| `docs/full_workflow.md` | Full component + agent workflow map. Every stage of `convert`, the other three commands, the evidence lanes, the dataset scripts, which of the eight declared agents are live, and a "what is generated, and by what" table covering all 21 emitted files and every model call site |
| `docs/workflow_evaluation.md` | Evidence-first quality review. Ten findings (F1–F10) from nine executed experiments, a readiness matrix, loop-boundedness table, stimulus-adequacy analysis, pre-run checklist, and a fix order |
| `scripts/check_readiness.py` | Read-only cross-platform probe. Reports which evidence tiers a machine can actually produce and tests the two Windows failure modes `shutil.which()` cannot detect. **Not yet run on the Windows box — that is the next action.** |

Published companion pages (same content, formatted):
- Workflow map — https://claude.ai/code/artifact/a93db7ad-1225-4f8c-9d65-25c7fc713bf4
- Quality evaluation — https://claude.ai/code/artifact/7bc8a789-c2e9-46e2-b63d-67865a8018c1

Test suite green throughout: `python -m unittest discover -s tests` → 222 passed, 1 skipped.

---

## 2. Scope, as the user has stated it

- **Not** one-step ASIC generation. For now: **generate functionally equivalent RTL**, plus
  **some generations for better QoR**.
- Long-term goal is ASIC for specific C algorithms, currently limited by how many lines of
  code the agent can convert.
- Deliverable class: a working tool used by the user and collaborators.
- Platform: should run on Linux, macOS and Windows; **focus on Windows for now** because
  that box has Vivado.
- Primary generation mode: **LLM generation from C, usually with a `--spec`**. Where no
  spec exists, specs are derived from open-source data.
- Process: docs stay on this branch; code fixes get proper issues. Fix, run, verify, then
  merge or delete from `main`.

### What that scope implies

- **In scope now:** the verifier ladder, RTL equivalence, `optimize` driven by Vitis
  csynth reports (`--objective latency|area|balanced`).
- **Deferred:** local PPA (yosys, OpenSTA, liberty), `--target-slack/-area/-power`, the
  gate-level waveform sim, ASIC area. Ignore ASIC-PPA failures in the probe output.
- Because equivalence *is* the product, **F1 is the top finding**, not one of ten.
- Without local PPA, F3's worst case drops from >30 h to ~11 h.

---

## 3. Priority order

| Tier | Items | Why |
|---|---|---|
| **0** | **W1** — Vitis launch on Windows | No RTL exists until this works. Blocks everything |
| **1** | **F1**, **F10** | The oracle hole, and stating precisely what a pass licenses. This is the product |
| **2** | **W2**, F5, F8, F9, visibility fixes | Make failures legible; get the lanes running on Windows |
| **3** | F2, F3, F4 | Interface control and bounding `optimize` for the QoR generations |
| **Deferred** | local PPA, OpenSTA, liberty, PPA targets | Out of scope until ASIC work resumes |

---

## 4. Findings index

Full detail in `docs/workflow_evaluation.md`. Windows findings (W1–W4) were discovered
after that file was written and are recorded here only.

| ID | Sev | One line |
|---|---|---|
| W1 | High* | On Windows the binary is `vitis_hls.bat`. `shutil.which` finds it via `PATHEXT`, but `Popen` uses `CreateProcess`, which appends `.exe` and **not** `.bat` → `FileNotFoundError`. `_run_vitis_phase` catches only `TimeoutExpired`, so it propagates uncaught out of `run_convert`. `hls_runner.py:61,104` |
| W2 | High | Generated project is Unix-shaped: Makefile hardcodes `python3`, `rm -f`, `mkdir -p`, `./$(EXE)`; generated Python shells out to `["python3", ...]` (`leveri_testgen.py:459`, `verilog_testgen.py:978`) instead of `sys.executable` |
| W3 | Medium | `start_new_session=True` is ignored on Windows and `os.killpg` does not exist, so a timeout falls back to `proc.kill()` and leaves the Vitis process tree running. `equivalence.py:96` |
| W4 | Low | `run_all.sh` is bash-only, no `.ps1`/`.bat` sibling |
| — | — | `vivado_hls` as a **binary name** is supported nowhere; only `vitis_hls` is hardcoded. `-flow_target vivado` in the TCL is a Vitis HLS option, not Vivado HLS. `--vitis-bin` exists only on the `--vitis-ssh` remote path |
| F1 | High | Output comparison is clamped by argument **name** + declared `range`. A design writing wrong values beyond index `n` passed 64/64. `tb/testbench.cpp` is shared by rungs 1, 2 **and** 4 (`add_files -tb`), and lane C inherits it via `cmp_scalar`. Only the LeVeri lane compares full arrays |
| F2 | High | LLM-chosen interface pragmas unconstrained, unrecorded (`interface_pragmas=[]`), and `rtl_tb_manifest.json` derives from config not source. Reproduced: config `ap_memory` → model emitted `m_axi` |
| F3 | Med-high | `optimize` has no run controller, no wall bound, no checkpoint, no LLM budget |
| F4 | Medium | `optimize` never checks the project passes before optimizing |
| F5 | Medium | A `blocked` classification closes the run as `failed` |
| F6 | Medium | `convert` resumes the budget but regenerates the source and clears the repair audit |
| F7 | Low | `--max-iterations` silently doubles as an immutable persistent budget |
| F8 | Low | `_report_is_fresh` ignores `hls_top.hpp` |
| F9 | Low | CoSim log gate runs before the remote artifact pull, so it only sees console output |
| F10 | Positive | CoSim here **is** a golden-C-vs-RTL check, because `run_csim.tcl` adds `tb/testbench.cpp` (which compiles the macro-renamed original C) as the testbench. Stronger than the caveat currently in README/AGENTS.md |

\* W1 is mechanism-level, high confidence, **not yet reproduced** — no Windows machine was
available. `scripts/check_readiness.py` settles it.

---

## 5. Answered

- **Q1** deliverable → (b) a working tool; ASIC is the long-term goal, not the current one
- **Q2** topology → all three platforms eventually, **Windows first** (has Vivado)
- **Q3** generation mode → (a) LLM from C, usually with a spec
- **Q4** process → (c) docs on this branch, code fixes get issues; fix → run → verify → merge or delete
- **Q7, Q17** → **withdrawn** (ASIC backend location, liberty file) — out of scope

## 6. Open — next action

**Blocking:**
- **Q5** — run `python scripts/check_readiness.py` from the repo root on the Windows box and
  paste the output. Ignore the ASIC-PPA failures. This settles W1.
- **Q6** — is the binary `vitis_hls` (supported) or the legacy `vivado_hls` (supported
  nowhere today)?

**Queued, can be decided while W1 is in flight:**
- **Q8** W2 fix shape — (a) platform-appropriate Makefile + `sys.executable` everywhere
  (b) `sys.executable` only, require MinGW `make`, document it (c) Makefile + `make.ps1`
- **Q9** confirm the five zero-risk visibility fixes (F5, F8, F9, print the active clamp,
  populate the interface-pragma ledger)
- **Q10** do the user's kernels ever have a legitimately-undefined output tail?
- **Q11** F1 fix shape — (a) explicit `active_length`, full comparison default (b) keep
  inference, tail as a warning (c) (b) now, (a) as a follow-up
- **Q12** may the model choose the interface mode? (a) no (b) yes but recorded (c) binding
  when set explicitly, free on `default`
- **Q13–Q15** `optimize` ceilings, resumability, precondition (simpler now — no PPA targets)
- **Q16, Q18** first real run, and acceptance bar
- **Q19** which QoR objective — `latency`, `area` or `balanced`? Note `area` is an **FPGA
  resource proxy** with hardcoded weights, not ASIC area
- **Q20** wire lane C (standalone RTL sim) into the ladder as a reported phase? `xsim` ships
  with Vivado, so it is near-free on the target machine and gives an equivalence witness
  independent of Vitis cosim
