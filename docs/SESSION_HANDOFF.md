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

## 4b. Verified by execution in this container (iverilog 12.0 + yosys 0.33 installed)

Lane C and the local-PPA flow had never been run anywhere. Both now have, against
hand-written RTL implementing the `ap_ctrl_hs` / `ap_memory` contract:

| Subsystem | Result |
|---|---|
| Lane C, scalar design (`ap_return`) | **pass**; sabotaging `a+b` -> `a-b` makes it fail with per-test expected/actual |
| Lane C, array design (`ap_memory`, 1-cycle read latency) | **pass** first try; corrupting one element pinpoints `out[5]` on every test |
| `gen_rtl_tb.py --from-rtl` reconciliation | **works**: detects `ap_rst_n`, flips polarity, rewires the instantiation, and the regenerated TB simulates clean against the active-low design |
| yosys synthesis + area parsing | **works** on yosys 0.33 (298 cells, 384.104 um^2) despite the code comment naming 0.6x |
| liberty -> Verilog cell models | **was broken** — see L1 below; now fixed and verified |
| gate-level waveform sim | **pass** after the L1 fix; VCD written; sabotage caught with a per-element mismatch |
| OpenSTA slack/power | **works** — built 3.1.0 from source (see below); worst setup slack 7.69 ns, worst hold 0.07 ns at a 10 ns clock |
| OpenSTA power | unverified: the test liberty carries no power tables, so `report_power` totals are 0 and the parser correctly declines to report a fabricated zero |

**Toolchain now installed in-container** (survives only while this container lives):
`iverilog 12.0`, `yosys 0.33` (apt), and **OpenSTA 3.1.0** built from source —
`git clone https://github.com/parallaxsw/OpenSTA`, CUDD 3.0.0 from
`github.com/davidkebo/cudd`, then `cmake -DCUDD_DIR=/opt/cudd-3.0.0 .. && make -j4`.
Symlinked to `~/tools/eda/opensta/bin/sta`, which is one of `resolve_sta_bin`'s default
probe locations, so the agent discovers it with no flags. Ubuntu archives and
`github.com` over git are reachable; `release.bambuhls.eu` and the GitHub **API** are not.

The readiness probe went from 4 blocking to 2: only the HLS toolchain and a per-project
liberty file remain.

**L2 / L3 (fixed, commit `f58228b`).** Found by building OpenSTA and running it.
(a) The failure note was always empty: OpenSTA writes diagnostics to STDOUT and leaves
stderr empty, but the note interpolated `sta_proc.stderr`, so a failed run reported
`"OpenSTA failed: "` with no reason. (b) Only `^Error[: ]` was matched, but OpenSTA labels
an abort `Critical <n>: ...`; combined with the already-noted "can exit 0 after a Tcl
error", a Critical would have been read as success and its truncated report parsed as
measurements. Detection is now the tested helper `sta_failure_line()`. (c)
`outcome.reports` advertised `sta_report.txt` after the failure path renamed it to
`sta_report.failed.txt`.

**L1 (fixed, commit `4fd9817`).** Liberty uses juxtaposition as AND; Nangate45 writes every
NAND/AND/AOI/OAI cell that way (`function : "!(A1 A2)"`). `_liberty_expr_to_verilog` did not
handle it and emitted invalid Verilog, and the unhandled-operator guard could not catch it
because whitespace is necessarily whitelisted there. So the cell was emitted broken rather
than skipped, contradicting the module's stated contract. The gate-level sim could not
compile against any real standard-cell library. Two regression tests added. Suite is now **225** (note: commit f58228b's message says 227 — that is wrong, and was not corrected because the branch is published).

**Reproduction assets** live in the scratchpad, not the repo: hand-written `add_scalars.v`
and `double_all.v` (ap_ctrl_hs + ap_memory), and a minimal `mini45.lib` with INV/BUF/NAND2/
NOR2/DFF. Worth re-creating as committed fixtures if lane C or local PPA gets more work.

---

## 4c. W5 — `--vitis-ssh` cannot target a Windows Vitis host

The user's natural split (Mac drives, Windows synthesizes) is **not supported today**.
`remote.py` assumes a POSIX remote in five ways: `ssh <host> bash -lc '...'`; GNU coreutils
`timeout -k 30s`; probing `/tools|/opt/Xilinx/.../settings64.sh` (Windows uses
`settings64.bat`); `rsync` for push and pull; POSIX paths throughout.

Three viable topologies, with cost:

| | Path | Needs | Gets |
|---|---|---|---|
| T1 | everything on the Windows box | W1 + W2 | fastest route to real RTL, no new architecture |
| T2 | everything on the Mac with Bambu | a Bambu backend (~7 modules) | RTL on the Mac, and rungs 2-4 finally testable in CI/containers |
| T3 | Mac drives, Windows synthesizes | W1 + W2 + W5 | the original split; most work, most fragile |

Recommendation: T1 now, T2 next, T3 only if the split is specifically wanted.

**Environment facts:** Vivado HLS was discontinued after 2020.2 — "Vivado HLS 2024.2" is
almost certainly **Vitis HLS 2024.2**, binary `vitis_hls.bat`, which is the name the code
already hardcodes. Bambu is on the user's Mac, not here: `release.bambuhls.eu` and GitHub
releases are both blocked by this container's network policy, and Bambu is not in the Ubuntu
archives. Ubuntu archives themselves ARE reachable, which is how iverilog and yosys got in.

---

## 5. Answered

- **Q1** deliverable → (b) a working tool; ASIC is the long-term goal, not the current one
- **Q2** topology → all three platforms eventually, **Windows first** (has Vivado)
- **Q3** generation mode → (a) LLM from C, usually with a spec
- **Q4** process → (c) docs on this branch, code fixes get issues; fix → run → verify → merge or delete
- **Q7, Q17** → **withdrawn** (ASIC backend location, liberty file) — out of scope

## 6. Open — next action

**Blocking:**
- **Q24** — topology: T1 / T2 / T3 (see 4c). Recommend T1 now, T2 next
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
