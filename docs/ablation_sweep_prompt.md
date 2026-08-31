# Ablation sweep prompt — pass@k × repair × components

Hand this to a fresh agent session (or run it yourself). It is written to be re-run each
time a new component lands, so the table grows a row rather than being rebuilt.

---

## Task

Produce a single results table for the c2hlsc agent across four orthogonal axes. Do not
collapse any two axes into one.

| Axis | Values | How it is set |
| --- | --- | --- |
| **Benchmark** | `leveri` (107), `chstone` (12), `hls_eval` (94), `bench4hls` (170) | harness choice |
| **k — within-round sampling** | 1, 5, 10 | `--samples K --k 1,5,10` on the harness |
| **R — cross-round repair cap** | 0, 1, 3 | `--convert-arg=--auto-repair --convert-arg=--max-iterations=R` (R=0 means omit `--auto-repair`) |
| **Config — components enabled** | see the component ladder below | `--convert-arg=...` flags per component |

`k` and `R` are INDEPENDENT. `pass@5, R=0` and `pass@1, R=3` are different experiments and
must appear as different cells. Never report a single "top-5 with repair" number that
silently mixes them.

## Component ladder (append a row as each lands)

| Config | Description | Flags |
| --- | --- | --- |
| `base` | deterministic generator, no model | `--no-llm` |
| `+llm` | model generation, no repair | `--use-llm --llm-backend <b> --llm-model <m>` |
| `+repair` | bounded repair loop | `+ --auto-repair --max-iterations R` |
| `+refine` | coverage-driven KLEE refinement | run `c2hlsc-agent refine --target 100` between rounds |
| `+kg` | graph-based knowledge | _(flag TBD when built)_ |
| `+reprog` | consistent reprogramming | _(flag TBD when built)_ |

Each new component is ONE new row at fixed (benchmark, k, R), plus a full k×R sub-sweep on
the primary benchmark only. Do not run the full cross-product for every component: it is
|benchmarks| × 3 × 3 × |configs| runs and will not finish.

## Metrics — every cell reports ALL of these

| Column | Source | Why |
| --- | --- | --- |
| `pass@k` | harness, Chen et al. unbiased estimator | headline |
| `n_compared` | `conversion_report.json` → `phases.*.comparisons` | **evidence.** A pass with `n_compared = 0` is vacuous and must never be counted |
| per-tier rate | `software_equivalence` / `trace_consistency` / `csim` / `csynth` / `cosim` | four-stage all-pass is the comparable headline in the literature |
| `blocker` histogram | harness `blocker_histogram` | tells you WHERE it stops |
| `repair_iters` | mean over passing cases | cost of the repair axis |
| `tokens`, `wall_s` | run log | cost per case |

### Hard rules on reporting

1. **A cell with any `comparisons = 0` is void.** Report it as `n/a (vacuous)`, never as a
   pass. The oracle and trace tiers each emit their own count; they must agree.
2. **Never quote a pass@k measured before commit `589c8c8`.** Runs before that had open
   vacuity routes and 48% of the LeVeri suite passed while comparing nothing.
3. **Separate tooling failures from agent failures.** `knn` fails `trace_consistency`
   because the trace exceeds practical size, while its oracle compares 104,857,600 values
   and passes. That is a tooling limit; counting it as an agent failure understates the
   agent. Add a `failure_kind` column: `agent` | `tooling` | `blocked`.
4. **csim/csynth/cosim require Vitis.** Without it they report `blocked`, and the run's
   overall status is `fail` — correct, but it is NOT an agent failure. Never report
   csim/cosim numbers from a host with no Vitis.

## Commands

```bash
# one cell: benchmark=leveri, k∈{1,5,10}, R=0 (no repair), config=+llm
python3 scripts/leveri_harness.py \
  --limit 107 --samples 10 --k 1,5,10 \
  --out build/sweep/leveri_llm_R0 --timeout 900 \
  --convert-arg=--use-llm --convert-arg=--llm-backend=anthropic

# same cell with repair cap R=3
python3 scripts/leveri_harness.py \
  --limit 107 --samples 10 --k 1,5,10 \
  --out build/sweep/leveri_llm_R3 --timeout 1800 \
  --convert-arg=--use-llm --convert-arg=--llm-backend=anthropic \
  --convert-arg=--auto-repair --convert-arg=--max-iterations=3

# with Vitis, for the four-stage all-pass number
  ... --convert-arg=--run-vitis          # or --convert-arg=--vitis-ssh=user@host
```

**Disk:** each 6-design run is ~9 GB because the paired trace writes one CSV column per
array element. A 107-design sweep will exhaust the disk. Either resolve the trace-scaling
policy first, or run in batches of ~6 and delete `build/<cell>/` after extracting
`leveri_report.json` (the report is ~4 KB; the projects are not worth keeping).

## Output

Write `docs/results/<date>_sweep.md` with one row per (benchmark, config, k, R) and every
metric column above. Also emit `docs/results/<date>_sweep.json` with the raw rows so the
table can be regenerated without re-running.

State explicitly, in prose above the table:
- which tiers actually executed and which were `blocked`,
- the total values compared across the sweep,
- any cell voided for vacuity, and why.
