# The agent headers: a walkthrough for partial edits

Verified against `C:/Users/luke/c2hlsc-rtllm`, branch **`fix/rtllm-windows-enablement`**, HEAD **`785f2e1`**, working tree **clean**. Three corrections to the brief up front, because they change what you need to do:

- The Windows fixes are **committed, not uncommitted** — `f035da5` ("Make the RTLLM v2 harness runnable on native Windows") and `785f2e1` ("Serialize sim_returncode…"). `git status --porcelain` is empty.
- `simulator_launch_failed` is now in `UNREPAIRABLE_FAMILIES` (`rtllm_agent.py:416`), and `python -m unittest tests.test_rtllm_agent` is **74 tests, OK**. The "failing family-coverage test" is resolved.
- Line numbers in `rtllm_agent.py` drifted ~5 lines from the older notes. Everything below is re-grepped.

---

## The agent roster

### RTLLM v2 loop (`c2hlsc_agent/rtllm_agent.py`) — the measurement path

| Agent | Header constant | file:line | What it decides |
|---|---|---|---|
| `rtl_planner` | `RTL_PLANNER_SYSTEM_PROMPT` | `rtllm_agent.py:131` | The interface contract (module name, ports, clocking, reset, edge cases) the generator must implement. Writes no Verilog. Skipped by `--no-plan` / `RtllmAgentConfig.plan` (`:436`). |
| `rtl_generator` | `RTL_GENERATOR_SYSTEM_PROMPT` | `rtllm_agent.py:159` | Round-0 Verilog file. **Everything downstream scores this.** |
| `rtl_repair_agent` | `RTL_REPAIR_SYSTEM_PROMPT` | `rtllm_agent.py:192` | The minimal patch, once per repair round, up to `max_repair_rounds` (default 2, `:434`). |
| `failure_analyst` | `BLIND_RETRY_EVIDENCE` (`:816`) + `REPAIR_INTENTS` (`:218`) + `FAMILY_REPAIR_INSTRUCTIONS` (`:273`) + `interface_restatement` (`:366`) | `rtllm_agent.py:950` | **What the repair agent is allowed to know.** No LLM call — deterministic assembler. |
| self_trace channel | inline `head` literal | `rtllm_agent.py:880` | The `self` rung's extra channel: the candidate's own signals over time. |
| timeout_diagnosis channel | inline `TimeoutDiagnosis.report()` | `rtllm_bench.py:1796` | Liveness evidence for a watchdog kill (where `sim_log` is empty). |
| oracle_diff channel | inline `BehaviourDiff.report()` | `rtllm_bench.py:1994` | First stdout divergence vs. the reference. **Upper bound only.** |

### Core C→HLS-C pipeline (`c2hlsc_agent/`) — not the RTLLM number

| Agent | Header constant | file:line | What it decides |
|---|---|---|---|
| `hlsc_generator_agent` | `HLSC_GENERATOR_SYSTEM_PROMPT` (id `hlsc_generator_vitis_beginner_v1`, `hlsc_generator.py:6`) | `hlsc_generator.py:19` | Authors `src/hls_top.cpp` from the original C. |
| `hlsc_repair_agent` | `REPAIR_SYSTEM_PROMPT` | `llm.py:460` | Rewrites `src/hls_top.cpp` when no mechanical repair matched. |
| `rtl_optimizer_agent` | `QOR_OPTIMIZER_SYSTEM_PROMPT` | `llm.py:533` | Post-equivalence QoR/PPA pragma rewrites. |
| NL reference author | `NL_REFERENCE_SYSTEM_PROMPT` | `llm.py:605` | In `--spec-only` mode **writes the golden oracle itself**. |
| `shift_left_testbench_agent` | `LEVERI_TESTBENCH_SYSTEM_PROMPT` | `leveri_testgen.py:13` | **Nothing. Never sent to a model.** Inert policy text. |
| `rtl_testbench_agent` | `RTL_TESTBENCH_SYSTEM_PROMPT` | `verilog_testgen.py:13` | **Nothing. Never sent to a model.** Inert policy text. |
| `contract_planner` | none | `agent_loop.py:53` | Declared `AgentProcedure` only. No file, no prompt — `analyze.py` does the work with regexes. |
| golden-C testbench gen | none | `testgen.py:134` | Emits the host-equivalence oracle. Pure template code; there is no header here. |
| HLS_NL cosim repair | `REPAIR_SYSTEM` | `scripts/cosim_repair_loop.py:86` | A **second, independent** repair agent. Does not use `REPAIR_SYSTEM_PROMPT`, does not use `ClaudeCLIClient`'s sandbox. |
| HLS_NL single-shot gen | `SYSTEM_PROMPT` | `scripts/generate_hls_nl_llm.py:63` | Bare NL→HLS-C batch arm. |

---

## Each header, and what you may change

### 1. `rtl_planner` — `rtllm_agent.py:131`

**Instructs.** Opens: `"You receive ONE natural-language hardware design description. You do NOT write RTL."` and the reason the header is exhaustive: `"Getting the interface wrong makes every later stage fail, so be exhaustive and literal: prefer what the description says over what a typical design would do."` Then a fixed 8-section skeleton — `Module`, `Ports` (`name | input|output | width | meaning`), `Clocking`, `Reset`, `Behavior`, `State`, `Edge cases`, `Ambiguities` — with `"Module: the exact module name the description gives (copy it character for character)"` and `"List EVERY port the description names, in the order it names them, with the exact identifiers."` Closes: `"Keep it under 40 lines. Output the contract as plain text only -- no code fences, no Verilog."`

