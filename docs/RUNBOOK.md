# Runbook: reproducing the benchmark numbers from a fresh checkout

Everything below assumes a machine that is **not** this dev container.

---

## 0. Get the right code

The verification fixes are on a branch, not on `main`. `main` is **36 commits behind** and
still has the defects that let roughly half the LeVeri suite pass while comparing nothing.
Numbers produced from `main` are not measurements.

```bash
git clone https://github.com/huluk98/c2hlsc-agent
cd c2hlsc-agent
git checkout claude/agent-component-scaffold-5cr39w   # until PR #23 merges
pip install -e .
```

## 1. Fetch the benchmarks

`third_party/` is gitignored, so **nothing under it ships with the clone**. `data/` IS
tracked, so the HLS_NL dataset (53 MB) arrives with the checkout.

| Suite | Status | How to get it |
| --- | --- | --- |
| **HLS-LeVeri** (107) | fetch | `git clone --depth 1 https://github.com/cz-5f/HLS-LeVeri third_party/HLS-LeVeri` |
| **CHStone** (12) | fetch | `python3 scripts/fetch_chstone.py` |
| **HLS_NL** (~10k) | ships with clone | `data/hls_nl/hls_nl_repaired.accepted.jsonl` |
| **HLS-Eval** (94) | **not wired** | needs a fetcher + harness |
| **Bench4HLS** (170) | **not wired** | needs a fetcher + harness |

The LeVeri harness prints that clone command itself if the benchmark is missing, so you
cannot get it wrong by forgetting.

## 2. Check the toolchain

```bash
python3 -m c2hlsc_agent doctor          # add --install to fetch what it can
```

| Tool | Needed for | Without it |
| --- | --- | --- |
| `g++`, `python3` | host tiers — **required** | nothing runs |
| `iverilog` + `vvp` | direct-RTL tier | tier reports `skipped` |
| `klee` | `refine` coverage loop | falls back to widening |
| `vitis_hls` | csim / csynth / cosim | all three report `blocked` — **not** a design failure |

## 3. Run

```bash
# smoke: 6 designs, ~25 min, ~9 GB
python3 scripts/leveri_harness.py --limit 6 --out build/smoke --timeout 900

# pass@k: --samples must be >= max(k)
python3 scripts/leveri_harness.py --limit 107 --samples 10 --k 1,5,10 \
        --out build/leveri_k --timeout 900

# with repair (the ORTHOGONAL axis — see docs/ablation_sweep_prompt.md)
python3 scripts/leveri_harness.py --limit 107 --samples 10 --k 1,5,10 \
        --out build/leveri_k_R3 --timeout 1800 \
        --convert-arg=--auto-repair --convert-arg=--max-iterations=3

# with Vitis, for the four-stage all-pass number
        --convert-arg=--run-vitis                       # local
        --convert-arg=--vitis-ssh=user@linux-host       # remote

# CHStone
python3 scripts/chstone_harness.py --out build/chstone --samples 1 --k 1

# single design, for debugging
python3 scripts/leveri_harness.py --ids 00005 --out build/one --timeout 600
```

### Disk

Each 6-design run is **~9 GB**: the paired trace writes one CSV column per array element.
A 107-design sweep will fill an ordinary disk. Until the trace-scaling policy is settled,
run in batches and keep only the reports:

```bash
cp build/<cell>/leveri_report.json reports/<cell>.json && rm -rf build/<cell>
```

The report is ~4 KB. The generated projects are not worth keeping.

## 4. Read the output correctly

Two files matter:

- `build/<out>/leveri_report.json` — `pass_at_k`, `blocker_histogram`, per-entry rows
- `build/<out>/<id>_s0/conversion_report.json` — per-design detail

**Before quoting any pass@k, check the evidence.** Every tier records how many values it
compared:

```bash
python3 - <<'PY'
import json, glob
for f in sorted(glob.glob("build/<out>/*_s0/conversion_report.json")):
    d = json.load(open(f)); ph = d.get("phases", {})
    se, tc = ph.get("software_equivalence", {}), ph.get("trace_consistency", {})
    print(f"{d['top']:12s} {d['status']:6s} "
          f"se={se.get('status'):8s} n={se.get('comparisons')} "
          f"tc={tc.get('status'):8s} n={tc.get('comparisons')}")
PY
```

Read it like this:

| Pattern | Meaning |
| --- | --- |
| `n` > 0 on both, and the two agree | real pass, cross-checked by two independent tiers |
| `n = 0` | **void.** The tier examined nothing; do not count it as a pass |
| `n = None` | that tier reports no count (e.g. never ran) — not the same as zero |
| `se=pass n=large`, `tc=fail n=None` | **tooling** failure, not an agent failure (this is `knn`) |
| `csim/csynth/cosim = blocked` | no Vitis. The run's overall `fail` is honest, but it is not the agent's fault |

Classify every failure as `agent`, `tooling`, or `blocked` before it enters a table. On the
current 6-design subset, one of the six failures is tooling, not agent — counting it wrong
understates the agent by 1/6.

## 5. Known-good reference

At commit `3bd0773`, 6-entry LeVeri subset, host tiers only, no Vitis:

```
pass@1 = 0.8333 (5/6)

              status   software_equivalence      trace_consistency
knn           fail     pass  n=104,857,600       fail  n=None      <- tooling
syrk          pass     pass  n=819,200           pass  n=819,200
dilate        pass     pass  n=27,852,800        pass  n=27,852,800
jacobi_1d     pass     pass  n=24,000            pass  n=24,000
nussinov      pass     pass  n=360,000           pass  n=360,000
avg_pool      pass     pass  n=51,200            pass  n=51,200
                                                 vacuous: 0 of 6
```

If your run does not reproduce this, something in the environment differs — check `doctor`
first, and confirm you are on the branch and not on `main`.
