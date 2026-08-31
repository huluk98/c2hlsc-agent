# C2HLSC agent — merged Codex + Claude technical report

**Snapshot:** 2026-08-31 19:35 (Asia/Shanghai, UTC+08:00)  
**Status:** Codex-led final integration; Claude’s bounded RTLLM retry stopped at the quota/transport ceiling  
**QoR authority:** AMD Vitis HLS 2024.2 (`csynth.xml`), not OpenSTA

## Technical summary

Codex and Claude worked in parallel without sharing a writer. Claude owned only the RTLLM generation checkpoints in `runs\paper_20260831`; Codex owned code integration, independent validation, Vitis QoR, result consolidation, and this report. Their work was isolated by Git worktree and branch, then merged by Codex after the generation boundary. An extra Claude `n=10` job was cancelled before it started so the scarce Claude quota remained bounded and Codex stayed the primary agent.

The merged implementation now uses Vitis HLS as the timing and resource authority. On the validated `dfmul` artifact, Vitis reports a 7.067 ns estimated clock, 26-cycle worst-case latency, 25-cycle maximum initiation interval, and all configured targets met. The Codex integration also fixes fallback HLS headers that previously omitted application-defined signature types. Fresh validation reached 100-stimulus host equivalence for the `dfadd`, `dfdiv`, and `dfmul` inner kernels, although those results remain validation-only because the inner-kernel mutation wrapper is still nullary and cannot yet prove a non-vacuous pass.

CHStone headline results must be split by claim. The complete agent system, which includes deterministic fallback, passes 8/12 and 9/12 in the two LLM-seeded arms. The clean LLM generator contribution is 7/12 in each arm after removing byte-identical deterministic fallbacks. Rosetta has no quotable score because its runner lacks mutation testing and one array-bound case is unsound.

Three RTLLM arms completed without backend contamination: baseline passes 47/50 designs, no-plan 46/50, and no-repair 34/50. After excluding the four vacuous oracles, baseline pass@1/pass@2 is 0.870/0.935; no-plan is 0.848/0.913; and no-repair is 0.641/0.674. This supports the repair loop as the larger measured contributor. The three evidence-policy arms are frozen and unscored because Claude CLI saturation left them incomplete or contaminated.

## Key findings

| Area | Result | Interpretation |
| --- | --- | --- |
| Agent coordination | Codex lead; Claude bounded to one checkpoint writer | Parallel work did not overwrite or double-count outputs. |
| QoR authority | Vitis HLS 2024.2 | OpenSTA is retained only as an optional legacy field, not a decision source. |
| RTLLM baseline | 47/50 official and strict design pass; adjusted pass@1 0.870 | Complete, 100 samples, zero backend errors. |
| RTLLM no-plan | 46/50; adjusted pass@1 0.848 | Planning adds 0.022 absolute pass@1 in this run. |
| RTLLM no-repair | 34/50; adjusted pass@1 0.641 | Removing repair costs 0.229 absolute pass@1 versus baseline. |
| Vitis `dfmul` timing | 7.067 ns against 10.0 ns maximum | Target met with 2.933 ns (29.3%) headroom. |
| Vitis `dfmul` latency | 26 cycles against 30 maximum | Target met with 4 cycles of headroom. |
| CHStone deterministic | 6/12 | Host-equivalence result from the paper run root. |
| CHStone agent system | 8/12 and 9/12 | Includes deterministic fallback; this is the deployable-system claim. |
| CHStone clean LLM generator | 7/12 and 7/12 | Excludes byte-identical deterministic fallbacks. |
| Inner-kernel fallback-type fix | `dfadd`, `dfdiv`, `dfmul` reach 100-stimulus host equivalence | Validation-only until the mutation wrapper supports typed arguments and returns. |
| Rosetta | No quotable aggregate | Mutation testing and sound array bounds are required first. |

## RTLLM results

`pass@k` uses the unbiased estimator `1 - C(n-c, k) / C(n, k)`. It is defined only for `k <= n`; the main arms have two samples per design, so pass@5 and pass@10 are not estimable. “Adjusted” excludes the four vacuous-oracle designs identified by mutation testing: `comparator_3bit`, `comparator_4bit`, `sequence_detector`, and `square_wave`. A backend-error cell is unknown, not a model failure; an arm becomes final only when its backend-error count is zero.