**Inputs.** `plan_contract` (`rtllm_agent.py:772`) = `_description_block(design)` (`:759`, interpolating only `design.name`, `design.category`, `design.description`) plus the line `"Write the interface contract for this design. Do not write any Verilog."` Nothing else — no testbench, no reference, no prior round.

**Parser required contract: NONE.** This is the only agent with no parser. `plan_contract` returns `_complete(...).strip()`; `_run_sample` does `contract = plan_contract(...) or None`. Whatever comes back is interpolated verbatim into the generator prompt at `generate_rtl` (`:788`).

**Freely rewordable.** Section names, section count, the 40-line cap, tone, the whole skeleton. Nothing parses it.

**Do not touch.**
- Don't add anything that reads `RtllmDesign.testbench` or `.reference_files` — that is the whole strict-track claim (module docstring rule 1, `rtllm_agent.py:23-32`).
- Keeping `"plain text only -- no code fences"` is advisable: drop it and fenced Verilog leaks into the generator prompt labelled *"implement it exactly"*, silently becoming a code suggestion nobody audits.

**Tests/docs that move with it.** `tests/test_rtllm_agent.py:556-566` pins the *generator-side* wrapper (asserts `"Ports: a | input | [7:0] | operand A"` and `"rtl_planner"` reach the generator prompt) — so if you change the pipe-delimited Ports format, update that test. `tests/test_rtllm_agent.py:544-554` covers the planner-outage path. `docs/rtllm_v2_benchmark.md:31-38`.

**Failure behaviour.** A planner outage is **non-fatal**: `_complete` retries 3 attempts (2s/4s/8s, `_RETRY_BASE_DELAY` at `:127`), then `LlmCallError` is caught and `plan_error` recorded; generation proceeds blind with `contract=None`.

---

### 2. `rtl_generator` — `rtllm_agent.py:159`

**Instructs.** The load-bearing lines:

> `"A hidden testbench you will never see instantiates your module and compares its outputs against a golden model; it is the only judge."`
> `"Name the top module EXACTLY as the description says, and declare EXACTLY the ports it names… The testbench binds ports by name: one renamed or missing port fails the whole design."`
> `"Target Verilog-2001 that Icarus Verilog (iverilog 12) accepts. Do NOT use SystemVerilog-only constructs: no `logic`, `always_ff`/`always_comb`…"`
> `"Do NOT write a testbench… or any simulation output or control system task: no `$display`, `$write`, `$monitor`, `$strobe`, `$fdisplay`, `$finish`, `$stop`, `$dumpvars`. This is enforced, not advisory: the harness refuses such a file without compiling it."`
> `"Output ONLY the complete Verilog file in ONE ```verilog fenced block. No prose outside it."`

Note the header hardcodes **iverilog 12** — this box runs 14.0. Cosmetic, but it's the only version string in any prompt, and see the version-coupling section below.

**Parser required contract.** `extract_verilog(response, design.name)` — `rtllm_agent.py:672`. Pipeline: truncate at `MAX_RESPONSE_CHARS = 512_000` (`:584`); take fenced blocks whose lang is in `_VERILOG_LANGS = {"", "verilog", "systemverilog", "sv", "v", "vlog"}` (`:579`) **and** that contain `module`; fall back to any block containing both `module` and `endmodule`; fall back to raw text. Then `_module_units` pairs line-initial `module`/`endmodule`. `_looks_like_testbench` drops volunteered testbench modules but never the required top. **Duplicate module names collapse to the LAST definition.**

**Freely rewordable.** All prose, the Verilog-2001 do/don't list, the iverilog version string, reset/sensitivity guidance, the helper-submodule permission.

**Do not touch — the parser or the measurement breaks:**
1. **The fence tag.** Asking for ```` ```rtl ```` or ```` ```code ```` only survives via the weaker `module`+`endmodule` fallback. Asking for a bare file with prose relies on a coarse first-module/last-endmodule slice.
2. **"Name the top module EXACTLY."** `extract_verilog` will **never rename** (module docstring rule 3, `:57-63`). Weakening this converts real designs into `missing_module` failures.
3. **The `$display`/`$finish` ban.** `rtllm_bench.find_illegal_system_tasks` (`rtllm_bench.py:503-563`) is what makes the pass oracle sound — the verdict is a substring test on a stream the DUT shares with the testbench. A candidate allowed to print can print `"Pass"`.
4. **"Do NOT write a testbench."** `_looks_like_testbench` only matches `testbench`/`tb`/`_tb`/`tb_` patterns; a differently-named testbench module compiles in and can collide.

Anything derived from `RtllmDesign.testbench` / `.reference_files` is forbidden.

**Tests/docs.** `tests/test_rtllm_agent.py:250-347` and `405-444` (extract contract + degenerate-response timing), `556-566`, `598-617`; `docs/rtllm_v2_benchmark.md:40-51`. The same ban is restated to the repair agent at `REPAIR_INTENTS["illegal_system_task"]` (`:251-255`) and `FAMILY_REPAIR_INSTRUCTIONS["illegal_system_task"]` (`:333-340`) — edit all three together.

**Failure behaviour.** `LlmCallError` here **ends the sample immediately**, no repair rounds. Bucketed as `llm_error`, drives `backend_failed` and the driver's exit code 3.

---

### 3. `rtl_repair_agent` — `rtllm_agent.py:192`

**Instructs.**

