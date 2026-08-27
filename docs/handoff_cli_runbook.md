# CLI Handoff: Implement the Combined Generation Workflow

> **Status:** Tasks 1–5 below are implemented on branch
> `claude/combined-generation-workflow` (LeVeri gate in the ladder, golden-trace
> smoke, structured divergence evidence, K=3 documentation, drift check, plus
> Windows support via `C2HLSC_VITIS_BIN` and a make `PYTHON` override). This
> document remains the specification of record; the backlog items in §4 are
> still open.

You are an agent working in the repository `huluk98/c2hlsc-agent`. Your job is
to implement the combined workflow described in
`docs/handoff_generation_workflow.md`: the evidence-driven generation/repair
spine (planner → generate → four-stage verify → analyze → repair, bounded
budget) with LeVeri-style paired testbench generation wired in as a
**shift-left** stage. Work top to bottom through the task list in §4. Read
`docs/handoff_generation_workflow.md` and `AGENTS.md` before writing any code.

## 1. Settled decisions — do not re-litigate

1. **Testbench generation needs no HLS-C.** `generate_leveri_testbenches()`
   (`c2hlsc_agent/leveri_testgen.py:686`) consumes only the interface contract
   from `analyze_source(input.c, top, config)` plus seed/num_tests. The golden
   testbench and KLEE driver compile against `input.c` alone and can run
   before any candidate exists. Only the HLS-side trace (and therefore the
   dual-tier comparison) is gated on the first candidate `src/hls_top.{cpp,hpp}`.
   Therefore: generate both testbenches at contract time; run the golden side
   immediately; run the paired comparison after each candidate/repair.
2. **Keep the repo ladder** `software_equivalence → csim → csynth → cosim`
   (host golden-C equivalence stays stage 1; it is stronger than a bare
   compile stage).
3. **Keep the repo's hardening**: deterministic-first repairs, structural
   output gates, oscillation guards, persistent budgets, infra-vs-defect
   classification. None of these may be weakened by the new work.
