# Branch delta: `claude/agent-workflow-review-owfc84` vs `main`

*For run-to-run comparison. `main` here is `origin/main` (`0aca96e`); this branch head
is the commit adding this file. Generated from `git diff origin/main...HEAD`.*

Summary: 32 files changed, 4326 insertions(+), 48 deletions(-). Test suite: **222 on main -> 261 on this branch** (all green, still
fully offline — CI installs no model SDK).

---

## 1. If you are comparing GENERATION runs: what a run on this branch does differently

This is the section you need for the comparison. Same command, different behaviour:

### Behaviour changes that apply to EVERY run (no new flags needed)

| # | Change | main | this branch |
|---|---|---|---|
| 1 | Interface pragma ledger | model-written sources reported `_None_` | pragmas parsed from the accepted source; array-port modes that differ from the configured `interface_mode` are flagged `DIFFERS` |
| 2 | Output-comparison clamp | silent | report gains an **Output Comparison Scope** section + `output_comparison_clamped` JSON key naming every clamped array |
| 3 | Unranged length-like scalar (`n`, `count`, `len`, `size`, ...) | full-range random stimulus — **the golden reference itself can go out of bounds and segfault** | stimulus clamped to the smallest matching array length; named in the testbench contract comment |
| 4 | Missing/unlaunchable toolchain | `FileNotFoundError` traceback out of `run_convert` (Windows `.bat` case), or run closed `failed` | classified `toolchain_unavailable`, run closed **`blocked`** with the classifier's next action as the reason |
| 5 | Local Vitis binary | PATH lookup of the bare name only | `--vitis-bin` / `C2HLSC_VITIS_BIN` work for LOCAL runs; `.bat`/`.cmd` launched via `cmd /c` on Windows |
| 6 | CoSim log gate | ran before the remote artifact pull; console text only | runs after the pull and also reads Vitis's own `sim/report/*` |
| 7 | Generated project portability | Makefile/scripts hardcoded `python3` | `PYTHON ?= python3` make variable; generated scripts use `sys.executable` |
| 8 | QoR baseline freshness | ignored `hls_top.hpp` | header counts; a header-only repair invalidates a stale csynth report |
| 9 | Local PPA cell models | Liberty implicit-AND (`"!(A1 A2)"`, i.e. every Nangate NAND/AOI) emitted invalid Verilog; gate sim could not compile | translated correctly; gate-level sim runs |
| 10 | OpenSTA failures | note was empty (read stderr, OpenSTA writes stdout); `Critical` aborts read as success | real reason reported; `Critical` detected; failed report renamed so it cannot be parsed as measurements |
| 11 | Repair loop (with `--use-llm`) | repair prompt only | **failure_analyst** refines the classification first (on by default; `--no-failure-analyst`); **audit_memory** offers up to 2 audited cards from past verified runs (`--no-repair-memory`, store `~/.c2hlsc/repair_cards.jsonl`, `--memory-dir` / `C2HLSC_MEMORY_DIR`) |
| 12 | After a PASSED `--use-llm` run with repairs | nothing | applied repairs are promoted as memory cards tagged with their verification scope (`host_equivalence` vs `full_ladder`) |

### New opt-in flags (add these to a generation run to exercise the new agents)

| Flag | Agent | Adds to the run |
|---|---|---|
| `--tb-augment` | shift_left_testbench_agent | model-proposed directed stimulus vectors, contract-validated, appended AFTER the deterministic tests; `tb/augmented_vectors.json` records accepted+rejected; pass line reads `... (N llm-directed)` |
| `--propose-contract` | contract_planner | `contract_proposals.json` with validated advisory proposals + per-field rejections; NEVER applied automatically |

Both auto-enable `--use-llm`. Neither changes the first `num_tests` iterations: the
seeded stimulus stream is bit-identical to a main run, so per-test results up to
`num_tests` are directly comparable across branches.

### New per-project artifacts to COLLECT in a comparison

- `conversion_report.md` — new sections: Output Comparison Scope; populated Interface
  Pragmas; transformation-ledger notes from the planner/augmenter
