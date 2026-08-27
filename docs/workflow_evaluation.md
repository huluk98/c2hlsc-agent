# Workflow quality evaluation

*Evidence-first review of the loops and generation paths at commit `0aca96e`.
Every claim below was reproduced by running the code, not inferred from reading it.*

Companion to [`docs/full_workflow.md`](full_workflow.md), which describes what the
workflow **is**. This page assesses how well it holds up, and what to check before a run
whose numbers you intend to report.

**Summary:** 2 high, 4 medium, 3 low findings, plus 1 case where the pipeline is stronger
than its own documentation claims.

---

## Verdict

The architecture is sound and unusually disciplined for this class of system. The
separation between *proposal* and *acceptance* is real and structurally enforced — a model
may only ever rewrite `src/hls_top.cpp`, the oracle is compiled rather than shown, the
ladder short-circuits without back-filling, and the bounded-run controller genuinely
prevents runaway retries. All four were verified by experiment.

Two findings are worth acting on first, and **the load-bearing risk is not the model — it
is the oracle**:

- **F1** — the host testbench narrows its output comparison based on an *argument's name*.
  A design whose outputs are all contractually defined, sabotaged beyond index `n`, passed
  64/64 tests. That one testbench is shared by host equivalence, Vitis CSim **and** Vitis
  CoSim, so a single heuristic can silently defeat three of the four rungs at once.
- **F2** — on a real end-to-end LLM run, the model replaced the configured `ap_memory`
  interface with `m_axi`. The conversion report recorded the interface pragma ledger as
  `_None_`, and the standalone RTL testbench was still built for BRAM ports, with no
  warning. Nothing in rungs 1–2 can see an interface change.

Everything else is a boundedness gap in `optimize` (F3, F4), a labelling inaccuracy (F5),
or documentation debt (F6–F9).

---

## Method

Structural review of all 23 package modules and 9 scripts, then nine executed experiments
designed to falsify specific claims the code makes about itself. Two findings (F1, F2)
were discovered by experiment and would not have been visible from reading alone.

| # | Experiment | Result |
|---|---|---|
| E1 | re-run an identical `convert` on a passed project | refused: "run … is already passed" — correct |
| E2 | deterministic convert on a fresh design | pass; the conservative copy can essentially never fail rung 1 |
| E3 | `--run-vitis` with no `vitis_hls`, `--auto-repair`, 3 iterations | correct `toolchain_unavailable`/blocked classification, **no source mutation**, 1 of 3 attempts used — but run closed as `failed` (F5) |
| E4 | resume that failed run | same (source, failure) → repeat count 2 → `exhausted`. Cross-invocation guard works |
| E5 | third invocation | refused: "already exhausted" — correct |
| E6 | threshold design where `n` is not a length | testbench emitted `compare_len_out = clamp_count(n, 16)` |
| E7 | sabotage every output element beyond `n`, re-run rung 1 and lane A | **rung 1 passed 64/64; LeVeri caught it at cycle 0** (F1) |
| E8 | hand-edit `src/hls_top.cpp`, re-run `convert` | silently overwritten (F6) |
| E9 | full LLM generation through the `claude-cli` backend | works: 51 s, 1 model call, model output used, rung 1 passed — and interface mode silently changed (F2) |

---

## Readiness matrix

| Component | Verified | Needs |
|---|---|---|
| Deterministic `convert`, rung 1 | **yes**, end to end | g++ |
| LLM generation via `claude-cli` | **yes**, real call, 51 s | `claude` on PATH |
| Run ledger, budgets, both oscillation guards | **yes** (E1/E3/E4/E5) | — |
| Failure classification & blocked handling | **yes** (E3) | — |
| Lane A — LeVeri paired traces | **yes**, and it caught F1 | g++, python3 |
| Lane B — gcov | **yes**, report written | gcov |
| Lane B — KLEE | skipped cleanly | klee, clang++ |
| Lane C — standalone RTL | skipped cleanly ("run synthesis first") | vitis_hls, then iverilog or xsim |
| Rungs 2–4 (CSim / CSynth / CoSim) | **not exercisable** | vitis_hls |
| `optimize` / QoR loop | **not exercisable** — but 36 unit tests cover it, incl. rollback, stale reports, target rounds | vitis_hls |
| Local PPA (yosys → sim → OpenSTA) | **not exercisable** | yosys, liberty, OpenSTA, iverilog |
| Remote Vitis over SSH | **not exercisable** | an SSH host with Vitis |