> `"You never see the testbench or any reference design -- only the description, the candidate, and the tool output the candidate produced."`
> `"Make the MINIMAL change that fixes the reported failure. Do not rewrite working logic, do not restyle, do not add features the description does not ask for."`
> `"Keep the module name and the declared port list unless the evidence proves the interface itself is wrong."`
> `"Diagnose before editing: name the mechanism in one line, then fix that mechanism. A wrong value is usually a width, sign, reset value, or off-by-one-cycle timing bug; a hang is usually a state that is never left or a done/valid pulse that is never asserted."`
> `"Return the COMPLETE corrected file in a single ```verilog fenced block, and nothing else of substance."`

**Integrity note worth knowing before you edit.** `"You never see the testbench or any reference design"` is literally true under every policy — no source is ever quoted. But under `evidence_policy=oracle` the user message *does* carry behaviour derived from the reference RTL. The header text is not updated for that track; `BehaviourDiff.report` supplies its own contradicting banner instead (`rtllm_bench.py:1997`).

**Inputs.** `repair_rtl` (`rtllm_agent.py:1008`): `_description_block`, then the `failure_analyst` evidence for **this round**, then `"Current `{name}.v` to repair:"` + the candidate fenced, then `"Change as little as possible."` **There is no cross-round transcript** — only the latest candidate and latest evidence.

**Parser required contract.** Identical `extract_verilog` — same cap, same testbench-dropping, same last-definition-wins, same never-rename. One asymmetry to know: the loop **overwrites `rtl` with the parse result before verification**, so an unparsable response discards the previous better candidate for the *next* round's prompt. The sample outcome is still best-of-rounds.

**Freely rewordable.** All prose, the diagnostic heuristics.

