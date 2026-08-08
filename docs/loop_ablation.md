# What each part of the loop is worth — an ablation study

Companion to [`rtllm_v2_session_handoff.md`](rtllm_v2_session_handoff.md) (the RTLLM runbook) and
[`chstone_rosetta.md`](chstone_rosetta.md) (the C-to-HLS-C harnesses). Those two documents say how
high the loop scores. This one asks a different question: **which ingredient is actually paying for
that score, and how confident can we be in the answer?**

**The short version.** This study is underpowered by design — on a 13-design subset, with Holm
correction across seven arms, an arm must flip **nine** designs to clear α=0.05 (§5). Exactly one
does:

- **The repair loop is demonstrated.** Removing repair entirely (`rounds=0`) takes the score from
  10/13 to **1/13** — nine discordant designs, all one way, Holm p=0.027. It clears the corrected
  bar precisely at the floor, and it is the only comparison in the matrix that clears it at all.
- **Everything else is a point estimate, not an effect.** The planner moves the score by one
  design. Repair rounds past the first are flat. Blind retry (`evidence=none`) loses five designs
  — the second-largest gap in the table — and is *still* not significant at Holm p=0.750.
- **The upper bound on richer evidence is zero.** `evidence=oracle`, which may see where the
  candidate diverges from the reference RTL, produced **identical outcomes on all 13 designs** as
  the plain log tail. Whatever the loop is worth, none of it is waiting behind better failure
  evidence.

So the honest summary is narrow: *having a repair loop* is worth a great deal and is proven here;
*how the loop is configured* — planner, evidence channel, round count — is not distinguishable at
this sample size, in either direction.

On **CHStone** the dominant effect is not an agent ingredient at all — it is a harness defect that
was recording zeros for benchmarks the agent never got to attempt. That one is believable despite
also failing the significance bar, because its mechanism is identified in the logs and it moves a
*reachability* metric (3/12 → 12/12), not just a pass rate.

One consequence worth stating in the summary, because it is easy to get backwards: the strict,
never-saw-the-oracle track contains exactly **one** arm — the blind retry — and it is the floor of
the matrix, not a shippable configuration. The uncontaminated number to quote is not an arm at all
but the **round-0 column**, which precedes any evidence under every policy (§1).

---

## 1. What is being measured, and what is held fixed

An ablation is only attributable if exactly one thing moves. Every arm below changes **one factor**
against a single fixed baseline; `scripts/run_ablation.py::validate_arms` is a hard gate that
refuses to run a matrix in which any arm changes zero or two or more factors, so this is enforced
rather than merely intended.

**Fixed across every RTLLM arm:**

| held fixed | value |
| --- | --- |
| model / backend | Claude Opus via `--llm-backend claude-cli --llm-model opus`, sandboxed (no file/shell/network tools, plan mode, scrubbed cwd) |
| samples per design | 1 |
| simulator | iverilog 12.0 `-g2012` + vvp |
| oracle | official RTLLM `auto_run.py` rule — simulator stdout contains `Pass`/`pass` |
| illegal-system-task gate | on (a candidate that prints its own pass banner is refused) |
| testbench shims | on |
| design set | the same 13 designs in every arm (§2) |

**Varied, one at a time, against the baseline `plan=on, evidence=logs, repair rounds=2, samples=1`:**

| factor | baseline | arms |
| --- | --- | --- |
| `plan` | on | off (`no-plan`) |
| `evidence_policy` | `logs` | `none`, `self`, `oracle` |
| `max_repair_rounds` | 2 | 0, 1, 3 |

The four evidence policies form a ladder, each rung adding exactly one channel:

| policy | what the repair agent is shown | track |
| --- | --- | --- |
| `none` | nothing — a blind retry. No stage, no failure family, no tool output | **strict** |
| `logs` | failure stage + family + intent + a family-specific repair procedure + the compile/sim log tail | testbench-fed |
| `self` | `logs`, plus a trace of the candidate's **own** ports and registers, obtained by simulating an instrumented, **non-scored** copy of it | testbench-fed |
| `oracle` | `logs`, plus the line number and expected-vs-produced values where the candidate's stdout first diverges from the reference RTL | testbench-fed **and** reference-fed |

### Which of these count as a strict, self-derived number

Read the "what the agent is shown" column literally and the ladder is cumulative: **every rung
except `none` begins with `logs`.** `self` is not an alternative to `logs`, it is `logs` plus one
extra channel. So although `self`'s extra channel really is self-derived — it is the candidate's
own signals, simulated from an instrumented copy — the policy as a whole still forwards the
benchmark testbench's transcript verbatim, and on **13 of the 50** RTLLM v2 testbenches that
transcript prints the expected value outright in the failing `$display`. (Criterion: a `$display`
whose format string names an expected/golden/reference value *and* interpolates it with a format
specifier — `adder_bcd`, `radix2_div`, `multi_8bit`, `multi_pipe_8bit`, `fixed_point_adder`,
`fixed_point_substractor`, `sub_64bit`, `ring_counter`, `LFSR`, `freq_divbyeven`, `freq_divbyfrac`,
`freq_divbyodd`, `instr_reg`. No testbench leaks a purely literal expected value that this
criterion would miss.)

That makes **`none` the only evidence policy in the strict track**, and it is a floor, not a
shippable configuration. `scripts/run_ablation.py` originally filed `self` as strict on the strength
of its name; `EVIDENCE_TRACKS` now files it as testbench-fed, and
`validate_track_classification()` no longer takes any policy's word for it — it *calls*
`rtllm_agent.build_evidence` with a marked transcript and checks whether the marker survives into
the prompt (`testbench_fed_policies()`), so the classification cannot drift from the implementation
again.

**The genuinely uncontaminated headline is not an evidence policy at all — it is the round-0
column.** Round 0 is the first generation, produced before any evidence of any kind is shown, so it
is identical in construction across all four policies and has had no contact with the oracle. That
is the number to compare against a published single-shot pass@1. Everything measured after a repair
round has consulted the testbench, under every policy except `none`, and belongs in the upper-bound
track.

---

## 2. Why a hard subset, how it was chosen, and what is in it

### Why

Most RTLLM designs pass at round 0 under every configuration. A design that passes in all eight
arms carries no information about any ingredient — it only inflates the denominator and shrinks
every apparent effect. Ablating on the full 50 would mean measuring seven interesting designs
through forty-three constants.

### How

The subset is **the designs that failed functionally at round 0** in the fixed baseline run
`runs/agent` — that is, the generation before any repair. Round-0 pass/fail is read with the
driver's own `summarize_row()`, not restated by the ablation runner, so the selection cannot drift
from how the driver scores. Two categories are then removed, because an arm cannot demonstrate
anything on a design whose verdict is fixed regardless of what it produces:

- **vacuous oracle** — an empty module passes the testbench (`comparator_3bit`, `comparator_4bit`,
  `sequence_detector`, `square_wave`). Of these only `sequence_detector` failed at round 0.
- **unpassable oracle** — catalogued in `rtllm_bench.KNOWN_ORACLE_ISSUES`: `ring_counter`,
  `clkgenerator`, `radix2_div`.

`runs/agent` scored 45/50 functional, 33/50 at round 0, 50/50 syntax. 17 designs failed at round 0;
removing the four above leaves **13**.

### The 13

`LFSR`, `LIFObuffer`, `adder_pipe_64bit`, `alu`, `asyn_fifo`, `barrel_shifter`,
`fixed_point_substractor`, `freq_divbyeven`, `freq_divbyfrac`, `freq_divbyodd`, `pulse_detect`,
`serial2parallel`, `signal_generator`.

### A defect in this selection, stated plainly