> Every component that touches **hardware** evidence — rungs 2–4, QoR, local PPA, lane C —
> is untested in the environment this evaluation ran in. The unit tests around them are
> good, but a first real run on the Vitis machine should be a deliberate smoke test, not a
> batch job.

---

## Findings

### F1 · HIGH · soundness — name-inferred output clamping can hide real mismatches in three of the four rungs

The generated host testbench narrows its output comparison to an "active length" when a
scalar argument's *name* looks length-like (`n`, `len`, `size`, `count`, `*_len`, `num_*`, …)
**and** the config declares a `range` for it inside the array's bound. Both conditions are
met by exactly the configuration the README recommends writing.

A design where `n` is a **threshold**, not a length — every one of the 16 outputs
contractually defined — sabotaged to write `12345` at every index ≥ `n`:

```c
void thresh(const int *in, int *out, int n) {
    for (int i = 0; i < 16; i++) out[i] = (in[i] > n) ? 1 : 0;
}
```
```yaml
arguments: { in: {length: 16}, out: {direction: output, length: 16}, n: {range: [0, 16]} }
```
```cpp
// what the generated testbench emitted
const int compare_len_out = clamp_count(static_cast<long long>(n), 16);
for (int i = 0; i < compare_len_out; ++i) { ... }
```
```console
$ make test
c2hlsc_agent: all 64 tests passed, seed=1          # rung 1 sees nothing

$ make leveri-test
HLS-LeVeri consistency check failed: behavior mismatch
  cycle=0 column=out[0] expected=0 actual=12345    # lane A catches it instantly
```

**Blast radius.** `tb/testbench.cpp` is not just the host harness — `run_csim.tcl` and
`run_cosim.tcl` both do `add_files -tb tb/testbench.cpp`, so the same clamp is in force
during Vitis CSim and Vitis C/RTL CoSim. Lane C inherits it through `cmp_scalar` in
`verilog_testgen.build_spec`. **Lane A is the only comparison in the whole system that
looks at the full array.**

- **Mechanism:** `testgen._looks_like_length_name` + `_active_length_arg`.
- **Not an oversight:** it is deliberate and test-locked (`tests/test_convert.py:85`
  asserts the clamp is emitted). The motivation is real — an equivalence-preserving HLS
  transform may over-write a contractually-undefined tail, e.g. unrolling to a fixed trip
  count.
- **But:** output buffers are already sentinel-filled on both sides, so a design that
  legitimately leaves the tail alone passes a full comparison anyway. The clamp only
  changes the verdict when the two implementations write *different* values in the tail —
  precisely the case you want to hear about.
- **Fix:** make it explicit — `arguments.<array>.active_length: <scalar>`, defaulting to
  full-array comparison — and print the active clamp in `conversion_report.md`. Contract
  change; requires updating the locking test.
- **Until then:** run `make leveri-test` as part of acceptance on any design with a ranged
  length-like scalar, and treat a rung-1 pass on such a design as provisional.

### F2 · HIGH · provenance — model-chosen interface pragmas are unconstrained, unrecorded, and desynchronize the RTL lane

Surfaced on the first real LLM run, with no adversarial setup. The config asked for
`ap_memory`; the model returned `m_axi`.