**Do not touch.** The single-```verilog-block demand; `"the top module must still be named"`; the system-task ban. **Careful:** softening `"Keep the module name and the declared port list"` produces port churn on `functional_mismatch` rounds, which `FAMILY_REPAIR_INSTRUCTIONS["functional_mismatch"]` actively fights (`"keep the port list exactly as it is"`, pinned by `tests/test_rtllm_agent.py:830-832`).

**Tests/docs.** `tests/test_rtllm_agent.py:568-596`, `694-707`, `1100-1150`; `docs/rtllm_v2_benchmark.md:82-89`.

**Early exits before the model is called.** `_stopped(stop)`, and `sim.failure_family in UNREPAIRABLE_FAMILIES = frozenset({"missing_golden_data", "simulator_launch_failed"})` (`:416`).

---

### 4. `failure_analyst` — `rtllm_agent.py:950` (no system prompt; makes no LLM call)

This is not a prompt but a set of constants that become the middle of every repair prompt. Verified: `rtllm_bench.py` contains no `client.complete` call at all.

**The four editable string banks:**

| Constant | Line | What it produces |
|---|---|---|
| `BLIND_RETRY_EVIDENCE` | `:816` | The entire evidence body under `policy=none` |
| `REPAIR_INTENTS` | `:218` | One line per family: `"Repair intent: …"` |
| `_DEFAULT_INTENT` | `:264` | Fallback for an unrecognised family |
| `FAMILY_REPAIR_INSTRUCTIONS` | `:273` | The per-family repair *procedure* |
| `interface_restatement` | `:366` | Re-quotes the description's own Module/Ports block, capped at `_INTERFACE_RESTATEMENT_LIMIT = 1500` (`:363`), fired only for `_INTERFACE_FAMILIES = {"missing_module", "port_mismatch"}` (`:396`) |

**Assembly order** is fixed at `build_evidence` (`:824`): stage/family/intent → family procedure (+interface restatement) → `"Tool output (tail, at most 4000 chars)"` fenced → extras last.

**Freely editable.** Every intent line, every procedure, `EVIDENCE_LIMIT` (`:100` — but keep it equal to `llm.py:45` `_EVIDENCE_LIMIT` so the two loops stay comparable, per the comment at `:97-99`), `_INTERFACE_RESTATEMENT_LIMIT`, section order.

**Do not touch:**
- **Don't add a policy rung that leaks oracle information without also adding it to `ORACLE_DERIVED_POLICIES` (`:125`).** The comment at `:121-125` names the four stamps that must move together and warns: *"Widening this set without widening those stamps is how an upper bound gets quoted as a headline."* You must also add it to `run_ablation.py:261` `EVIDENCE_TRACKS`, or every arm using it prints as **"unclassified evidence policy: do not quote"** (`run_ablation.py:278`, `:1123`).
- **Don't make `none` emit anything beyond the fixed notice** — `tests/test_rtllm_agent.py:899-907`, `872-875` assert the family name and log text are absent.
- **Don't let `interface_restatement` read `design.testbench` or `.reference_files`** — asserted at `tests/test_rtllm_agent.py:843-850`.
- If you edit `FAMILY_REPAIR_INSTRUCTIONS["timeout"]` (`:319-332`), keep the liveness topics: `tests/test_rtllm_agent.py:825-828` requires the literal strings `"combinational loop"`, `"clock edge"`, `"terminating condition"`.

**Family coverage.** Every family in `rtllm_bench.FAILURE_FAMILIES` (`rtllm_bench.py:123-135`) except the unrepairable ones needs both a `REPAIR_INTENTS` and a `FAMILY_REPAIR_INSTRUCTIONS` entry — `tests/test_rtllm_agent.py:808-816` enforces it. **This currently passes.** If you add a family to `rtllm_bench.FAILURE_FAMILIES`, add both entries or that test goes red.

**Failure behaviour.** Every extra channel is best-effort: `_collect` (`:986`) catches bare `Exception`, logs `"{design}: {name} evidence unavailable ({exc!r})"`, drops the channel from `sources`, and continues. An exception in one costs the round its extra evidence, never the sweep.

---

### 5–7. The three evidence channels (inline strings, not constants)

**self_trace** — `rtllm_agent.py:880`. Header handed to the model: `"Self-derived behaviour trace. A COPY of your module -- your scored file is untouched -- was instrumented to print its OWN ports and registers and run on the same stimulus. Every value below is your design's; nothing here comes from a reference implementation or from the testbench's own output."` Prose is free. **Do not weaken `filter_trace_lines` (`rtllm_bench.py:1658-1675`)** — it keeps only `TRACE_MARKER`/`DIAG_MARKER` lines and is the only thing keeping the testbench's expected values out of a track advertised as strict. **Keep the sub-run in its own directory** (`:994`, asserted at `tests/test_rtllm_agent.py:997-1009`): sharing a dir with a scored attempt reuses `<design>.v`/sim filenames and can fabricate a syntax pass.

**timeout_diagnosis** — `rtllm_bench.py:1796`. Exists because a watchdog kill leaves `sim_log` empty, so `logs` alone hands the repair agent `"(no captured tool output)"`. Opens: `"Timeout diagnosis (bounded re-run of an instrumented, NON-SCORED copy of YOUR module; every value below is your own design's):"` then exactly one of three mutually-exclusive readings (`rtllm_bench.py:1833-1849`): zero-delay loop / *"NOT ONE of your module's signals ever changed after the start. The design is not being driven"* / *"A terminating condition, done/valid pulse, or state transition is unreachable."* **Keep the three readings distinct** — `signals_moved` vs `time_advanced` separates two faults needing opposite repairs. Fires under every policy except `none`, in an `elif` chain: a timeout under `self` takes the **bounded** diagnosis instead of the unbounded self trace.

**oracle_diff** — `rtllm_bench.py:1994`. The only channel that leaks answer-key information. Banner: `"ORACLE-DERIVED EVIDENCE (upper-bound track -- this run is NOT comparable to a self-derived or published RTLLM number):"` and `"a known-good implementation of this description was run on the same testbench; you are being shown WHAT IT PRINTED, never how it is written."` Then first-divergence line number, `expected:`, `got:`. **`tests/test_rtllm_agent.py:941` asserts `"ORACLE-DERIVED EVIDENCE"` appears; `1060-1067` asserts reference-source markers never do.** Hard rules: never let the reference source, the testbench source, or more than the first divergence line into the report. Note the stamp is **policy-based, not fire-based** — `SampleResult.oracle_derived_evidence` (`:509-512`) is set from config even if no diff ever fired, so a design that passed at round 0 in an oracle sweep still carries the mark.

---

### 8. `hlsc_generator_agent` — `hlsc_generator.py:19`

**Instructs.** `"You are an FPGA HLS code-generation assistant."` / `"The target tool is unspecified; default to AMD/Xilinx Vitis HLS syntax and conventions."` (`:30`) / `"Preserve functional correctness first."` (`:36`) / `"Do not add pragmas blindly. … Every pragma must be tied to a specific code pattern and a specific expected benefit."` (`:71-72`) / `"If multiple pragma strategies are plausible, provide Option A (conservative) and Option B (aggressive)."` (`:86`). The machine-integration half is in the **user** prompt, not the header: `"Section 4 (\"Vitis HLS annotated code\") MUST contain a single complete, self-contained C++ translation unit"` (`llm.py:448-457`).

**Parser required contract.** `extract_hls_source` (`llm.py:694`): regex-searches case-insensitively for the literal `vitis hls annotated code` (**`llm.py:713`**, verified), parses only text after it, filters fences to `_CODE_LANGS` (`llm.py:658`), requires the block to define the top function, then picks the **LAST** differing candidate — i.e. Option B beats Option A — then gates on `is_plausible_translation_unit` (`llm.py:684`).

**Confirmed on this branch — the golden-C leak.** `convert.py:105` does `original_source = analysis.function.source_path.read_text(...)` and passes it straight into the prompt at `:106`. That same path is what `hls_project.py:164` copies to `<out>/input.c`, and `testgen.py:200-205` macro-includes as the golden oracle. **The generator agent receives the exact bytes of the oracle**, and it is a tested invariant (`tests/test_hlsc_generator.py:60-64`). This contradicts `README.md:253-254`, `AGENT_SUMMARY.md:14`, and the module docstring at `llm.py:20`. If you want to close it, **the edit is at `convert.py:105-106` / `hlsc_generator.py:136-144`, not in the header** — and section 3 ("Original code") plus those tests must go with it.

**Do not touch.** The literal string `"Vitis HLS annotated code"`; the one-```cpp-block + `#include "hls_top.hpp"` + exact-signature demand; the Option A/Option B instruction (the parser takes the LAST block, so removing it changes which candidate is adopted). Sections 1,2,3,5,6,7,8 are demanded and **discarded** — reword them freely.

**Tests.** `tests/test_hlsc_generator.py:19-26`, `35-48`, `60-64`; `tests/test_llm_agents.py:93-131`, `227-292`.

---

### 9. `hlsc_repair_agent` — `llm.py:460`

`"Produce the MINIMAL change that fixes the reported failure."` / `"Preserve functional equivalence with the original C and the exact top-function signature."` / `"Do not change observable outputs, argument meanings, declared array lengths, or the golden oracle."` / `"Return the COMPLETE corrected file in a single ```cpp fenced block, and nothing else of substance."`