| Arm | Single changed factor | Clean designs / target | Backend-error cells | Official design pass | Adjusted pass@1 | Adjusted pass@2 | Reporting status |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline | — | 50/50 | 0 | 47/50 | 0.870 | 0.935 | Final |
| No plan | `plan=false` | 50/50 | 0 | 46/50 | 0.848 | 0.913 | Final |
| No repair | `max_repair_rounds=0` | 50/50 | 0 | 34/50 | 0.641 | 0.674 | Final |
| Self evidence | `evidence_policy=self` | 34/50 | 0 | — | — | — | Frozen incomplete: 16 designs not run; surviving subset is selection-biased |
| No evidence | `evidence_policy=none` | 13/50 | 0 | — | — | — | Frozen incomplete: 37 designs not run; surviving subset is selection-biased |
| Oracle evidence | `evidence_policy=oracle` | 11/50 | 76 | — | — | — | Pre-outage surviving subset; selection-biased |

The official and strict functional verdicts agree for every sample in the three final arms. Their syntax-pass counts are 99/100 (baseline), 98/100 (no-plan), and 85/100 (no-repair). Generation parity validation passes across all six configured arms: model, backend, benchmark, shims, compile/simulation timeouts, and retry settings match, with exactly one intended factor changed per ablation.

| Control | Designs | n | Official design pass | Adjusted pass@1 | Interpretation |
| --- | ---: | ---: | ---: | ---: | --- |
| Shipped reference RTL | 50 | 1 | 47/50 | 0.935 | Reproduces the benchmark reference; includes the four vacuous tests in the official count. |
| Empty-module baseline | 50 | 1 | 4/50 | 0.000 | Identifies the four vacuous oracles excluded from adjusted estimates. |

The shipped external GPT sets cover 29 designs with five samples each, so they are useful context but are not a like-for-like 50-design comparison with the Claude arms. Their measured pass@1 values are 0.414 for GPT-4 and 0.255 for GPT-3.5; pass@5 is 0.621 and 0.379 respectively.

## HLS-C and CHStone results

| Arm | Agent-system pass | Clean LLM-generator pass | Denominator | Trust note |
| --- | ---: | ---: | ---: | --- |
| Deterministic | 6 | n/a | 12 | Mutation-red host-equivalence, but weak stimulus coverage. |
| LLM seed 1 | 8 | 7 | 12 | `dfadd` is a deterministic fallback in the system count. |
| LLM seed 2 | 9 | 7 | 12 | `dfadd` and `dfmul` are deterministic fallbacks in the system count. |

The `chstone_main` testbench repeats the same zero-argument call 100 times. Mutation-red proves the comparison is not vacuous, but “100 stimuli” is repetition rather than input coverage. No CHStone or Rosetta paper arm in this run root used Vitis; their reported verdicts are host-equivalence results.

### Application-type closure fix

Codex commit `ec7a75d` makes the non-libclang fallback discover local headers that declare custom types in the generated function signature, stages those headers into the HLS project, includes the generated header before the linked-golden declaration, and renames an original-source `main` while linking an inner top. The focused converter tests pass. Fresh isolated runs show:

| Inner kernel | Host-equivalence | Mutation check | Reportable in paper denominator? |
| --- | --- | --- | --- |
| `dfadd` | Pass, 100 stimuli | Inconclusive | No |
| `dfdiv` | Pass, 100 stimuli | Inconclusive | No |
| `dfmul` | Pass, 100 stimuli | Inconclusive | No |
| `dfsin` | Type/link defect cleared; execution did not terminate on unrestricted random bit patterns | Not reached | No |
| `gsm` | Staging not applied | Not reached | No |

## Vitis QoR result

Source artifact: `C:\Users\luke\runs_win\chstone_final\benchmarks\dfmul\project\qor_report.json`.

| Metric | Vitis result | Target / device capacity | Status |
| --- | ---: | ---: | --- |
| Estimated clock | 7.067 ns | ≤ 10.0 ns | Met |
| Latency, best / worst | 26 / 26 cycles | worst ≤ 30 | Met |
| Initiation interval, min / max | 25 / 25 cycles | — | Reported |
| BRAM | 7 | 624 (1.12%) | Within device |
| DSP | 16 | 1,728 (0.93%) | Within device |
| FF | 2,809 | 460,800 (0.61%) | Within device |
| LUT | 4,840 | 230,400 (2.10%) | Within device |
| URAM | 0 | 96 (0.00%) | Within device |

The optimizer correctly accepted the baseline without proposing a rewrite because every configured PPA target was already met. The report contains no OpenSTA slack, area, or power values; those nullable legacy fields do not affect this decision.

## Collaboration and merge provenance