- `conversion_report.json` — new key `output_comparison_clamped`
- `tb/augmented_vectors.json` (only with `--tb-augment`)
- `contract_proposals.json` (only with `--propose-contract`)
- `<memory dir>/repair_cards.jsonl` (only after a passed `--use-llm` run with repairs)
- run ledger: blocked-family runs now end `status=blocked`, not `failed` — if your
  comparison buckets outcomes by ledger status, re-bucket main's environment-failure
  runs before comparing pass/fail rates

### Comparison caveats (honest differences, not improvements)

- A design that PASSED on main can FAIL here only via `--tb-augment` finding a real
  mismatch in the appended vectors — the deterministic prefix is unchanged.
- A design that CRASHED (segfault) on main with an unranged length-like scalar now runs;
  its pass/fail on this branch has no main-side counterpart to compare against.
- With `--use-llm`, model calls per run can rise by up to 3 (analyst + augment +
  planner); the persistent `max_llm_calls` budget (default 8) still caps everything.

---

## 2. Commits on this branch (16)

```
c8c2a02 Add the live-agent dogfood evidence as a doc
7e42b5a Record the dogfood-and-hardening pass in the handoff
b135842 Fix everything the live-agent dogfood and adversarial review confirmed
85ea87a Document the live agent roster
10570f2 Bring the four declarative agents live: analyst, memory, planner, stimulus
e2bd26b Make silent behaviour visible, and let generated projects run off-Windows-PATH
0445c7c Fix W1: launch Vitis on Windows, and never crash when it will not start
51c6d94 Record the OpenSTA build and the L2/L3 fixes in the handoff
f58228b Report the real reason when OpenSTA fails, and detect Critical aborts
b391357 Add scripts/probe_bambu.py: capture Bambu's real CLI and outputs
311a4ec Record verified lane C and local PPA results, L1 fix, and W5 in the handoff
4fd9817 Fix Liberty implicit-AND translation in generated cell models
0fb731c Add readiness probe and session handoff notes
bba56bd Add docs/workflow_evaluation.md: evidence-first quality review
7c01258 Correct the generated-file count in docs/full_workflow.md
b4ebc88 Add docs/full_workflow.md: full component and agent workflow map
```

## 3. Files added (11)

```
c2hlsc_agent/audit_memory.py
c2hlsc_agent/contract_planner.py
c2hlsc_agent/stimulus_augment.py
docs/SESSION_HANDOFF.md
docs/agent_dogfood_evidence.md
docs/full_workflow.md
docs/workflow_evaluation.md
scripts/check_readiness.py
scripts/probe_bambu.py
tests/__init__.py
tests/test_live_agents.py
```

## 4. Files modified (21)

```
AGENT_SUMMARY.md
README.md
c2hlsc_agent/agent_loop.py
c2hlsc_agent/cli.py
c2hlsc_agent/config.py
c2hlsc_agent/convert.py
c2hlsc_agent/hls_project.py
c2hlsc_agent/hls_runner.py
c2hlsc_agent/hlsc_repair_agent.py
c2hlsc_agent/leveri_testgen.py
c2hlsc_agent/llm.py
c2hlsc_agent/local_ppa.py
c2hlsc_agent/qor_optimizer.py
c2hlsc_agent/report.py
c2hlsc_agent/run_control.py
c2hlsc_agent/testgen.py
c2hlsc_agent/verilog_testgen.py
tests/test_llm_agents.py
tests/test_qor.py
tests/test_review_fixes.py
tests/test_strong_agents.py
```

## 5. Where the detail lives

- `docs/full_workflow.md` — the workflow map (all eight agents now live, section 13)
- `docs/workflow_evaluation.md` — the F1-F10 quality findings this work responds to
- `docs/agent_dogfood_evidence.md` — verbatim outputs of the real-model dogfood runs
- `docs/SESSION_HANDOFF.md` — running log of every increment (temporary, delete at merge)
- `examples/agent_dogfood/` + `scripts/dogfood_live_agents.py` — reproduce the dogfood