The third rule is a **prompt-level request only** — the actual control is that `_llm_repair` writes nothing but `src/hls_top.cpp` (`hlsc_repair_agent.py:219`, `253-258`).

**Parser.** `extract_full_file(response, must_contain=f"{top}(")` (`llm.py:738`) returns the **longest** matching C/C++ block. Then four gates at `hlsc_repair_agent.py:246-251`, including a sha256 oscillation check against `_known_candidate_hashes`.

**Do not touch.** The COMPLETE-file-in-ONE-block demand: a header inviting a diff or multiple partial blocks makes the parser write a fragment, which fails `is_plausible_translation_unit` and **silently yields `status=no_change`** — an invisible loss of the repair rung. Don't weaken the exact-signature clause; the testbench and rtl_vectors harness are generated against `fn.signature`.

`decision.family`/`next_action`/`repair_scope` come from the deterministic classifier at `agent_loop.py:147-273` — edit there, not here. `_EVIDENCE_LIMIT = 4000` is duplicated at `hlsc_repair_agent.py:21`. Tests: `tests/test_strong_agents.py:528-590`.

---

### 10. `rtl_optimizer_agent` — `llm.py:533`

Runs **only after the full ladder passes**. `"Keep the EXACT top-function signature… The golden-C testbench re-verifies every candidate."` / `"Prefer pragma-level changes (PIPELINE, UNROLL, ARRAY_PARTITION, DATAFLOW, INLINE, DEPENDENCE, LATENCY, BIND_STORAGE/BIND_OP) tied to a specific loop/array and the specific bottleneck visible in the report."` / `"Every pragma must be justified: add a short // comment on the line above each change."` / `"Do NOT repeat a strategy listed as already tried."`

The `// comment` rule is **pure documentation** — nothing parses those comments. The strategy label the loop actually uses is regex-derived from `#pragma HLS` lines by `_PRAGMA_RE`/`_pragma_summary` (`qor_optimizer.py:233-254`), so renaming pragmas in prose is harmless but emitting them in a non-standard spelling breaks accounting.

Freely editable: the pragma list, the resource-budget wording, objective-specific guidance. Do not touch the single-```cpp-block + `#include` + top-function requirement, or the exact-signature clause. Everything else is gated **mechanically** anyway — sha dedupe, host equivalence, csynth, timing regression, full ladder with rollback (`qor_optimizer.py:399-572`).

---

### 11. NL reference-model author — `llm.py:605`

**This agent writes the oracle.** Its output becomes `nl_reference.c` → `input.c`, the reference every later stage is compared against.

`"Define exactly one externally visible function with the EXACT name the user gives; any helpers must be `static`."` / `"Every loop bound must be a compile-time constant."` / `"DO NOT take a runtime element-count/length parameter (no `int n` that bounds a loop): the automated testbench passes a random value for such a scalar, which would read past a fixed-size buffer."` / `"Return ONLY the complete C file in a single ```c fenced block."`

**Three rules are load-bearing for the soundness of the oracle, not for parsing:** the exactly-one-function rule; the compile-time-bound + no-runtime-length pair (because `testgen.py:24-28` drives scalars with random values — a runtime length parameter produces out-of-bounds reads *in the oracle itself* and the measured pass rate becomes meaningless); the single-fence rule. There is **no deterministic fallback** in NL-only mode (`cli.py:181-183` hard-errors when no backend resolved). Tests: `tests/test_strong_agents.py:216-268`.

---

### 12–13. The two inert headers — read this before you edit them

`LEVERI_TESTBENCH_SYSTEM_PROMPT` (`leveri_testgen.py:13`) and `RTL_TESTBENCH_SYSTEM_PROMPT` (`verilog_testgen.py:13`) are **never interpolated and never sent to a model**. Exhaustive grep: each appears only at its own definition plus its test file. The actual generators are deterministic Python f-strings in the same files.

**You can rewrite either header freely with zero effect on any run — that is exactly the trap.** If you want to change what those testbenches *do*, edit the templates (`leveri_testgen.py` around `:150-500`; `verilog_testgen.py` `_elem_bits` and the SV emitters). If you rewrite the text, update `tests/test_leveri_testgen.py:51-57` (seven exact substrings) and `tests/test_verilog_testgen.py:129-131` (`"ap_ctrl_hs"`, `"registered one-cycle read latency"`, `"Keep the original C in the oracle path"`) or the suite goes red. Don't casually change `LEVERI_TESTBENCH_POLICY_ID` / `RTL_TESTBENCH_POLICY_ID` — they're stamped into generated file banners and manifests, and `verilog_testgen.py:620` hardcodes the literal as a dict-get default, so a rename leaves a stale fallback string.

---

### 14. Script-level agents (outside the package sandbox)

**`scripts/cosim_repair_loop.py:86` — `REPAIR_SYSTEM`.** A second repair agent that does not reuse `REPAIR_SYSTEM_PROMPT`. Notably it says `"Keep the EXACT same top-function name and a sensible synthesizable signature"` — unlike the package agent, **this one is allowed to change the signature**. Its parser `pick_code` (`:97-108`) has no brace-balance or plausibility gate, so a truncated block gets written to disk. Two problems independent of the header: (a) `:74-81` runs `claude -p` with **no `--disallowedTools` and no `--permission-mode`** — the oracle-isolation argument at `llm.py:138-156` does not cover this loop at all; (b) `:78` is `subprocess.run(..., text=True)` with no `encoding=` — the exact gbk bug fixed in `llm.py:213`. Both verified still present.