Two of the three "unpassable" exclusions are wrong for this purpose. `KNOWN_ORACLE_ISSUES` was
written to adjust the *reference* baseline — its entries record that **the benchmark's own shipped
`verified_*.v` fails its own testbench**, which is the right criterion when asking what the
reference scores. It is the wrong criterion for excluding designs from an agent ablation, and
reusing it here silently imported the wrong test.

In `runs/agent`, `clkgenerator` and `radix2_div` **both failed at round 0 and both passed after
repair** (`func_pass=True`, and on the strict oracle too). They are exactly the kind of design this
study is trying to measure — the repair loop rescuing a failed generation — and they were dropped on
the grounds that "no RTL can score", which is false for both. Only `ring_counter` is genuinely
unpassable (the testbench's two `always @(posedge clk)` blocks race and iverilog never prints the
banner).

The honest subset is therefore **15 designs, not 13**. This does not change any verdict below —
the significance floor is 9 discordant designs at n=13 and still 9 at n=15 — but it does mean every
rate in §3 is computed over a subset that is 2 designs smaller than it should be, and that the
missing 2 are designs where repair is known to work. Treat the absolute rates as approximate; the
between-arm comparisons are unaffected because all arms ran the identical subset.

### What the subset is not

These rates are **not** comparable to the full-suite numbers in the handoff document. The subset is
selected on round-0 failure, so by construction it is the hard tail. A `rounds=0` arm measured here
is being asked to re-pass designs chosen for having failed at round 0 in a previous sample of the
same configuration, which is a regression-to-the-mean setup: it will score above zero purely
because generation is stochastic, and that number means nothing on its own. It is only meaningful
*relative to the other arms*, which all inherit the identical selection bias.

---

## 3. RTLLM results

<!--RTLLM_RESULTS:BEGIN-->
All arms ran the same 13 designs. The `significance` column is the verdict; the delta columns are point estimates with intervals and must not be read as effects on their own.

| arm | track | func | round-0 | Δ designs | Δ pp [95% CI] | significance |
| --- | --- | :-: | :-: | :-: | :-: | --- |
| **`baseline`** | oracle-derived | 10/13 (77%) | 1/13 (8%) | reference | reference | reference arm |
| `no-plan` | oracle-derived | 9/13 (69%) | 3/13 (23%) | -1 | -7.7 [-23, +0] | NOT SIGNIFICANT -- cannot be: 1 discordant of 13 (+0/-1), Holm p=1.000; at alpha=0.05 no arm reaches significance with fewer than 9 discordant designs |
| `evidence=none` | self-derived | 5/13 (38%) | 1/13 (8%) | -5 | -38.5 [-69, +0] | NOT SIGNIFICANT -- cannot be: 7 discordant of 13 (+1/-6), Holm p=0.750; at alpha=0.05 no arm reaches significance with fewer than 9 discordant designs |
| `evidence=self` | oracle-derived | 11/13 (85%) | 1/13 (8%) | +1 | +7.7 [+0, +23] | NOT SIGNIFICANT -- cannot be: 1 discordant of 13 (+1/-0), Holm p=1.000; at alpha=0.05 no arm reaches significance with fewer than 9 discordant designs |
| `evidence=oracle` | oracle-derived | 10/13 (77%) | 1/13 (8%) | +0 | +0.0 [+0, +0] | identical outcomes on all 13 designs |
| `rounds=0` | oracle-derived | 1/13 (8%) | 1/13 (8%) | -9 | -69.2 [-92, -46] | significant (arm below baseline): 9 discordant of 13 (+0/-9), Holm p=0.027 |
| `rounds=1` | oracle-derived | 8/13 (62%) | 1/13 (8%) | -2 | -15.4 [-38, +0] | NOT SIGNIFICANT -- cannot be: 2 discordant of 13 (+0/-2), Holm p=1.000; at alpha=0.05 no arm reaches significance with fewer than 9 discordant designs |
| `rounds=3` | oracle-derived | 10/13 (77%) | 0/13 (0%) | +0 | +0.0 [-23, +23] | NOT SIGNIFICANT -- cannot be: 2 discordant of 13 (+1/-1), Holm p=1.000; at alpha=0.05 no arm reaches significance with fewer than 9 discordant designs |

