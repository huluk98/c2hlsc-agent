# c2hlsc-agent — function reference & agent plan

*Generated 2026-07-06 at commit `8576b04` (merge of PR #1 "Fix review findings").*

> This file is a point-in-time snapshot of the function reference. For the current
> flow see [`docs/workflow_end_to_end.md`](docs/workflow_end_to_end.md), and for the
> per-component contracts see [`docs/agent_components.md`](docs/agent_components.md),
> generated from `c2hlsc_agent/components.py`. The "Adding real agents" plan at the
> bottom of this file has since been implemented as that component scaffold: each of
> the eight declared agents is now bound to its real entry points and has an
> executable `run(context) -> ComponentOutcome` adapter.

## How the pipeline fits together

`c2hlsc_agent` converts an ordinary C top function into a Vitis HLS C/C++ project and
verifies **functional equivalence** through a short-circuiting ladder:
**host software equivalence** (golden-C oracle testbench via `make test`) → **Vitis CSim**
→ **CSynth** → **C/RTL CoSim**. The runtime flow in `cli.py run_convert` is:
`analyze_source` → `generate_hls_sources` → `write_project` → `verify_project` →
`classify_failure` → `repair_project` → re-verify (bounded by `--max-iterations`, guarded
against oscillation). Two invariants hold everywhere: the **verifier is the equivalence
gate** (LLM output is never trusted, only tested), and the **original C is never handed to
the model** as a reference to copy — it is compiled as a macro-renamed golden oracle.
The LLM layer is pluggable (`none` / OpenAI-compatible incl. local / Anthropic), and the
newest addition (`scripts/cosim_repair_loop.py`) drives repairs through the **local
`claude` CLI by default** — subscription auth, no API key.

## What changed in the July-2 merge (PR #1)

- `analyze`: `==` no longer counted as a write (pointer-direction inference bug); C99
  `restrict` stripped from types/signatures so generated C++ compiles.
- `hls_runner`: `_gate_cosim_on_log` — CoSim "pass" is downgraded to fail when the log
  reports a co-sim failure even if Vitis exits 0.
- `testgen`/`leveri`: float comparisons use a relative tolerance instead of exact `!=`.
- `llm`: Anthropic retry tuple fixed (no accidental catch-all).
- `convert`/`hlsc_repair_agent`: LLM errors are surfaced (stderr / transformation notes),
  not silently swallowed.
- `cli`: repair-oscillation guard via project content hashes; `--max-iterations` no longer
  overrides config; `keep_going` not clobbered; `_external_failure_state` hardened.
- `config`: loop knobs (`max_iterations`, `auto_repair`, `keep_going`, `run_vitis`) read
  from config; inert `allow_performance_pragmas` removed; dead `templates/*.j2` deleted.
- New `tests/test_review_fixes.py` (9 regression tests), `tests/test_analyze.py` additions.

---

## Package: `c2hlsc_agent/`

### `cli.py` — argparse CLI: `convert` and `repair` subcommands
- **`build_parser()`** — defines `convert` (input/top/out, part/clock/num-tests, `--run-vitis`, `--use-llm`/`--no-llm`, `--llm-backend {auto,none,anthropic,openai}`, `--llm-base-url`, `--llm-model`, `--max-iterations`, `--auto-repair`, `--keep-going`) and `repair` (`--project`, `--stage {software_equivalence,csim,csynth,cosim}`, `--evidence`/`--evidence-text`, `--iteration`).
- **`run_convert(args)`** — full pipeline: merge config → build LLM client (warn+fallback if unavailable) → analyze → generate → write project → loop `verify_project`; on fail with `--auto-repair` calls `repair_project`; stops on pass, no-change repair, iteration cap, or when `_project_signature` repeats (oscillation guard); writes reports; exit 0 only on pass.
- **`_project_signature(project_dir)`** — SHA-256 over `src/hls_top.cpp`+`hls_top.hpp`; used to detect a repair that reproduces a previously seen state.
- **`_load_project_top(project_dir)`** — reads `top` back from `conversion_report.json` for `repair` defaulting.
- **`_read_evidence(paths, inline)`** — concatenates inline text and `--evidence` files into one evidence blob.
- **`_external_failure_state(stage, evidence, run_vitis)`** — synthesizes a `VerificationState` from an external run: phases before `stage` assumed pass, `stage` failed with the evidence, later phases blocked; the declared stage is force-appended if not in the plan.
- **`run_repair(args)`** — offline repair from external evidence: rebuild analysis, synthesize failure state, `repair_project`, write `manual_repair_report.json`; exit 0 iff a change was applied.
- **`main(argv)`** — dispatch.

### `config.py` — dataclasses + minimal YAML/JSON config loading
- **`ArgumentConfig`** — per-argument metadata: `direction`, `length`, `range`, `interface`.
- **`AgentConfig`** — all knobs: `input_files`, `top`, `arguments`, `num_tests=100`, `directed_tests`, `part`, `clock=10.0`, `interface_mode`, `allow_pragmas`, `cosim_tool`, `rtl`, `seed=1`, `max_iterations`, `auto_repair`, `keep_going`, `run_vitis`, `use_llm`, `llm_backend/model/base_url`.
- **`_parse_scalar(value)`** — scalar coercion (bool/none/list/quoted/int/float) for the fallback YAML parser.
- **`_minimal_yaml(text)`** — dependency-free indent-based YAML subset parser (used when PyYAML is absent).
- **`_load_data(path)`** — JSON if `{`-led, else PyYAML, else `_minimal_yaml`.
- **`_as_list(value)`** / **`_argument_config(data)`** — normalization helpers.
- **`load_config(path)`** — builds `AgentConfig` from a file, resolving paths relative to it; loop knobs come from config too.
- **`merge_cli_config(config, args)`** — CLI flags override config; `--use-llm`/`--no-llm` and `--run-vitis`/`--no-run-vitis` are explicit-only overrides; `keep_going` only set to True, never clobbered to False.

### `diagnostics.py` — diagnostic value types
- **`Diagnostic`** — severity/code/message/location/suggestion (+`to_dict`).
- **`DiagnosticBag`** — `add`, `extend`, `has_errors` (any error-severity), `by_severity`, `to_list`.

### `analyze.py` — regex-based static analysis of the C top function
- **`FunctionArg`** — parsed parameter: raw text, name, `c_type`, pointer depth, array dims, `direction`, `length`, `scalar_range`, `interface`; `is_pointer_like` property.
- **`FunctionInfo`** / **`AnalysisResult`** — top-function record (signature/body/definition) and the analysis bundle (function, diagnostics, type mappings, unsupported constructs).
- **`strip_comments(source)`** — removes `/*…*/` and `//…`.
- **`_find_matching_brace(source, open_index)`** — string-aware brace matcher.
- **`_split_params(params)`** — top-level comma split respecting nesting.
- **`_parse_arg(raw, metadata)`** — parses one parameter; strips `restrict`/`__restrict__` from raw and type (C99→C++ fix); infers length from literal array dims; applies config metadata.
- **`_extract_function(source, top, path, config)`** — regex-locates the top function, extracts return type/params/body/signature.
- **`_guess_arg_name(raw)`** — last identifier of a parameter for config lookup.
- **`_infer_pointer_directions(function, config)`** — write pattern `=(?!=)` (the `==` fix), read pattern after removing LHS writes → input/output/inout.
- **`_unsupported(function)`** — error diagnostics for malloc/free, rand/qsort/etc., system calls, file/console I/O, function pointers, unbounded loops, recursion, unrestricted pointer arithmetic, variable-length arrays.
- **`_type_mappings(function)`** — identity original→generated type rows for the report.
- **`analyze_source(input_file, top, config)`** — orchestrates all of the above; defaults missing pointer bounds to length 16 with a warning.

### `convert.py` — HLS-C source generation (deterministic + LLM)
- **`GeneratedSource`** — header/source text, transformation ledger, interface-pragma rows, generator prompt id.
- **`_include_for_types(args, return_type)`** — `<stdint.h>` (+`<ap_int.h>` when ap types appear).
- **`_pragma_lines(config, args)`** — INTERFACE pragmas per `interface_mode` (`s_axilite`/`ap_memory`/`m_axi`/`axis`) + reasons; none in `default` mode.
- **`_generate_conservative_sources(analysis, config)`** — verbatim body copy with generated header/pragmas; the safe baseline.
- **`generate_hls_sources(analysis, config, llm=None)`** — builds the conservative version first; when `use_llm`, prompts the model (policy `hlsc_generator_vitis_beginner_v1`), extracts a validated translation unit via `extract_hls_source`, else falls back recording the error reason in the transformation ledger.

### `hlsc_generator.py` — the generator prompt contract (no I/O)
- **`HLSC_GENERATOR_PROMPT_ID` / `HLSC_GENERATOR_OUTPUT_SECTIONS` / `HLSC_GENERATOR_SYSTEM_PROMPT`** — the 8-section beginner-facing Vitis HLS generation policy (assumptions → hotspots → original → annotated code → impact → trade-offs → Intel notes → checklist).
- **`HlscGeneratorContract`** / **`get_hlsc_generator_contract()`** — dataclass + accessor (`owns_testbench=False`).
- **`render_hlsc_generator_task(input_code)`** — wraps the input C in the task prompt.

### `llm.py` — pluggable LLM backends + prompt builders + response parsing
- **`LLMClient`** (Protocol) — `complete(system, user, max_tokens) -> str`.
- **`AnthropicLLMClient`** — Anthropic Messages API wrapper; tries adaptive thinking + high effort, retries plain on `TypeError`/`BadRequestError` (fixed tuple — no catch-all).
- **`OpenAICompatibleLLMClient`** — stdlib-only Chat Completions client for local (Ollama/LM Studio/llama.cpp/vLLM) or cloud endpoints.
- **`_text_from_response` / `_openai_text`** — response text extraction.
- **`_env(*names)` / `_anthropic_installed()` / `_is_local_url(url)`** — env/capability helpers.
- **`resolve_backend(config)`** — `auto`: configured base-url → anthropic (if SDK+key) → OpenAI key → none.
- **`missing_llm_reason(config)`** — human-readable reason the LLM path is unavailable.
- **`build_llm_client(config)`** — constructs the resolved backend or returns `None` (deterministic fallback); never networks at construction.
- **`_argument_lines` / `_diagnostic_lines`** — prompt fragments from the analysis.
- **`build_generator_user_prompt(analysis, original_source)`** — task + contract + diagnostics + hard AUTO-RTL requirements (exact signature, single self-contained ```cpp unit, equivalence-preserving pragmas only).
- **`REPAIR_SYSTEM_PROMPT`** / **`build_repair_prompt(...)`** — minimal-patch repair prompt with truncated evidence (1600 chars) and the current file.
- **`_FENCE` / `extract_code_blocks(text)`** — fence-length-aware code block extraction.
- **`_defines_function` / `_braces_balanced` / `_is_code_lang` / `_normalize`** — structural checks.
- **`is_plausible_translation_unit(code, top)`** — cheap gate: balanced braces + defines the top.
- **`extract_hls_source(text, top, original, header_include)`** — picks the last non-echo C/C++ block defining the top (preferring after the "Vitis HLS annotated code" marker), validates it, prepends the header include if missing; returns `None` → caller falls back.
- **`extract_full_file(text, must_contain)`** — longest C/C++ block (optionally containing a symbol) for repair responses.

### `testgen.py` — deterministic golden-C oracle testbench generator
- **`_LENGTH_NAMES`** — scalar names recognized as array-length parameters.
- **`_is_unsigned` / `_scalar_decl` / `_storage_type` / `_value_print`** — codegen helpers.
- **`_init_array(arg)`** — outputs get sentinel fills; inputs get patterned stimulus (zeros / all-ones / min-max / alternating / random by test index).
- **`_call_args(prefix, args)`** — `ref_`/`hls_` argument lists.
- **`_looks_like_length_name` / `_active_length_arg`** — detect a bounded scalar that acts as the array's active length → comparisons clamp to it.
- **`_scalar_log_expr` / `_array_trace_lines`** — mismatch-context logging fragments.
- **`_contract_comment(...)`** — human-readable testbench contract header (warns when nothing observable is compared).
- **`generate_testbench(analysis, config)`** — emits the complete C++ testbench: includes `../input.c` inside `extern "C"` with the top macro-renamed to `*_ref` (and `restrict` defined away), deterministic `mt19937_64(seed)` stimulus, directed patterns, sentinel-filled outputs, `values_equal` with relative float tolerance (1e-6), per-element mismatch lines in the exact format `parse_mismatches` reads, `clamp_count` for active lengths.

### `leveri_testgen.py` — HLS-LeVeri paired-trace testbenches + coverage hooks
- **`LEVERI_TESTBENCH_POLICY_ID` / `LEVERI_REFERENCE_REPO` / `LEVERI_TESTBENCH_SYSTEM_PROMPT`** — shift-left testbench policy (paired golden/HLS traces, dual-tier checking, gcov/KLEE hooks).
- **`LeVeriTestbenchContract` / `get_leveri_testbench_contract()`** — contract dataclass/accessor.
- **`LeVeriTestbenchBundle`** — the 7 generated artifacts as strings.
- **`_is_unsigned` / `_storage_type` / `_scalar_decl` / `_init_array` / `_call_args`** — same stimulus helpers, keyed by `cycle`.
- **`_header_and_roles(args, ret)`** — CSV header row + role row (`meta`/`in`/`out`), expanding arrays per element and splitting `inout` into `_in`/`_out` columns.
- **`_write_header_line` / `_array_declarations` / `_scalar_declarations` / `_array_initializers` / `_write_value_line` / `_write_row_lines`** — trace-writing codegen.
- **`_common_helpers(seed)`** — shared C++ stimulus templates (`random_value`, `bounded_scalar`, `patterned_value`, `output_sentinel`, `write_csv_value`, `make_trace_rng`).
- **`_render_trace_tb(analysis, config, target_name, output_csv, include_block)`** — one trace testbench (golden `*_ref` or HLS top) writing header+roles+per-cycle CSV rows.
- **`_compare_script()`** — emits `tb/leveri_compare.py`: static checks (header/role/cycle-count/stimulus-column equality) + dynamic output check with `values_match` float tolerance.
- **`_klee_driver(analysis)`** — KLEE symbolic driver for the golden top (`klee_make_symbolic` + `klee_assume` range constraints).
- **`_gcov_script()`** — emits `tb/run_gcov.py`: builds both trace TBs with `--coverage`, runs them + the comparator, invokes gcov; passes iff instrumentation produced data (gcov's exit code is advisory).
- **`_klee_script()`** — emits `tb/run_klee.py`: env-resolved klee/clang++ (no hardcoded paths), compile to LLVM bitcode, run KLEE with timeout, report ktest count; skips gracefully when tools are missing.
- **`_manifest(analysis, config)`** — `tb/leveri_manifest.json`: policy, checks, coverage hooks, argument metadata.
- **`generate_leveri_testbenches(analysis, config)`** — assembles the whole bundle.

### `hls_project.py` — project emission (files on disk)
- **`ProjectFiles`** — root + generated file list.
- **`render_run_hls` / `render_run_csim` / `render_run_csynth` / `render_run_cosim`** — TCL scripts (full ladder and per-phase; cosim honours `cosim_tool`/`rtl`).
- **`render_makefile(config)`** — Makefile with `test` (oracle TB), `leveri-test` (paired traces + compare), `gcov-coverage`, `klee-coverage`, `vitis`, `clean`.
- **`render_run_all()`** — `run_all.sh`: `make test` then vitis_hls if present.
- **`write_project(out_dir, analysis, generated, config)`** — writes input.c copy, src/, tb/ (oracle + 6 LeVeri artifacts), 4 TCLs, Makefile, run_all.sh; sets executable bits; returns `ProjectFiles`.

### `equivalence.py` — verification value types + subprocess runner
- **`PhaseResult`** — name/status/returncode/stdout/stderr/log_path/summary (+`to_dict`, output truncated to 4000 chars).
- **`Mismatch`** / **`format_mismatch`** — structured testbench mismatch (test index, argument, element, expected/actual, seed).
- **`parse_mismatches(text)`** — regex-parses the testbench's `Mismatch test=… arg=… expected=… actual=… seed=…` lines (array and return forms).
- **`run_command(command, cwd, phase, timeout)`** — Popen in its own session; on timeout SIGTERM→SIGKILL the process group; always writes `<phase>.log`.
- **`VerificationState`** — phase map + mismatches; `add_phase`, `status_for` (default "skipped").

### `hls_runner.py` — the verifier ladder
- **`PHASE_ORDER`** — `software_equivalence, csim, csynth, cosim`.
- **`earliest_failing_phase(state, run_vitis)`** — first required phase not "pass", else `None`.
- **`run_software_equivalence(project_dir)`** — `make test` (timeout 120s) → `PhaseResult`.
- **`run_vitis(project_dir, run_requested)`** — CSim (600s) → CSynth (1200s) → CoSim (600s) via the per-phase TCLs, short-circuiting with "blocked" statuses; "vitis_hls not found" fails csim and blocks the rest.
- **`_COSIM_FAILURE_MARKERS`** / **`_gate_cosim_on_log(result)`** — *(new)* downgrade CoSim pass→fail when the log carries an explicit co-simulation failure marker, so exit 0 can't defeat the equivalence gate.
- **`verify_project(project_dir, run_vitis, verbose)`** — host equivalence (+`parse_mismatches`), blocked-cascade on failure, else the Vitis phases; returns `VerificationState`.

### `agent_loop.py` — the multi-agent architecture (declarative) + failure routing (live)
- **`AgentProcedure`** — name/role/owns/inputs/outputs/stop_condition (+`to_dict`). **Descriptive, not executable.**
- **`FailureAnalysis`** — family/owner_agent/next_action/evidence_needed/repair_scope/status.
- **`multi_agent_procedures()`** — the 8 declared agents: `contract_planner`, `shift_left_testbench_agent`, `hlsc_generator_agent`, `cosim_operator`, `failure_analyst`, `hlsc_repair_agent`, `rtl_optimizer_agent`, `audit_memory_agent`.
- **`_phase_text(state, phase)`** — summary+stdout+stderr for a phase.
- **`classify_log_family(phase, text)`** — regex triage: toolchain_unavailable / timeout_or_deadlock / behavioral_mismatch / interface_contract / memory_pointer / numeric_bitwidth / loop_scheduling / non_synthesizable_construct / phase defaults.
- **`classify_failure(state, run_vitis, diagnostics_has_errors)`** — maps the earliest failure to a `FailureAnalysis` with owner agent and next action (static-rejected → contract_planner; host mismatch → failure_analyst; TB/compile issues → testbench agent; csynth → repair agent; cosim mismatch → PMLC-style analysis; all-pass → rtl_optimizer, status "pass").
- **`render_procedures_markdown()`** — docs rendering of the procedures.
- **`hlsc_generator_policy()` / `leveri_testbench_policy()`** — contract dicts for reports.

### `hlsc_repair_agent.py` — mechanical + LLM repair with audit trail
- **`RepairFileChange`** — path/action/sha256 before-after/unified diff.
- **`RepairOutcome`** — iteration/stage/family/owner/status/summary/targets/changes/evidence/next_action/repair_scope; `changed` property.
- **`load_repair_audit(project_dir)` / `clear_repair_audit`** — read/reset `repair_audit.json`.
- **`repair_project(project_dir, analysis, config, state, iteration, llm=None)`** — the repair driver: classify → run the 4 mechanical repairs → if none applied and LLM enabled, `_llm_repair` → append audit; statuses `pass`/`blocked`/`applied`/`applied_llm`/`no_change`.
- **`_llm_repair(...)`** — only ever rewrites `src/hls_top.cpp`; testbench and golden `input.c` are never model-writable; response structurally validated (`extract_full_file` + `is_plausible_translation_unit`); errors printed to stderr (not swallowed).
- **`_phase_evidence(state, phase)`** — summary+stdout+stderr+log file text.
- **`_append_audit` / `_sha256` / `_relative` / `_rewrite_file`** — audit plumbing; `_rewrite_file` writes and returns the diffed change record.
- **`_ensure_includes(text, includes)`** — insert missing `#include`s after the last include.
- **`_includes_needed_from_evidence(evidence)`** — maps undeclared-symbol errors to standard headers (stddef/limits/string/math/ap_int).
- **`_repair_missing_standard_includes`** — applies the above to `hls_top.hpp`.
- **`_replace_restrict_tokens` / `_ensure_testbench_restrict_macro` / `_repair_restrict_for_cpp`** — C99 `restrict` → `__restrict__` across sources + TB guard.
- **`_candidate_missing_symbols(evidence)`** — extracts undeclared/undefined symbol names from compiler errors (excluding stdlib names).
- **`_has_function_definition(source, symbol)`** — regex definition check in input.c.
- **`_support_include_block(top)` / `_repair_missing_original_support`** — if the failing symbols are helper functions defined in the original C, injects a guarded `#include "../input.c"` with the top renamed, so preserved top bodies can call original helpers.
- **`_repair_invalid_interface_pragmas(project_dir, phase, family, evidence)`** — strips generated `#pragma HLS INTERFACE` lines after interface-related Vitis failures, leaving an explanatory comment.

### `report.py` — human + machine reports
- **`_table(headers, rows)`** — markdown table helper.
- **`final_status(state, run_vitis, diagnostics_has_errors)`** — overall pass/fail across required phases.
- **`write_reports(project, analysis, generated, config, state, iterations, repairs)`** — writes `conversion_report.md` (status, inputs, files, type mapping, directions, pragmas, transformations, unsupported constructs, diagnostics, coverage summary, phase results, `classify_failure` assessment, repair audit table, mismatches) and `conversion_report.json` (same, machine-readable, incl. per-phase dicts).

---

## Scripts: `scripts/`

### `cosim_repair_loop.py` — **closed-loop CoSim + Claude-CLI repair (the new key script)**
- Loop per record: write Vitis project → run CSim→CSynth→CoSim (per-phase timeout) → on failure, feed the NL spec + failing source + log tail to the repairer → rewrite `dut.cpp` → re-run; up to `--max-iterations`.
- **`make_completer(args)`** — repair backend factory: **`claude-cli` (default)** runs `claude -p --model <opus>` with the prompt on stdin (`--claude-cmd "ssh you@mac claude"` drives a remote Mac from the Vitis server); `anthropic` uses the API via `AnthropicLLMClient`.
- **`REPAIR_SYSTEM`** — Vitis-repair system prompt (same top name, synthesizable, one ```cpp block).
- **`pick_code(resp, top)`** — first C/C++ block defining the top, else first block, else raw text.
- **`write_project(...)`** — reuses `write_design` + the 4 TCL renderers from the batch scripts.
- **`failing_evidence(design_dir, result)`** — last 120 lines of the earliest failing `vitis_<phase>.log`.
- **`repair(complete, record, hls_cpp, stage, evidence)`** — builds the user prompt and extracts the corrected source.
- **`select(records, args)`** — record selection: `--only-failing results.jsonl`, `--record-id`, or offset/limit.
- **`main()`** — drives the loop; writes `results.jsonl` (per-record outcomes with per-attempt status) and `repaired_corpus.jsonl`; exit 0 iff no failures.

### `run_hls_nl_vitis_batch.py` — batch Vitis runner over HLS_NL records
- **`render_verilog_tcl` / `render_csim_tcl` / `render_csynth_tcl` / `render_cosim_tcl`** — per-phase TCL (csim+csynth combined "verilog" mode, or split phases for full cosim).
- **`resolve_vitis_hls(path_arg, generate_only)`** — `--vitis-hls-bin` → `VITIS_HLS_BIN` → PATH; validates existence/executability.
- **`verilog_files(design_dir)` / `cosim_artifacts(design_dir)`** — artifact discovery under `hls_nl_project/`.
- **`text_tail` / `subprocess_output_text`** — log helpers.
- **`generate_designs(args)`** — writes a design dir (dut/tb/TCLs) per parseable record; returns designs+skipped.
- **`VitisProcessResult`** / **`terminate_process_group` / `kill_process_group` / `run_vitis_command`** — process-group-safe execution with timeout escalation, partial-output capture.
- **`phase_plan(run_full_cosim)`** — `[csim, csynth, cosim]` or `[verilog_csynth]`.
- **`run_design(vitis_hls, design, timeout, run_full_cosim, log_tail_lines)`** — runs the plan, per-phase logs + aggregate log, statuses `pass`/`fail`/`timeout` with `failed_phase`; final `pass` additionally requires synthesized Verilog to exist.
- **`write_report(out_dir, report)`** — `vitis_batch_report.json` + `vitis_batch_results.jsonl` (and legacy verilog-named copies).
- **`_config_path` / `load_config` / `finalize_args`** — optional JSON config merge with CLI precedence.
- **`main()`** — generate → run (optionally `--stop-on-fail`) → report; exit 0 iff all pass.

### `generate_hls_nl_testbenches.py` — testbench scaffolds for HLS_NL records
- **`Arg`** — parsed parameter with `base_type`, `is_reference_or_pointer`, `is_stream`, and heuristic `direction` (stream name hints; non-const ref/pointer → output).
- **`FunctionSig`** — return type/name/args/signature.
- **`split_top_level_commas` / `find_matching` / `parse_arg` / `extract_function`** — angle/paren/bracket-aware signature parser for ap_int/ap_fixed/hls::stream types.
- **`extract_design_title` / `load_records` / `record_source_file` / `record_design_title` / `record_id_for` / `identifier` / `macro_lines` / `cpp_string`** — record utilities (`load_records` handles both JSON array and JSONL).
- **`is_integer_type` / `value_expr` / `print_expr` / `declare_arg` / `call_arg`** — stimulus codegen (reset-name awareness, streams pre-filled with 4 values, 16-element buffers for pointers).
- **`find_arg` / `input_args` / `output_args` / `mismatch_line`** — oracle helpers.
- **`DRIVER_COMMENT`** — the default no-golden-assert stance: **CoSim is the equivalence oracle**.
- **`bit_width(base_type)`** — ap_int/intN width for width-correct golden math.
- **`semantic_checks(sig, oracle_mode)`** — default `driver` mode emits stimulus-only TBs (smoke/property); opt-in `semantic` mode emits heuristic golden checks for recognized patterns (calculator, comparator, width-aware full adder, adder-subtractor, subtractor, multiplier, max, gray code, mux) that can false-fail.
- **`render_testbench(record, sig, record_id, oracle_mode)`** — the full `tb.cpp` (deterministic `pattern_value`, 64 tests).
- **`render_tcl(sig, part, clock)`** — combined csim+csynth+cosim TCL.
- **`write_design(...)`** — writes `dut.cpp`, `tb.cpp`, `run_hls.tcl`, `instruction.txt`; returns the manifest row.
- **`main()`** — batch generation + `manifest.json` + README table.

### `repair_hls_nl_dataset.py` — auditable mechanical dataset repair
- **`RepairResult`** — status (accepted/quarantine/deleted) + code + repairs/warnings/reasons/features.
- **`sha256_text` / `extract_design_title`** — utilities.
- **`strip_markdown_fence(code)`** — unwraps ```-fenced code.
- **`normalize_includes(code)`** — dedupe includes, drop unused `hls_stream.h`, add missing `ap_int.h`/`ap_fixed.h`.
- **`detect_features(code)`** — includes/pragmas/static/main/testbench-language/STL-IO/dynamic-alloc/non-synth-IO/unbounded-loop/placeholder/brace-balance flags.
- **`extract_top_function(code)`** — first plausible function name.
- **`likely_semantic_warnings(prompt, code)`** — targeted heuristics (priority-encoder index bug, division-without-guard, level-sensitive clock modeling).
- **`repair_record(record, index)`** — normalize → strip fence → fix includes → feature-based quarantine; unbounded-loop records are deleted (code omitted, hashes kept).
- **`write_jsonl` / `main()`** — emits `all/accepted/quarantine/deleted` JSONLs, an SFT chat-format JSONL of accepted records, and JSON+MD repair reports with counts.

### `generate_hls_nl_llm.py` — batch NL→HLS-C generation through pluggable backends
- **`code_from_response(text)`** — longest C/C++ block, else raw text.
- **`build_client(args)`** — `openai` (local default `http://localhost:11434/v1`) / `anthropic` / `reference` (re-emit dataset code) / `replay` (pre-recorded responses).
- **`done_record_ids(out_path)`** — resume support: skip ids already in the output JSONL.
- **`generate_one(client, args, record, index)`** — one generation → result row (`ok`, `hls_cpp`, error captured rather than aborting).
- **`main()`** — threaded batch (`--workers`), append-only output, progress rate lines; output schema is drop-in input for `run_hls_nl_vitis_batch.py`.

### `export_cosim_successes.py` — clean corpus of CoSim-passing cases
- **`load_report` / `stable_case_name`** — report validation/naming.
- **`copy_minimal_case(row, dest, include_logs)`** — copies dut/tb/tcl/instruction + writes `cosim_pass_evidence.json` (status, artifacts, command, log tail).
- **`write_jsonl` / `build_parser` / `main_from_args` / `main`** — refuses non-full-cosim reports unless `--allow-non-cosim`; wipes+rebuilds out dir; writes manifest, `passes/failed/skipped.jsonl`, README.

### `retarget_hls_prompts_to_vitis.py` — prompt vendor retargeting
- **`replace_vivado_terms(text)`** — Vivado (HLS) → Vitis (HLS) with edit records.
- **`retarget_instruction(instruction)`** — swaps the generic HLS preamble for the Vitis generation preamble (or prepends it), normalizes line endings.
- **`load_records` / `main()`** — rewrites instructions and code, reports edit counts, remaining-vivado and missing-vitis records, samples.

### `run_vitis_bundle.py` — portable JSON bundle runner
- **`repo_root` / `default_bundle` / `default_work_dir`** — defaults (tracked simple_calculator cosim bundle).
- **`resolve_vitis_hls(vitis)`** — binary path, install root (globbed `Vitis_HLS/*/bin`), or PATH name.
- **`bundle_file_text` / `unpack_bundle`** — writes the bundle's `files` map (path-traversal guarded), requires `run_hls.tcl`.
- **`main()`** — unpack (+`--unpack-only`) → run `vitis_hls -f run_hls.tcl` → log + tail on failure.

### `run_vitis_with_bin.py` — convert-with-explicit-vitis wrapper
- **`VITIS_HLS_BIN` / `VITIS_HLS_BIN_FILE`** — inline constant or `vitis_hls_bin_path.txt`.
- **`_repo_root` / `_read_path_file` / `_auto_find_vitis_hls`** — resolution: flag → env → constant → file → common-install glob.
- **`_split_args` / `main()`** — validates the binary, prepends its dir to PATH, sets `VITIS_HLS_BIN`/`XILINX_HLS`/`PYTHONPATH`, then execs `python -m c2hlsc_agent.cli convert --run-vitis <passthrough args>`.

### `collect_debug_bundle.py` — debug tarball
- **`DEFAULT_FILES`** — reports, phase logs, TCLs, sources, testbench.
- **`copy_if_exists` / `main()`** — copies those + all `c2hlsc_project` logs/reports into `debug_bundle_<name>.tar.gz`.

---

## Adding real agents

**Current state.** The 8 agents in `agent_loop.multi_agent_procedures()` are *declarative
role descriptions*, not executable agents. The live pipeline is deterministic with exactly
three model call sites: generation (`convert.generate_hls_sources`), in-loop repair
(`hlsc_repair_agent._llm_repair`), and the standalone corpus repair loop
(`scripts/cosim_repair_loop.py`, Claude-CLI-first). `classify_failure` already *routes*
failures to named agent owners — the routing table is live, the agents behind it are not.

**The seams** (where a live agent can replace/augment deterministic logic):
1. **`contract_planner`** over `analyze.analyze_source` — an LLM pass to propose argument
   directions/bounds/ranges where the regex inference is uncertain, emitting the same
   `ArgumentConfig` shape (verifier still gates).
2. **`shift_left_testbench_agent`** over `testgen`/`leveri_testgen` — model-proposed extra
   directed stimuli or coverage-driven refinement; keep the deterministic TB as the floor.
3. **`failure_analyst`** over `classify_failure` — replace regex log triage with a model
   that produces the same `FailureAnalysis` dataclass, plus PMLC-style slicing for cosim
   mismatches (the `Mismatch` records and phase logs are already structured evidence).
4. **`rtl_optimizer_agent`** — a new post-pass after `final_status == "pass"`: propose
   pragmas, then rerun the *full* ladder before accepting (the stop condition is already
   specified in the procedure declaration).
5. **`cosim_operator` / `audit_memory_agent`** — already effectively implemented as
   `hls_runner.verify_project` and `repair_audit.json`; a memory agent would promote
   audited repair successes into retrieval for future prompts.
6. **Interface plumbing** — `LLMClient.complete(system, user)` is single-shot. The
   Claude-CLI completer in `cosim_repair_loop.make_completer` is the template for
   subscription-auth agents; a richer loop (multi-turn, tool use) would wrap `claude -p`
   with session resume or the Agent SDK, but every agent must keep returning
   *verifier-checkable artifacts*.

**Order of increments.** (1) failure_analyst (pure classification, zero risk — output
gated by the same dataclass), (2) contract_planner suggestions surfaced as config
proposals, (3) testbench augmentation, (4) rtl_optimizer with full re-verification,
(5) retrieval memory from `repair_audit.json`.

**Do not break:** `extract_hls_source`/`is_plausible_translation_unit` structural gates,
the deterministic fallback in `generate_hls_sources`, the never-hand-original-C-to-model
rule, `_gate_cosim_on_log`, the repair audit provenance, and the oscillation guard in
`run_convert`.