4. **Standard run configuration** for agent-driven runs is
   `--auto-repair --max-iterations 3` (mirrors the paper's K=3). Do NOT
   change the code defaults in `config.py` — document the invocation instead.

## 2. Environment setup and baseline verification

```bash
# from the repo root
python3 -m pip install -e .          # zero runtime deps; add '.[yaml]' if you need YAML configs
python -m unittest discover -s tests # MUST be fully green before you change anything
```

The suite must pass **offline, with no `anthropic` package and no Vitis
installed** — that is the CI contract (`.github/workflows/ci.yml` runs exactly
`python -m unittest discover -s tests` after `pip install -e .`).

Smoke the existing flow (no LLM, no Vitis):

```bash
python -m c2hlsc_agent.cli convert --config examples/vector_add/config.yaml \
  --out build/vector_add --no-run-vitis
make -C build/vector_add test         # host software equivalence
make -C build/vector_add leveri-test  # the LeVeri pair, manual today — this is what you will automate
```

Optional LLM (any one of): Claude Code CLI on PATH (default backend), or
`pip install -e '.[llm]'` + `ANTHROPIC_API_KEY`, or an OpenAI-compatible
endpoint via `--llm-backend openai --llm-base-url ... --llm-model ...`.
Optional Vitis: `--run-vitis` on a machine with `vitis_hls` on PATH, or
`--vitis-ssh <host>` to run only the Vitis phases remotely.

## 3. Guardrails (from `AGENTS.md` — binding)

- Preserve the short-circuit ladder and the status vocabulary
  `pass / fail / blocked / skipped` (+ controller `exhausted`).
- The golden `input.c` and the oracle testbench are never sent to any model
  and never rewritten by repair.
- Every loop stays bounded (attempts, LLM calls, Vitis runs, wall clock);
  budgets stay immutable across resume.
- Infrastructure failures (`toolchain_unavailable`, ssh loss, timeouts of the
  transport) must never trigger source mutation.
- The package keeps **zero runtime dependencies** — stdlib only. Do not add a
  dependency without stopping and flagging it.
- Every behavior change ships with a unit test in `tests/`; the full suite
  stays green offline on Python 3.10–3.12.

## 4. Ordered tasks

Work these in order. Commit after each task with the suite green.

### Task 1 — Golden-trace smoke step before generation

**Goal:** catch contract-extraction bugs before any LLM call or Vitis run.

- In `run_convert` (`c2hlsc_agent/cli.py`), immediately after
  `analyze_source` succeeds and before generation: render the LeVeri golden
  testbench from the analysis into the out dir, compile it with `g++`
  (same flags family as the Makefile `test` target), run it, and require the
  golden trace CSV to appear.
- On failure: finish the run `BLOCKED` with a diagnostic naming the contract
  problem. No LLM call and no Vitis reservation may have happened.
- Respect `run_command`-style timeouts (reuse `c2hlsc_agent/equivalence.py`
  `run_command`; do not hand-roll subprocess handling).
- **Accept when:** a test exists in which a broken contract (e.g. an argument
  the golden TB cannot compile against) blocks the run with an LLM stub that
  fails the test if it is ever called; and the vector_add example still
  converts end-to-end.

### Task 2 — Wire the LeVeri paired comparison into the automated ladder

**Goal:** `verify_project` runs the paired-trace check automatically instead
of leaving it as a manual Makefile target.

- Add a phase `leveri_trace` between `software_equivalence` and `csim`:
  build+run `tb/leveri_hls_tb.cpp` (golden trace already exists from Task 1;
  rebuild it if stale), then run `tb/leveri_compare.py`, and record a
  `PhaseResult` from its outcome.
- Integration points you must update together — missing one breaks the loop:
  - `c2hlsc_agent/hls_runner.py:12` `PHASE_ORDER` and
    `earliest_failing_phase` (`hls_runner.py:16-23`);
  - `verify_project` (`hls_runner.py:167-184`) — a `leveri_trace` failure
    blocks the Vitis phases exactly like a `software_equivalence` failure;
  - `classify_failure` / `classify_log_family`
    (`c2hlsc_agent/agent_loop.py:147-273`) — a comparator
    `behavior mismatch` line classifies as `behavioral_mismatch`; a
    `stimulus mismatch` or structural-tier failure classifies as a testbench
    contract problem, not a design bug;
  - `final_status` (`c2hlsc_agent/report.py:24-30`) — the new phase joins the
    required-pass set;
  - existing tests that enumerate phases (`tests/test_cli_repair_loop.py`,
    `tests/test_hlsc_repair_agent.py`, `tests/test_review_fixes.py`).
- Make the phase skippable via config (`leveri_gate`, default on) so the
  batch/dataset flows are untouched; status `skipped` when disabled.
- **Accept when:** a seeded behavioral divergence (flip an op in
  `src/hls_top.cpp`) fails at `leveri_trace` with `csim/csynth/cosim` marked
  `blocked`; a clean project passes all phases; `make leveri-test` still works.

### Task 3 — Structured divergence evidence in the repair prompt

**Goal:** on a behavioral mismatch, the repair LLM sees the first divergence
as structured data, not only a raw log tail (this is the PMLC-L1/L3 analog).

- Parse the comparator's failure line
  (`behavior mismatch cycle=<c> column=<name> expected=<e> actual=<a>`, and
  the `stimulus mismatch` form) into a structured object next to
  `parse_mismatches` (`c2hlsc_agent/equivalence.py:62-93`).
- In `build_repair_prompt` (`c2hlsc_agent/llm.py:434-464`), when the failing
  phase is `leveri_trace` (or host equivalence with parsed mismatches),
  emit a compact "Mismatch evidence" section — first divergent cycle, column,
  expected vs actual, plus the column's role — placed **before** the
  4000-char log tail. Keep the existing evidence limit; do not grow the
  prompt unboundedly.
- **Accept when:** a prompt-construction test asserts the structured section
  appears with the parsed values and the raw-tail section still follows.

### Task 4 — Document the standard K=3 run configuration

- Add the standard invocation to `README.md` (convert section):
  `--auto-repair --max-iterations 3` (+ `--use-llm` when a backend exists),
  noting the persistent budgets still bound the run.
- Do not change `config.py` defaults (tests pin them).
- **Accept when:** README shows it and the example runs.

### Task 5 — Testbench regeneration guard

- Add a check to `scripts/team_preflight.sh` (and `.ps1`): regenerate the
  LeVeri bundle from the project's contract into a temp dir and diff against
  the checked-in/on-disk `tb/` copies; fail on drift ("generated testbenches
  must not be hand-edited — change the contract and regenerate").
- **Accept when:** preflight fails on a hand-edited generated TB and passes
  on a clean project.

### Backlog — do not start without a go-ahead

- L2 backward slicing (requires an AST dependency decision — conflicts with
  the zero-dependency policy; needs an explicit human call).
- Repair-card store fed from `repair_audit.json` with human audit before
  promotion (`docs/handoff_generation_workflow.md` Stage G).

## 5. Verification before every commit

```bash
python -m unittest discover -s tests
bash -n scripts/team_preflight.sh
python -m py_compile scripts/verify_github_guardrails.py
python -m c2hlsc_agent.cli convert --config examples/vector_add/config.yaml \
  --out /tmp/smoke_vector_add --no-run-vitis
```

All four must succeed. Commit with a clear message per task; push to your
working branch (repo convention: `work/<issue>-<user>-<slug>` per
`COLLABORATOR_START_HERE.md`, or the branch you were given).

## 6. Definition of done

- Tasks 1–5 landed, each with tests; full suite green offline on a clean
  `pip install -e .`.
- `verify_project` on the vector_add example runs
  `software_equivalence → leveri_trace → (csim → csynth → cosim when Vitis
  is available)` with correct short-circuit and blocking.
- A seeded mismatch produces a repair prompt containing the structured
  divergence section.
- README documents the standard K=3 invocation.
- No new runtime dependencies; no weakening of budgets, oscillation guards,
  status vocabulary, or golden-C isolation.
