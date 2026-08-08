# Running the RTLLM v2 harness on native Windows

`docs/rtllm_v2_session_handoff.md` assumes Ubuntu (`sudo apt-get install -y iverilog`). This
file records what it takes on native Windows, and what was actually measured there on
2026-08-08. Everything below was verified on this machine, not inferred.

Host: Windows 10, zh-CN locale (`locale.getpreferredencoding(False)` → `cp936`, `gbk`),
Python 3.12, Icarus Verilog 14.0 from OSS CAD Suite `20260806`.

## 1. Simulator: put BOTH `bin` and `lib` on PATH

The suite's own `environment.bat` does `set PATH=%ROOT%bin;%ROOT%lib;%PATH%`. **Both.** With
only `bin`, `iverilog` compiles fine and `vvp.exe` dies without a word:

```
rc = 3221225781   (0xC0000135 STATUS_DLL_NOT_FOUND)   stdout = b''   stderr = b''
```

`libvvp-1.dll` sits next to `vvp.exe` in `bin`, but its transitive dependencies are in `lib`.
The Windows loader kills the process before it can write to either stream, so the harness saw
an empty log and — before the fix in §5 — reported every design as `family=no_output`, i.e.
a *design* verdict. The sweep printed a clean `reference: 0/50`, which reads as "these designs
are bad" rather than "your simulator is unusable".

```bash
R="$HOME/.local/opt/oss-cad-suite-20260806/oss-cad-suite"
export PATH="$R/bin:$R/lib:$PATH"
vvp -V   # must print a version, not a loader error
```

**The calibration gate in §3 of the handoff doc is what catches this.** Do not skip it.

## 2. Calibration, measured on Windows

Both gates match the documented Linux values exactly:

```bash
python scripts/run_rtllm_v2.py --benchmark "$RTLLM_ROOT" --reference      --out-dir runs/reference --workers 8
python scripts/run_rtllm_v2.py --benchmark "$RTLLM_ROOT" --empty-baseline --out-dir runs/empty     --workers 8
```

| run | required | measured on Windows |
| --- | :-: | :-: |
| `--reference` (ceiling) | 47/50 | **47/50** (94.0% official, 43/43 adjusted, syntax 50/50) |
| `--empty-baseline` (floor) | 4/50 | **4/50** (8.0% official, 0/43 adjusted) |

The four designs that pass with **no logic at all** — a vacuous oracle, so their score is
meaningless: `comparator_3bit`, `comparator_4bit`, `sequence_detector`, `square_wave`.

Worked example of why, from `Arithmetic/Comparator/comparator_3bit/testbench.v`:

```verilog
if ((A > B && !A_greater) || (A == B && !A_equal) || (A < B && !A_less))
  error = error + 1;
```

With an empty module the outputs are undriven (`z`), `!z` is `x`, and `if (x)` takes the false
branch — so `error` stays 0 and the bench prints `Your Design Passed`. It only ever checks that
the *correct* output went high, never that the other two went low, so it also passes a design
that asserts all three at once.

## 3. LLM backend: three separate Windows traps

Only the agent run needs a model; `--reference`, `--empty-baseline` and `--external-rtl` do not.

1. **Use `claude.cmd`, not `claude`.** The npm shim `claude` is a `#!/bin/sh` script, so Python
   raises `OSError: [WinError 193] %1 is not a valid Win32 application`.
2. **Use forward slashes** in `--llm-cli-cmd`. The flag is `shlex.split()` in POSIX mode, so
   backslashes are silently eaten: `C:\Users\...` arrives as `C:Users...` and the harness then
   correctly refuses to run ("is not on PATH").
3. **`claude` must be logged in.** `claude -p` returns rc=1 with `Not logged in · Please run
   /login` on *stdout*, which surfaces as `family=llm_error`. Run `claude` once interactively
   and `/login` first. The harness does not score these — it warns that they are not model
   results — which is correct behaviour, but it means a whole sweep can cost you nothing but
   time if you skip this.

```bash
python scripts/run_rtllm_v2.py --benchmark "$RTLLM_ROOT" \
  --llm-backend claude-cli --llm-model opus \
  --llm-cli-cmd 'C:/Users/<you>/AppData/Roaming/npm/claude.cmd' \
  --max-repair-rounds 2 --out-dir runs/agent --workers 8
```

## 4. Scoring pre-generated RTL (no model needed)

Measured against the calibration above, scoring the committed `rtllm_v2_results/designs`:

```bash
python scripts/run_rtllm_v2.py --benchmark "$RTLLM_ROOT" \
  --external-rtl rtllm_v2_results/designs --label committed-run \
  --out-dir runs/agent --workers 8
python scripts/triage_rtllm_run.py --run runs/agent --reference runs/reference --empty runs/empty --markdown
```

| bucket | n | designs |
| --- | :-: | --- |
| free (empty stub passes — vacuous oracle) | 4 | `comparator_3bit`, `comparator_4bit`, `sequence_detector`, `square_wave` |
| unscorable (neither reference nor candidate passes) | 2 | `radix2_div`, `ring_counter` |
| reference wrong (candidate passes a bench the reference fails) | 1 | `clkgenerator` |
| real signal — passed | 39 | |
| **REAL FAILURE** | 4 | `asyn_fifo`, `barrel_shifter`, `serial2parallel`, `signal_generator` |

- official: **44/50 (88.0%)**
- signal basis: **40/44 (90.9%)** — the honest number; 6 designs carry no information
- lifted by the repair loop: **0**

### Reproducibility: three designs flip against the committed results

`rtllm_v2_results/results.jsonl` records 43/50; this run scores the *same RTL* at 44/50.

| design | committed | fresh | why |
| --- | :-: | :-: | --- |
| `alu` | fail (`missing_golden_data`) | **pass** | setup defect in the original run, not a design failure |
| `calendar` | fail (`missing_golden_data`) | **pass** | same |
| `ring_counter` | pass | **fail** | the reference fails it here too → simulator-version dependent |

The committed run used **Icarus Verilog 12.0**; this one used **14.0**. `missing_golden_data`
is called out in the handoff doc as a setup problem (support files not reaching the sandbox),
so the committed 43/50 was undercounting by two. Do not compare scores across simulator
versions without re-running `--reference` on both.

## 5. Harness change that came out of this

`rtllm_bench.classify_failure` gained a `simulator_launch_failed` family
(`SIMULATOR_LAUNCH_FAILURE_CODES`, `SimResult.sim_returncode`). A simulator that never started
tells you nothing about the design and must not be booked as a design failure. Detection is
evidence-based — a known loader exit code with an empty log, or a loader message in the log —
and deliberately does **not** treat `returncode is None` as a launch failure, because that is
indistinguishable from "this caller recorded no returncode" and would relabel legacy records.

See `tests/test_rtllm_simulator_launch.py`.