```console
$ python -m c2hlsc_agent.cli convert --config examples/vector_add/config.yaml \
    --out build/e9 --use-llm --llm-backend claude-cli --llm-model haiku
LLM generator/repair enabled (model=haiku)
c2hlsc_agent: all 64 tests passed, seed=7        # rung 1 is blind to interfaces
```
```
config                    interface_mode: ap_memory
generated src/            #pragma HLS INTERFACE mode=m_axi port=a offset=slave bundle=gmem  (x3)
                          #pragma HLS PIPELINE II=1
conversion_report.md      ## Interface Pragmas → _None_        # the ledger is empty
tb/rtl_tb_manifest.json   interface_mode: "ap_memory"
                          array ports:    ["address0","ce0","q0"]   # BRAM model, AXI design
                          notes:          []                        # AXI warning never fires
```

Three things go wrong and compound: the generator prompt hard-constrains the signature but
says nothing about the interface; `convert._llm_candidate` returns `interface_pragmas=[]`
so the report's ledger is empty *while the source is full of pragmas*; and
`verilog_testgen.build_spec` reads `config.interface_mode` rather than the emitted source,
so its own AXI-unmodelled warning cannot fire.

Neither rung 1 nor rung 2 can detect this — a C testbench cannot observe a port protocol.
The design is not *wrong*; `m_axi` may be the better choice. But a different hardware
contract was produced than was requested, and no artifact in the run says so.

- **Fix:** parse `#pragma HLS INTERFACE` out of the accepted source into
  `GeneratedSource.interface_pragmas` (the report then tells the truth for free); state the
  configured mode in the generator prompt as a constraint; derive `build_spec`'s
  `interface_mode` from the emitted source, or warn on mismatch.
- **Cheap partial:** the first half alone converts a silent drift into a visible one.

### F3 · MEDIUM-HIGH · boundedness — `optimize` sits entirely outside the run controller

```
command          RunController   BudgetedLLMClient   ledger events
run_convert      yes             yes                 yes
run_repair       NO              NO                  no
run_optimize     NO              NO                  no
```

Its only bounds are `--iterations` (default 4) and `--max-rounds` (default 5): round 0
contributes one deterministic candidate plus four model candidates, then four per round
after — **up to 21 scored candidates and ~20 model calls**. No wall-clock budget, no
durable checkpoint, no resume.

The per-tool timeouts are the only ceiling. Per candidate: host equivalence 120 s + CSim
600 s + CSynth 1200 s, and with local PPA on, yosys 1800 s + iverilog 300 s + vvp 600 s +
OpenSTA 900 s — a worst case near 92 minutes *per candidate*, so the loop's theoretical
ceiling is well over 30 hours with no single control that caps it.

This is a direct gap against the stated invariant *"keep every autonomous loop bounded by
explicit attempts, timeouts, worker caps, durable checkpoints, and repeated-state
detection"*. Repeated-state detection is present (the `seen` source-hash set); the other
four are not.

- **Fix:** thread `_start_run_controller` through `run_optimize` (attempts = rounds, plus
  the existing LLM and Vitis reservations) and wrap the client in `BudgetedLLMClient`. The
  identity fingerprint would need the objective and targets added.
- **Interim:** drive it with `--iterations 2 --max-rounds 2` under an external timeout.
- **Model to copy:** `scripts/cosim_repair_loop.py` is better bounded than the in-package
  loop — immutable retry budgets that must match on resume, failure fingerprints,
  retryable-vs-non-retryable error classes, and a dead-letter terminal state.

### F4 · MEDIUM · claim integrity — `optimize` never checks that the project passes first

The module docstring says "Operates on a project that already passes the verification
ladder" and the CLI help says "of a verified project". Nothing enforces either.
`verify_project` appears in `qor_optimizer.py` only at the winner-acceptance step.

Damage is bounded because the winner still has to pass the full ladder, so an unverified
design cannot be *accepted* on the default path. The realistic failure is a misleading
report: baseline QoR measured on a design that does not pass, with deltas quoted against
it. The sharper edge is `--no-cosim-winner`, which accepts after host equivalence only —
combined with an unverified baseline, a route to an accepted QoR change on a design never
CoSim-verified.