| Owner | Worktree | Branch | Responsibility / accepted output |
| --- | --- | --- | --- |
| Claude | `C:\Users\luke\c2hlsc-rtllm` | `fix/self-contained-translation-unit` | Bounded RTLLM generation, parity contract, pass/fail and pass@k tooling. |
| Codex | `C:\Users\luke\c2hlsc-vitis-qor` | `codex/vitis-qor-authority` | Vitis-only QoR authority, commit `104056c`. |
| Codex | `C:\Users\luke\c2hlsc-codex-main` | `codex/main-integration` | Vitis merge `68ca828`, application-type closure fix `ec7a75d`. |
| Codex | `C:\Users\luke\c2hlsc-final` | `codex/final-integration` | Final merge `ed94edc`, Windows path normalization `ec09c3b`, independent checks, canonical report. |

The agents did not edit the same checkpoint directory concurrently. Claude’s six-arm job processed pairs sequentially with three workers per arm, keeping the Claude CLI concurrency at six after a higher-concurrency attempt saturated the backend. Codex did not start or resume any RTLLM writer. When saturation returned during the self-evidence arm, Codex stopped the retry before the final pair began. A later detached Claude launcher briefly resumed `ev_self` and `ev_none`; Codex stopped that launcher, regenerated every consolidated output from the final frozen files, and made the launcher opt-in. Both queued `rtllm_baseline_n10` processes were stopped before generation because they were outside the bounded handoff and would have consumed roughly 500 additional Claude calls.

## Methods and evidence

| Evidence | Location | Use |
| --- | --- | --- |
| RTLLM per-design rows | `C:\Users\luke\c2hlsc-rtllm\runs\paper_20260831\rtllm_*\results.jsonl` | Sample counts, syntax, functional verdicts, backend errors. |
| Consolidated record | `C:\Users\luke\c2hlsc-rtllm\runs\paper_20260831\consolidated.json` | Cross-suite pass/fail/unknown cells and artifact paths. |
| Pass@k report | `C:\Users\luke\c2hlsc-rtllm\runs\paper_20260831\passk.md` | Unbiased pass@k estimates and defined-k guard. |
| Lane register | `C:\Users\luke\c2hlsc-rtllm\docs\paper_run_lanes.md` | Work ownership, collision rules, known oracle limitations. |
| Vitis QoR | `C:\Users\luke\runs_win\chstone_final\benchmarks\dfmul\project\qor_report.json` | Timing, latency, interval, resources, target decision. |
| Codex isolated validation | `C:\Users\luke\runs_win\codex_inner_fix_*_20260831` | Regression evidence for the application-type closure fix; excluded from paper denominators. |

The final RTLLM table was regenerated from source JSONL after every writer stopped, then independently reconciled against row counts, unique design counts, sample totals, functional counts, strict functional counts, syntax counts, and non-null `llm_error` cells. Only the three 50-design, 100-sample, zero-error arms are scored. Small exact tables are used instead of charts because denominator and provenance lookup are the important comparisons and a plot would hide trust-state differences.

## Limitations

- Four RTLLM tests have vacuous oracles and are excluded from adjusted estimates.
- CHStone’s top-level test repeats one zero-argument call, so coverage is weak even when mutation-red.
- The inner-kernel mutation wrapper does not yet support typed arguments and returns; new inner-kernel passes are not headline results.
- Rosetta has no mutation check; `face-detection` also lacks a sound pointer bound.
- `gsm` inner-kernel staging remains unresolved.
- The focused Windows converter/repair/QoR suite is green (56/56) after commit `ec09c3b` normalised generated audit, Yosys, and STA paths. The broader suite still has separately documented pre-existing Windows-specific failures outside this focused validation.
- Evidence-policy effects cannot be estimated from this snapshot because all three evidence arms are incomplete or contaminated by backend saturation.
- This is a dated snapshot. A later sweep or code commit requires a new report version rather than silently replacing these numbers.

## Recommended next steps

1. Generalize the CHStone mutation wrapper from the generated function signature, then revalidate the three newly reachable inner kernels.
2. Add Rosetta mutation testing and explicit pointer bounds before publishing any Rosetta aggregate.
3. Fix `gsm` inner-kernel staging and the Windows path-normalization expectations.
4. Run Vitis CSim/CSynth/CoSim across the now-sound inner-kernel subset; keep Vitis as the sole QoR authority.
5. Spend additional Claude quota on deeper sampling only if pass@5 or pass@10 is a required decision metric. Codex should continue to own integration, validation, and reporting.

## Further questions

- Does the project need pass@5/pass@10, or are the complete `n=2` ablations sufficient for the next decision?
- Should the orphaned `c2hlsc-qor-bottleneck-explorer` worktree be harvested, committed on its own branch, or archived? It contains substantial uncommitted work that predates this integration.
- Which FPGA part and clock target should define the next multi-benchmark Vitis run?