**`scripts/generate_hls_nl_llm.py:63` — `SYSTEM_PROMPT`.** Two sentences, deliberately minimal so the arm measures the bare model. Editing it changes the arm, and `instruction_sha256` (`:125`) hashes only the instruction, **not the system prompt** — so a header edit is invisible in the output rows. Add the system prompt to that hash if you edit it and still want old and new runs comparable.

---

## Evidence policies

`EVIDENCE_POLICIES = ("none", "logs", "self", "oracle")` — `rtllm_agent.py:117`. Default `"logs"` (`:437`). A typo raises `ValueError` in `__post_init__` rather than degrading (`:438-447`) — deliberate, so `"sef"` cannot silently become a self-derived claim.

| Policy | What the repair agent actually receives | Sources tuple |
|---|---|---|
| `none` | Only `BLIND_RETRY_EVIDENCE` (`:816`): *"Tool output: withheld (evidence_policy=none -- this is a blind retry). Repair intent: you get no diagnostics; re-derive the design from the description and produce a materially different implementation."* No stage, no family, no procedure, no sub-runs. **The candidate itself is still shown.** | `("none",)` |
| `logs` | Earliest failing stage, `Failure family`, `Repair intent`, the family procedure, interface restatement for `missing_module`/`port_mismatch`, and the tool-output tail (4000 chars). Plus `timeout_diagnosis` when the family is `timeout`. | `("logs",)` or `("logs","timeout_diagnosis")` |
| `self` | Everything in `logs`, plus the instrumented self-trace when the candidate compiled and did not hang. A timeout takes the bounded diagnosis instead. | `("logs","self_trace")` |
| `oracle` | Everything in `logs`, plus the first stdout divergence vs. the reference RTL, when the candidate compiled. A timeout gets **both** extras. | `("logs","oracle_diff")` etc. |

`sources` is recorded per round as `AttemptRecord.evidence_sources` and aggregated into the row, so a report states what was **shown**, not merely what was permitted.

**Which scores are non-comparable — and the repo holds two answers.**

- `rtllm_agent.py:125` stamps **only `oracle`** as oracle-derived.
- `scripts/run_ablation.py:261-266` classifies **both `logs` and `oracle`** as `ORACLE_DERIVED`, with the reason string for `logs` being `"the repair agent sees the BENCHMARK testbench's simulation output"`, and a comment at `:252-256` stating plainly: *"`logs` is oracle-derived: the simulation transcript it forwards is the oracle's verdict on the candidate."*

The honest reading: `logs` forwards the benchmark testbench's own transcript, which typically prints expected values, so it **is** oracle *feedback* — but no reference behaviour is computed for the comparison, and it is the same channel published RTLLM-style loops use, which is why it is the default. `oracle` additionally runs the reference RTL and reports where the candidate diverges. **That is an answer key and an upper bound.** Anything produced under `oracle` must never be quoted as a headline agent score; `run_rtllm_v2.py:1203-1215` writes that banner into report.md and `:1653-1665` warns on stderr before the run.

---

## What is broken right now

### (a) Fixed and committed on this branch

1. **`ClaudeCLIClient.complete` gbk decode crash** — `llm.py:213-214` now pins `encoding="utf-8", errors="replace"`. Before this, one curly quote in model prose raised `UnicodeDecodeError` inside `subprocess.py:1599` and failed every design as `driver_error`.
2. **Stale deny-list names** — `MultiEdit`/`SlashCommand` removed from `_CLI_DISALLOWED_TOOLS` (`llm.py:157-160`); CLI 2.1.226 rejected the whole invocation otherwise (rc=1, every design `llm_error`). Successors `Edit` and `Skill` remain denied.
3. **`simulator_launch_failed` family** — `rtllm_bench.py:134`, `SIMULATOR_LAUNCH_FAILURE_CODES` at `:143`, checked at `classify_failure:536`, `sim_returncode` serialized at `:355`, and now in `UNREPAIRABLE_FAMILIES` (`rtllm_agent.py:416`). Full agent suite passes (74 tests).

### (b) Still broken — ranked by damage to the number

1. **`PowerShell` is not in the deny list** — `llm.py:157-160` names `Bash` but never `PowerShell`, which CLI 2.1.226 registers as a distinct enabled tool. With a shell tool live, the model can read the staged testbench by absolute path. **This voids the oracle-isolation claim for any agent-path number produced on native Windows**, and such a number cannot be compared to the 47/50 reference ceiling or to published RTLLM results. Fix: add `"PowerShell"`, then move to `--tools ""` as the primary fail-closed control at `llm.py:195` (a deny list can only name tools you know about — `llm.py:146-149` concedes exactly this, and PowerShell is that case having already occurred).

2. **The admissibility gate misses the severity tasks** — `_ILLEGAL_TASK_RE` (`rtllm_bench.py:559-562`) names only `display|write|monitor|strobe|fdisplay|fwrite|fmonitor|fstrobe|dump`; `_ILLEGAL_CONTROL_RE` (`:563`) only `finish|stop`. `IVERILOG_STANDARD = "-g2012"` (`:87`) enables `$info`/`$error`/`$warning`/`$fatal`, none of which are named. A candidate printing its own verdict via `$info` and terminating with `$fatal(0)` scores a strict pass on wrong RTL. **Every number this harness produces rests on this gate.** Fix: add `info|error|warning|fatal` to the task regex and `fatal` to the control regex; consider inverting to an allow-list (`$signed`/`$unsigned`/`$clog2`/`$bits`/`$time`/`$random`/`$realtime`).