- **Fix:** at entry, run the ladder once, or require `conversion_report.json` to record
  `status: pass` with a project signature matching the current sources.

### F5 · MEDIUM · status fidelity — a `blocked` classification closes the run as `failed`

E3 ran `--run-vitis` with no `vitis_hls`. The classifier got it exactly right and the
repair agent correctly refused to touch anything. The run status did not follow.

```json
// conversion_report.json → agent_decision
{ "family": "toolchain_unavailable", "owner_agent": "cosim_operator", "status": "blocked",
  "next_action": "Install or activate Vitis HLS on PATH, then rerun the verifier from CSim." }

// same file → run_control
{ "status": "failed", "reason": "no safe repair changed the failing project" }
```

The two artifacts disagree about the kind of problem. `AGENTS.md` requires the status
values to be used exactly and says `blocked` runs are handed to a human — the right
disposition for a missing tool. As it stands, anything reading the ledger (including
`status`) cannot distinguish "your toolchain is missing" from "your design is wrong", and
the stated reason actively points at the design.

- **Fix:** in `run_convert`, when `classify_failure(...).status == "blocked"`, close with
  `RunStatus.BLOCKED` and carry the classifier's `next_action` as the reason. Both statuses
  are reopenable, so nothing else changes. The same path would correctly label a dropped
  SSH connection, which `remote.py` already relabels into this family.

### F6 · MEDIUM · resume semantics — `convert` resumes the budget but regenerates the source

```console
$ md5sum src/hls_top.cpp                       # 66ccb2c7…
$ echo '// HUMAN EDIT' >> src/hls_top.cpp      # 14e035c9…
$ python -m c2hlsc_agent.cli convert --config … --out <same dir> --new-run
$ md5sum src/hls_top.cpp                       # 66ccb2c7…  the edit is gone
```

`write_project` overwrites `src/hls_top.cpp` unconditionally, and `clear_repair_audit`
wipes repair history on every invocation. So resuming a run continues the *budget* while
discarding the *state* the budget was being spent on: previous repairs are lost, and
`_llm_repair`'s cross-invocation oscillation guard is defeated because the audit it reads
is empty.

The ledger's own `seen_states` guard still holds — E4 confirmed a resumed deterministic run
correctly detects the repeated (source, failure) pair and closes as `exhausted`. The safety
property survives; what is lost is progress and repair memory.

`convert` is a "regenerate and verify" command, not a "continue where I left off" command.
That is defensible — it just is not what the word *resume* in the ledger implies.

- **Fix:** cheapest — say it in `--help` and the report. Better — skip
  `clear_repair_audit` when the controller resumed an existing run id. Best — a
  `--resume-source` that keeps an existing `src/` and re-enters at verification.

### F7 · LOW · ergonomics — `--max-iterations` silently doubles as an immutable persistent budget

Its help reads "max verification iterations (default 1)". It is also
`RunBudget.max_attempts` — persisted, counted across invocations, immutable for the life of
a run id.

```
E1  re-run an identical passed convert   → refused: "run … is already passed"
E4  re-run a failed convert              → consumes attempt 2/3, hits the repeat guard, exhausted
E5  run it once more                     → refused: "run … is already exhausted"
--  change --max-iterations mid-run      → RunClosed: "budgets … are immutable"
```

With the default of 1, a single failed invocation leaves one attempt spent out of one, so
"let me just try that again" is not a retry — it is the last attempt.

- **Fix:** one sentence in the help text.

### F8 · LOW · correctness — the QoR freshness check ignores `hls_top.hpp`

`_report_is_fresh` compares the `csynth.xml` mtime against the newest of
`src/hls_top.cpp` and `tb/testbench.cpp`. The header is not in that set, so a header-only
change — which `_repair_missing_standard_includes` makes routinely, since it writes to
`hls_top.hpp` — leaves a stale synthesis report looking fresh, and `optimize` would
baseline against the previous design. **One-line fix.**