**1 of 7 arms clears the corrected bar: `rounds=0`.** At n=13 with Holm across 7 tests the floor is **9 discordant designs** (§5). Every other row is a measurement with an interval, not a demonstrated effect, and no direction word appears in its significance column.

### Reading the arms

- **The repair loop is the one demonstrated ingredient.** With no repair at all (`rounds=0`) the score is 1/13 against the baseline's 10/13 — 9 discordant designs, all in the same direction, Holm p=0.027. This is the only comparison in the matrix that clears the corrected bar, and it clears it exactly at the floor. Everything the loop scores on this subset, the loop earned: the generator alone recovers almost none of it.
- **Repair works by diagnosis, not by resampling — but this arm does not prove it.** Blind retry (`evidence=none`) keeps the retries and removes the evidence, and scores 5/13 against 10/13. The point estimate is large (-38.5 pp) and the direction is the expected one, but at 7 discordant (+1/-6) it is Holm p=0.750 and **not significant**. It is the second-largest effect in the table and still cannot be claimed.
- **The upper-bound evidence channel adds literally nothing.** `evidence=oracle` is allowed to see where the candidate's output first diverges from the *reference RTL* — an advantage no shippable system has — and it produced **identical outcomes on all 13 designs** as the baseline (10/13). Zero discordant. This is the most useful negative result here: it bounds how much of the score could possibly be attributed to richer failure evidence, and the bound is zero.
- **`evidence=self` is the nominal top scorer and should not be read as one.** It scores 11/13 against 10/13 — a one-design difference, which is 7.7 pp and this experiment's noise floor. It is also not a strict-track arm (§1), so it is not a candidate for a headline configuration even if the difference were real.
- **Returns past the first repair round are flat.** `rounds=1` scores 8/13, the baseline (`rounds=2`) 10/13, `rounds=3` 10/13. The jump is from 0 rounds to 1; after that the curve is level within noise, which is the argument for leaving the default at 2 rather than raising it.
- **The planner is not measurable here.** `no-plan` scores 9/13 against 10/13 — one design, one discordant. Nothing in this matrix supports keeping or dropping it.

The subset's absolute rates carry a selection artifact worth restating: every design here failed at round 0 in the source run, so `rounds=0` scoring above zero at all is regression to the mean in a stochastic generator, not evidence that the generator improved. Only the between-arm comparisons mean anything.
<!--RTLLM_RESULTS:END-->

---

## 4. CHStone results

<!--CHSTONE_RESULTS:BEGIN-->
Two columns, and the left one is the one that matters. **`reached the oracle`** counts benchmarks whose equivalence binary built at all, so the candidate was actually exercised. A benchmark that never reached the oracle did not fail — it was never measured, and scoring it 0 is a harness defect reporting itself as a result.

| generator | staging | repair rounds | reached the oracle | passed | run |
| --- | --- | :-: | :-: | :-: | --- |
| deterministic | `legacy_inline` | 0 | **8/12** | 0/12 | `runs/abl_det_legacy_r0` |
| deterministic | `legacy_inline` | 1 | **3/12** | 0/12 | `runs/abl_det_legacy_r1` |
| deterministic | `legacy_inline` | 2 | **3/12** | 0/12 | `runs/abl_det_legacy_r2` |
| deterministic | `legacy_inline` | 3 | **3/12** | 0/12 | `runs/abl_det_legacy_r3` |
| deterministic | `golden_c_tu` | 0 | **12/12** | 0/12 | `runs/abl_det_staged_r0` |
| deterministic | `golden_c_tu` | 1 | **12/12** | 6/12 | `runs/abl_det_staged_r1` |
| deterministic | `golden_c_tu` | 2 | **12/12** | 6/12 | `runs/abl_det_staged_r2` |
| deterministic | `golden_c_tu` | 3 | **12/12** | 6/12 | `runs/abl_det_staged_r3` |
| LLM | `golden_c_tu` | 0 | **12/12** | 1/12 | `runs/abl_llm_staged_r0` |
| LLM | `golden_c_tu` | 1 | **12/12** | 8/12 | `runs/chstone_llm_staged` |
| LLM | `golden_c_tu` | 2 | **12/12** | 9/12 | `runs/abl_llm_staged_r2` |