3. **Compile-side launch failure is booked as a design verdict** — the `if not syntax_pass:` branch (`rtllm_bench.py:512-521`) returns before the launch-failure check at `:536`, and `compiled.returncode` is never threaded out of `_evaluate_rtl` (`:1145` computes `syntax_pass` from it and drops it). A missing or DLL-broken `iverilog` — which runs *before* `vvp* — reports `compile_error` for all 50 designs, reading as "the model cannot write Verilog". This is the same integrity bug the `simulator_launch_failed` fix was written for, left open on the half of the toolchain that fails first.

4. **`_ORACLE_CRITICAL_DENIALS` is a comment, not a control** — `llm.py:165` defines it with the warning *"trimming it would silently void the measurement"*; a repo-wide grep returns **exactly one hit, its own definition**. It also omits `PowerShell`, so even enforced it would not have caught this. Fix: assert at import or in `__init__`, and add every currently-exposed name.

5. **`--resume` provably cannot recover backend-failed designs** — `run_rtllm_v2.py:1921` tells the user *"Rerun them with --resume once the backend is back"*, but `:1763` is `done = {row["design"] for row in prior}` with no filter on `row.get("error")` or `backend_failed`. The documented recovery path does not work, and because the run also exits 3, `run_ablation`'s `DRIVER_OK_CODES=(0,3)` accepts the arm and keeps the zeros.

6. **`backend_failed` removes real failures from the adjusted basis** — `run_rtllm_v2.py:839` is `bool(llm_error) and func_success == 0`, but `llm_error` can come from a *repair-round* call that timed out after round 0 already produced real RTL and a real sim verdict. That design is then excluded from the adjusted denominator (`:938`), raising the adjusted rate, and `sample_family` returns the last round's family so the round-0 `compile_error` vanishes from the histogram.

7. **The headline row is a best-of-k rate with no label** — `run_rtllm_v2.py:1233` renders `designs_func_success/designs`, and `designs_func_success` counts a design once if **any** sample passed. At the documented sweep setting (`--samples 5 --max-repair-rounds 2`) that cell is best-of-15-generations and can read 100% while pass@1 is 20%. The label is just `func pass, official oracle (designs)`. Cheapest fix: interpolate k into the label.

8. **No toolchain preflight** — `shutil.which` appears once in `run_rtllm_v2.py`, at `:119`, for `git`. With a dead simulator the driver runs the full sweep (50 designs × samples × plan/generate/repair calls, hours and real token spend) before reporting 0/50. `local_ppa.py:301` already does this check for its own gate sim, so the pattern exists in the repo.

9. **Windows watchdog escalation raises** — `rtllm_bench.py:1009` evaluates `signal.SIGKILL` and `:1018` calls `os.killpg`; verified on this box both `hasattr(signal,"SIGKILL")` and `hasattr(os,"killpg")` are **False**. `_run`'s docstring says *"Never raises"*. Inside `evaluate_rtl` the `AttributeError` is swallowed and booked as `compile_error`; in `run_self_trace` (which catches `OSError` only) it escapes as a driver error counted as a real failure. Also `start_new_session=True` (`:981`) is ignored on Windows, so the process-group guarantee does not hold.

10. **13 more `text=True` subprocess sites without `encoding=`** — 14 files match; `llm.py:203` is the only fixed one. On the measurement path: `scripts/run_ablation.py:847` (the ablation→sweep pipe — a rejected byte kills the arm *and* truncates its log), `scripts/run_rtllm_v2.py:136` (`--clone`, and the only handler is `except OSError`, while `UnicodeDecodeError` is a `ValueError`), `scripts/cosim_repair_loop.py:78`. Fix once: a shared `run_text()` helper defaulting `encoding="utf-8", errors="replace"`, plus a lint rule.

11. **`KNOWN_ORACLE_ISSUES["clkgenerator"]` claims "no RTL can score"** (`rtllm_bench.py:186-189`) for a design the committed results *and* a fresh iverilog-14 run both score as a strict pass. `build_report` drops it from the adjusted basis anyway (`run_rtllm_v2.py:934-939`), so report.md contradicts its own designs table and deletes a genuine pass from both numerator and denominator.

12. **`tests/test_strong_agents.py:150-173` cannot detect a sandbox gap** — it substring-tests a comma-joined string, so `"Edit"` is satisfied by `"NotebookEdit"`, and `subprocess.run` is mocked so no test ever observes what the real CLI exposes. Split on `","` and assert exact set membership.

### (c) Version-coupled landmines

- **`KNOWN_ORACLE_ISSUES` is a hardcoded iverilog-12 snapshot** (`rtllm_bench.py:185-201`, conditions stated only in prose at `:40` and `:53`). It decides the denominator of every adjusted number. Measured on identical RTL, **three designs flip between iverilog 12 and 14**: `alu` and `calendar` (`missing_golden_data` → pass), `ring_counter` (pass → `functional_mismatch`). Nothing compares the list against a live `--reference` run.
- **No simulator version is recorded anywhere.** `run_config_fingerprint` (`run_rtllm_v2.py:226-241`) captures benchmark path, config, backend, model — grep for `version` across the driver and `rtllm_bench.py` returns zero hits. So a sweep started on iverilog 12, upgraded, then `--resume`d, merges two oracles into one number with no signal. That 3-design swing is larger than most agent deltas anyone would publish.
- **`RESUME_CRITICAL_KEYS` (`run_rtllm_v2.py:210-223`) omits `backend`, `model` and `oracle_derived_evidence`** — even though `run_config` carries all three, and the list's own comment names exactly this hazard for external runs. Interrupt an opus sweep, resume with haiku: it passes silently and one report attributes all 50 designs to haiku.
- **Legacy-schema rows are accepted, not rejected.** Every tolerance branch is a skip (`:251`, `:280`, `:285`). Point `--out-dir` at the committed `rtllm_v2_results` and pass `--resume`: all 50 designs read as done, nothing runs, and the report claims 0/50 with pass@1 n/a. Exit 0, no warning.
- **`--llm-cli-cmd` is `shlex.split` in POSIX mode** (`llm.py:193`, `:289`), so a Windows path with backslashes becomes `C:UserslukeAppData...` and `_cli_available` returns False with no hint. Use `posix=(os.name != "nt")`. Also: the npm `claude` shim is a `#!/bin/sh` script → `WinError 193`; `claude.cmd` with forward slashes is the launchable entrypoint.
- **The deny list has two opposite failure modes on a CLI bump**: an unknown name hard-fails the whole sweep as `llm_error`, and a *new* tool silently opens the sandbox. The loud failure pressures whoever is debugging into trimming names — which is how `PowerShell` was never noticed.
- **`docs/rtllm_v2_benchmark.md:406-408` overstates the sandbox**: `--disallowedTools` "covering every file/shell/network tool" is false on CLI 2.1.226. `docs/rtllm_v2_session_handoff.md:345` repeats it. `rtllm_v2_results/report.md:23` is the honest artifact — it records that the run was not sandboxed.