### F9 · LOW · defence in depth — the CoSim log gate only ever sees console output

`_gate_cosim_on_log` runs inside `run_vitis`'s `try` block, *before* the `finally` that
pulls remote artifacts back, so it scans stdout, stderr and the runner-written
`<phase>.log` and never reads `c2hlsc_project/solution1/sim/report/`. Local and remote
behave identically, so there is no asymmetry — but the whole gate rests on the
co-simulation verdict appearing on the console.

This is the single most important defensive line in the runner, so it is worth more than
one source of truth.

- **Fix:** after the artifact pull, re-run the gate against
  `c2hlsc_project/solution1/sim/report/*`. Purely additive — it can only turn a pass into
  a fail.

### F10 · POSITIVE — your CoSim is a stronger check than your documentation claims

`README.md` and `AGENTS.md` both carry the caveat that Vitis C/RTL CoSim "checks generated
RTL against the HLS-C design… it does not, by itself, prove that RTL is equivalent to the
original C". That is correct as a general statement about CoSim. It understates *this*
harness:

```tcl
# render_run_csim — the project run_cosim.tcl later reopens
add_files src/hls_top.cpp
add_files -tb tb/testbench.cpp     # the testbench that #includes ../input.c
                                   # with the top macro-renamed to *_ref
```

The CoSim testbench **is** the golden-oracle testbench. When Vitis drives the RTL through
it, expected values are computed by the macro-renamed original C inside that same binary.
So a CoSim pass here is direct, bounded evidence that **the RTL matches the original C**
over the generated stimulus — not merely that it matches the HLS-C.

Worth saying precisely, because it is the strongest claim the pipeline supports and it is
not currently being made. Correct phrasing: *a CoSim pass is bounded evidence of
RTL ≡ original C over the generated stimulus set* — subject to F1, which is exactly why F1
matters so much.

---

## Loop correctness

Judged against the stated requirement — bounded by explicit attempts, timeouts, worker
caps, durable checkpoints, and repeated-state detection:

| Loop | Attempts | Wall | Timeouts | Checkpoint | Repeat-state |
|---|---|---|---|---|---|
| `convert` verify/repair | **yes**, persistent | **yes**, 14400 s | **yes**, per phase | **yes**, ledger | **yes**, ×2 guards |
| `optimize` rounds | rounds only | **none** | yes, per tool | **none** | yes, hash set |
| `repair` (single-shot) | n/a | **none** | yes | audit only | yes, audit hashes |
| `cosim_repair_loop` (dataset) | **yes**, immutable | per phase | yes | **yes**, resumable | yes, fingerprints |

The dataset script is better bounded than the in-package `optimize` loop. Whatever is done
to bound `optimize` can be modelled on it.

**Verified guard behaviour:** E5 confirmed the budget guard refuses a third run on an
exhausted id; E4 confirmed the persistent failure fingerprint detects a repeat across
invocations; E3 confirmed a blocked family produces **zero** source mutation — the repair
agent correctly refuses to patch over a missing toolchain.

---

## Stimulus adequacy — what a pass actually licenses

**1. On the deterministic path, a rung-1 pass is close to tautological.** The conservative
generator copies the original top-function body verbatim into the wrapper. Host equivalence
then compares that body against the same body compiled as `*_ref`. E2 confirmed the
consequence: the deterministic path essentially cannot fail rung 1. Such a pass tells you
the project builds and the C-to-C++ wrapper is sound — not that anything was verified.
**The deterministic path only becomes informative at rungs 2–4.** A deterministic run with
`--no-run-vitis` reporting `pass` should not be cited as equivalence evidence.

This inverts for the LLM path, where rung 1 is doing real work — the model's translation is
genuinely different code, and E9's 64/64 is a meaningful result.