_Still in flight at the time of writing: `abl_llm_staged_r3`, `abl_llm_legacy_r1`. Re-run `scripts/render_ablation_sections.py` to fill these in._

### The findings

**0. The repair loop carries this suite too, and the return is almost all in the first round.** With staging fixed so every benchmark reaches the oracle, the deterministic converter goes **0/12 → 6/12** on the first repair round and then flat (0/12, 6/12, 6/12, 6/12 at 0/1/2/3 rounds); the LLM generator goes **1/12 → 8/12** on the first round (1/12, 8/12, 9/12 at 0/1/2 rounds). This is the same shape as the RTLLM result in §3: generation alone recovers almost nothing, one repair round recovers most of it, and further rounds add little. Note the CHStone repair is largely *mechanical* — `hlsc_repair_agent` applies deterministic fixes before any model is consulted — so this is not a claim about LLM self-correction.

**1. The dominant CHStone effect is a harness fix, not an agent ingredient.** Moving from `legacy_inline` to `golden_c_tu` staging takes reachability from 3/12 to 12/12 at one repair round. Reporting that as a pass-rate improvement would be wrong twice over: it understates the change (nine benchmarks went from *unmeasured* to *measured*, which is not the same as nine failures becoming passes), and it credits the agent for a defect in the test rig.

**2. Under the old staging, enabling repair made things worse.** `legacy_inline` reaches 8/12 with repair off and **3/12 with one repair round**. The mechanism is identified, not inferred: the repair round includes the original C into the candidate to supply helper definitions, the golden reference is already inlined into the same binary, and the link fails with `multiple definition of 'main_result'`. A repair step that destroys reachability is the kind of thing a pass-rate-only report hides completely — both configurations score 0/12.

### On significance

At n=12 with Holm across this family, **even a clean 0→6 sweep does not clear α=0.05**: p=0.031 uncorrected, **p=0.156 corrected**. Both are reported; neither is dropped. The case for the staging fix does not rest on a p-value — it rests on an identified mechanism, a link error in the logs, and a reachability metric that moves from 3/12 to 12/12.
<!--CHSTONE_RESULTS:END-->

---

## 5. How much of a difference is real?

This section is the one to read before quoting anything above it.

### The arithmetic of a small subset

On a 13-design subset **one design is 7.7 percentage points**. On the 12-benchmark CHStone
suite one benchmark is 8.3. Any delta of one or two designs is inside the noise of a single
resample of a stochastic generator, and this study runs `--samples 1`.

### The test, and why it is paired

Arms are not independent samples: every arm runs the *same designs* under a different
configuration. So the comparison is paired, and the statistic is an **exact McNemar test** — a
two-sided binomial sign test on the **discordant** designs only (baseline passed & arm failed,
versus arm passed & baseline failed). Designs that came out the same way in both arms carry no
information about the difference between them and are correctly excluded from the test, even
though they are counted in each arm's rate.

### The floor, computed rather than asserted

Because the test is a sign test on discordant pairs, there is a hard minimum number of
discordant designs below which **no arrangement of results can reach significance**:

| correction | family size | minimum discordant designs | what 6 discordant would score |
| --- | :-: | :-: | --- |
| none | 1 | **6** | p = 0.031 |
| Holm | 5 (CHStone) | **8** | p = 0.156 |
| Holm | 7 (RTLLM) | **9** | p = 0.219 |