---

## Changing one agent without breaking the measurement

**0. Make the toolchain reachable first.** Both `bin` **and** `lib` must be on PATH — with only `bin`, `vvp.exe` dies with rc=3221225781 and empty stderr:

```bash
export PATH="/c/Users/luke/.local/opt/oss-cad-suite-20260806/oss-cad-suite/bin:/c/Users/luke/.local/opt/oss-cad-suite-20260806/oss-cad-suite/lib:$PATH"
```

**1. Before touching anything, establish the two oracle gates.** These construct no LLM, so they cost nothing but wall time:

```bash
python scripts/run_rtllm_v2.py --benchmark "$RTLLM_ROOT" --out-dir cal/ref   --reference
python scripts/run_rtllm_v2.py --benchmark "$RTLLM_ROOT" --out-dir cal/empty --empty-baseline
```

Required: **`--reference` = 47/50 (94.0%)**, **`--empty-baseline` = 4/50 (8.0%)** with the pass list exactly `comparator_3bit, comparator_4bit, sequence_detector, square_wave`. If `--reference` moves, your simulator changed and `KNOWN_ORACLE_ISSUES` is stale — stop and recalibrate before believing any agent number. If `--empty-baseline` grows, a new design has a vacuous oracle and its score means nothing.

**2. Run the suite.** `python -m unittest discover tests` — currently green (`tests.test_rtllm_agent` is 74/74 OK). The prompt-level assertions that will catch a bad header edit are `tests/test_rtllm_agent.py:556-566` (contract reaches generator), `598-617` (description reaches every prompt, oracle never does), `766-790` (policy ladder + stamp), `808-816` (family coverage), `1017-1067` (end-to-end prompt content), `1031-1043` (golden markers absent under `none`/`logs`/`self`).

**3. Prove the parser still reads the model.** For any generator- or repair-header edit, run a handful of designs and check `results.jsonl` for a rise in `missing_module` — that is the signature of a parser that stopped matching. `extract_verilog` fails **silently to `""`**, which then scores as a design failure, so a broken fence tag looks exactly like a bad model.

**4. Score against the same RTL, not a new sweep.** The cheapest way to isolate a prompt change from simulator noise:

```bash
python scripts/run_rtllm_v2.py --benchmark "$RTLLM_ROOT" --out-dir cal/ext \
  --external-rtl rtllm_v2_results/designs --label baseline
```

On this box that gives **44/50 official**, triaging to **40/44 (90.9%)** on a signal basis: 4 vacuous-oracle, 2 unscorable (`radix2_div`, `ring_counter`), 1 reference-wrong (`clkgenerator`), 39 real passes, 4 real failures (`asyn_fifo`, `barrel_shifter`, `serial2parallel`, `signal_generator`). The committed `results.jsonl` says 43/50 on iverilog 12 — **the 1-point difference is the simulator, not the agent.**

**5. Compare the right number.** For an agent-quality claim use **pass@1 round-0** (`_passed_first_round`, `run_rtllm_v2.py:776-787`) — that is the RTLLM-comparable single-shot figure. Do **not** quote the headline `func pass (designs)` cell when `--samples > 1`; it is best-of-k. Do **not** quote anything from an `--evidence-policy oracle` run. Use a fresh `--out-dir` per prompt variant — `--resume` cannot tell two prompts apart, and `RESUME_CRITICAL_KEYS` does not carry model or backend either.

**6. If your edit touches the evidence ladder**, also update `run_ablation.py:261` `EVIDENCE_TRACKS` and the four stamps enumerated at `rtllm_agent.py:121-125`, or the arm prints as *"unclassified evidence policy: do not quote"*.

**7. Until `PowerShell` is denied (item b-1), treat every agent-path number from this box as unsound as a blind spec-to-RTL measurement.** The reference/empty/external-RTL calibrations are unaffected — they construct no LLM.