**2. The stimulus is bounded, and nothing measures input-space coverage.** Default 100
tests (examples use 64), from `mt19937_64(seed)` plus four directed patterns, with
sentinel-filled outputs and directed boundary values for ranged scalars. Reproducible from
the seed, which is right. But for a 16-element `int32` array that is a vanishingly small
sample, and the claim is only ever "over these vectors".

gcov measures *line* coverage of the golden C — a useful proxy, not the same question. And
KLEE drives only the **golden top**: it is a symbolic coverage and robustness driver, not a
differential one, so it cannot produce an equivalence counterexample between golden and
HLS-C. To strengthen evidence, raise `num_tests` and add `directed_tests` for domain edges
rather than reaching for KLEE.

**3. The transitive argument is sound.** Rungs 1, 2 and 4 all drive the same
oracle-carrying testbench, so a full ladder pass gives golden C ≡ HLS-C (rungs 1–2) and
golden C ≡ RTL (rung 4, per F10) over one shared seeded stimulus set. A clean, defensible
bounded claim whose single weak joint is F1.

---

## Pre-run checklist

- [ ] **Check every ranged scalar in your config against F1.** If a scalar named `n`,
      `len`, `size`, `count` (or `*_len`, `num_*`) has a `range` and is *not* the active
      length of the output array, your comparison is being narrowed. Grep the generated
      testbench for `compare_len_` to see which arrays are affected.
- [ ] **Add `make leveri-test` to your acceptance step.** The only full-array comparison in
      the system, and it costs seconds.
- [ ] **After any LLM-generated conversion, diff the interface pragmas against your
      config:** `grep 'HLS INTERFACE' src/hls_top.cpp`. The report's ledger will say
      `_None_` until F2 is fixed.
- [ ] **Smoke-test the Vitis rungs before batching** — one `convert --run-vitis` on
      `examples/vector_add` on the real machine first.
- [ ] **Run `optimize` under an external timeout** with `--iterations 2 --max-rounds 2`
      until F3 is closed.
- [ ] **Confirm the project passes before you optimize it** — `optimize` will not check
      (F4).
- [ ] **Decide `--max-iterations` up front.** It is immutable for the run id, and the
      default of 1 means one failed invocation has already spent the budget (F7).
- [ ] **Do not split one conversion across sessions** expecting repairs to carry over (F6).
- [ ] **Record the seed and `num_tests` with any equivalence claim.**

---

## Suggested fix order

Ordered by risk removed per line of change, not by severity alone.

| # | Change | Size | Removes |
|---|---|---|---|
| 1 | Populate `GeneratedSource.interface_pragmas` by parsing the accepted source | ~15 lines | half of F2 — silent drift becomes visible drift |
| 2 | Close as `RunStatus.BLOCKED` when the classifier says blocked | ~5 lines | F5 entirely |
| 3 | Add `hls_top.hpp` to `_report_is_fresh` | 1 line | F8 entirely |
| 4 | Print the active clamp in `conversion_report.md` | ~10 lines | makes F1 *visible* without a contract change |
| 5 | Precondition check at the top of `run_optimize` | ~15 lines | F4 entirely |
| 6 | Help-text corrections for `--max-iterations` and resume behaviour | prose | F7, half of F6 |
| 7 | Re-run the CoSim gate after the artifact pull | ~10 lines | F9 — purely additive |
| 8 | Skip `clear_repair_audit` on a resumed run id | ~5 lines | rest of F6 |
| 9 | Thread the run controller through `run_optimize` | ~60 lines | F3 entirely |
| 10 | `arguments.<array>.active_length`, default full comparison | contract change + test update | F1 entirely |
| 11 | Constrain interface mode in the generator prompt; derive `build_spec` from source | ~30 lines | rest of F2 |
| 12 | Tighten the CoSim caveat wording in README and AGENTS.md | prose | F10 — lets you make the stronger claim you have earned |

Items 1–7 are a single afternoon and remove every Medium finding plus the visibility half
of both High ones. Items 10 and 11 are the real fixes for F1 and F2 and both change a
contract, so they want their own issues.