The verdict this study reports is the **Holm-corrected** one, so the floor that applies to the
RTLLM matrix is **9 discordant designs out of 13** — an arm would have to flip nine designs, all
in the same direction, to clear α=0.05. Nothing in a 13-design subset is going to do that. This
is stated as a limit of the design, not discovered afterwards: `run_ablation.py --dry-run`
prints the floor before a single arm runs.

An earlier version of the runner printed the *uncorrected* floor (6) next to a *corrected*
verdict, which understated the real bar by three designs. `min_discordant_for_significance()`
now takes the family size, and a test pins each value.

### What this means for how the results may be described

`significance_verdict()` is the only place in the runner permitted to emit judgement language,
and it emits a direction word ("above", "below") **only** when the corrected p clears α.
Everything else reads `NOT SIGNIFICANT` with the counts attached, so a reader can see exactly
why. Concretely, for this study:

- **Exactly one RTLLM arm may be described directionally: `rounds=0`.** It flips nine designs,
  all the same way, for Holm p=0.027 — the only comparison that clears the corrected bar, and it
  clears it exactly at the floor of 9. Every other RTLLM delta is reported as a measurement with
  an interval and never as an effect, including `evidence=none`, which has the second-largest
  point estimate in the table (-38.5 pp) and a corrected p of 0.750.
- **The one significant result is a removal, not an improvement.** Nothing here shows any arm
  scoring *above* the baseline. `evidence=self` is nominally highest at 11/13, which is one
  design, and one design is noise. No configuration change is supported by this matrix.
- **The CHStone effects are large and mechanistically explained, and still not significant.** A
  clean 0→6 sweep at n=12 is p=0.031 uncorrected and **p=0.156 after Holm across the 5-arm
  family**. Both are reported side by side; neither is quietly dropped in favour of the
  flattering one. The reason to believe the staging effect is not the p-value — it is that the
  mechanism is identified, the failing link error is in the logs, and the fix moves reachability
  from 3/12 to 12/12.
- **A null result is a result.** If no arm separates from the baseline, the honest conclusion is
  that this matrix cannot distinguish them at this sample size — not that the ingredients are
  worthless, and not that the highest-scoring arm won.

### What would actually settle it

Roughly: `--samples 5` on a 30-design subset, which puts ~150 generations behind each arm
instead of 13 and makes a 2-3 design effect resolvable. At ~10 minutes per design per arm that
is on the order of 40 agent-hours for the full matrix. That is the honest price of a
significant answer here, and it was not paid.

---

## 6. Reproducing this

Every arm is a subprocess of `scripts/run_rtllm_v2.py` with `--resume` always on, so an
interrupted matrix continues where it stopped and a completed one costs nothing to re-analyse.

```bash
RTLLM=/path/to/RTLLM-v2.0

# What would run, without running it -- prints every arm's full command line and the
# power note (n, pp-per-design, and the Holm-corrected discordant floor).
python3 scripts/run_ablation.py --benchmark "$RTLLM" \
  --hard-subset-from runs/agent/results.jsonl --out-dir runs/ablation_rtllm --dry-run

# The matrix. Arms run sequentially; designs within an arm run --workers wide.
python3 scripts/run_ablation.py --benchmark "$RTLLM" \
  --hard-subset-from runs/agent/results.jsonl --out-dir runs/ablation_rtllm \
  --workers 8 --resume

# Re-render the report from arm directories already on disk, running nothing. Use this
# after changing anything in the analysis -- track classification, alpha, the floor.
python3 scripts/run_ablation.py --benchmark "$RTLLM" \
  --hard-subset-from runs/agent/results.jsonl --out-dir runs/ablation_rtllm --report-only
```

CHStone arms are `scripts/run_chstone.py` invocations differing in one factor each:

```bash
CH=/path/to/CHStone
for r in 0 1 2 3; do
  python3 scripts/run_chstone.py --benchmark "$CH" --out-dir "runs/abl_det_staged_r$r" \
    --workers 3 --label "det_staged_r$r" --resume --repair-rounds "$r"
done
# and --legacy-staging for the pre-fix staging, --use-llm for the LLM generator.
```

**Two things that will not reproduce identically.** The generator is a sampled LLM at
`--samples 1`, so per-design outcomes vary between runs; only the paired between-arm
comparisons are stable, and even those carry the uncertainty in §5. And the `claude-cli`
backend is shared, so running two sweeps concurrently can slow both — an arm that fails on
backend contention measures the backend, not the arm. Run arms sequentially.

### Provenance of every number in this document

| section | run directories |
| --- | --- |
| the hard subset | `runs/agent` (the fixed baseline sweep the subset is derived from) |
| RTLLM arms (§3) | `runs/ablation_rtllm/<arm>` |
| CHStone deterministic arms (§4) | `runs/abl_det_{legacy,staged}_r{0,1,2,3}` |
| CHStone LLM arms (§4) | `runs/chstone_llm_staged`, `runs/abl_llm_*` |
| calibration | `runs/reference`, `runs/empty`, `runs/chstone_native_recheck` |

---

## 7. Corrections made to this study's own machinery

An ablation is a measuring instrument, and this one was found to be miscalibrated in three
places while it was being run. All three are recorded here rather than quietly fixed, because
the point of the document is that its numbers can be audited.

### 7.1 `evidence=self` was filed in the strict track, and is not strict

`EVIDENCE_TRACKS` classified `self` as self-derived on the strength of its name and of its
extra channel, which genuinely is the candidate's own signals. But the policy is implemented as
**`logs` plus that channel**, so it forwards the benchmark testbench's transcript verbatim — the
same transcript that makes `logs` oracle-derived. A strict headline could therefore have been
quoted from an arm that had read the oracle's verdict.

The name-list check that was supposed to prevent this could not catch it: it compared
`EVIDENCE_TRACKS` against `rtllm_agent.ORACLE_DERIVED_POLICIES`, and the driver does not stamp
`self` either (its narrow question is "derived from the *reference RTL*", and a self-trace is
not). Two name lists agreeing with each other proved nothing.

`testbench_fed_policies()` now answers the question by **calling** `build_evidence` with a
marked transcript and checking whether the marker reaches the prompt. `validate_track_classification()`
tests containment against that observed set as well as the declared one. Both mutations —
`self` or `logs` forced back to strict — now fail loudly, with a test for each.

### 7.2 The reported significance floor was the uncorrected one

The report printed "no arm reaches significance with fewer than **6** discordant designs" while
`significance_verdict()` tested the **Holm-corrected** p. Those are different bars: 6 discordant
all in one direction is p=0.031 uncorrected but p=0.219 after Holm across seven arms. The
printed floor understated the real bar by three designs, in a document whose entire purpose is
to stop small deltas being over-read.

`min_discordant_for_significance()` now takes the family size and is called with it, giving
**9** for the seven-arm RTLLM matrix and **8** for the five-arm CHStone one.

### 7.3 A leaked-expected-value count was off by one

The claim that "14 of the 50 RTLLM testbenches print the expected value outright" was carried
over unverified. Recounting against a stated criterion — a `$display` whose format string names
an expected/golden/reference value *and* interpolates it — gives **13**, listed in §1. No
testbench leaks a purely literal expected value that the criterion would miss. The argument the
number supports is unchanged; the number is now checkable.

### What is *not* corrected

The hard subset is still the 13 designs described in §2, not the defensible 15. All arms ran the
identical subset, so every between-arm comparison is unaffected, and re-running eight arms to
move two designs that change no verdict was not worth the backend time. The defect is documented
in §2 and the absolute rates in §3 should be read as approximate.
