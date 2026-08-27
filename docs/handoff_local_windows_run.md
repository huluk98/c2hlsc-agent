# Handoff: Local Windows Run of the Combined Workflow (CHStone)

This is the working handoff for a Claude Code session running **locally on the
operator's Windows machine**, where the full toolchain is available. It points
at the exact state produced by the remote implementation session and says what
to run next. Read `docs/handoff_cli_runbook.md` (implementation spec, tasks
1–5 all landed) and `AGENTS.md` (binding guardrails) before acting.

## 1. Where you are starting from

- **Branch:** `claude/combined-generation-workflow` on
  `huluk98/c2hlsc-agent` — work on this branch; do not rebase or force-push it.
- **What it contains:** the combined generation workflow (evidence-driven
  closed-loop spine + LeVeri shift-left gate): golden-trace smoke before
  generation, the `leveri_trace` ladder phase between host equivalence and the
  Vitis phases, structured first-divergence evidence in the repair prompt, the
  K=3 standard run configuration, the generated-testbench drift check, and
  Windows support (`C2HLSC_VITIS_BIN`, make `PYTHON` override, `.exe` smoke).
- **Verified so far (in a Linux container, no Vitis available):**
  - Full offline unit suite green: 249 tests.
  - `examples/chstone_dfmul` ran end-to-end through the available ladder with
    the Claude CLI backend: golden smoke pass → LLM authored a 654-line
    HLS-ready translation unit (1 LLM call, 0 repairs) → host equivalence
    200/200 → `leveri_trace` pass. **CSim/CSynth/CoSim were skipped: no Vitis
    in that container.** Completing them is this session's job.
  - `examples/chstone_dfadd` and `examples/chstone_dfdiv` are assembled and
    syntax-checked (C and C++), but **have not been run at all yet**.
- CHStone provenance: each example is the kernel's `softfloat.c` include chain
  inlined into one file, `main()` driver omitted, all CHStone/SoftFloat
  license headers preserved, plus a plain-typed top wrapper
  (`dfmul_top`/`dfadd_top`/`dfdiv_top`, two `unsigned long long` bit-pattern
  inputs, one output). Source mirror:
  `github.com/A-T-Kristensen/patmos_HLS` `benchmarks/CHStone/`.

## 2. Machine setup (once)

```bat
git clone -b claude/combined-generation-workflow https://github.com/huluk98/c2hlsc-agent.git
cd c2hlsc-agent
python -m pip install -e .
python -m unittest discover -s tests
set C2HLSC_VITIS_BIN=D:\Xilinx\Vivado\2024.2\bin\vitis_hls.bat
```

- Use the `.bat` launcher in `bin\` — **not** the raw binaries under
  `bin\unwrapped\` — it sets up the tool environment; `C2HLSC_VITIS_BIN`
  exists because Windows cannot resolve a bare `vitis_hls` to a `.bat` from
  PATH.
- Host stages need `make` and `g++` on PATH (MSYS2/MinGW). No `python3` alias
  is needed: the gated LeVeri check passes the running interpreter to make.
- The unit suite must be green before any conversion work; it needs no Vitis,
  no network, and no `anthropic` package.
- The Claude CLI on this machine is the default LLM backend (`--use-llm`).

## 3. The work queue

Work in order; after each kernel, commit the updated results (see §4).

1. **dfmul, full ladder** — the design already passes host + LeVeri; this run
   adds the three Vitis phases:

   ```bat
   python -m c2hlsc_agent.cli convert --config examples\chstone_dfmul\config.yaml ^
     --out build\chstone_dfmul --use-llm --auto-repair --max-iterations 3 --run-vitis
   ```

2. **dfadd, then dfdiv** — same command with the respective config. These have
   never run: expect the possibility of generation-time or Vitis-stage
   failures; that is what the K=3 repair loop is for. Let it work. If a run
   ends FAILED/EXHAUSTED, diagnose from `conversion_report.md`, the phase
   logs, and `repair_audit.json` before re-running with `--new-run`.
3. **Optional, per passing kernel:** post-equivalence QoR —
   `python -m c2hlsc_agent.cli optimize --project build\chstone_<k> --objective latency`.
   Only on designs that pass the full ladder; the winner re-runs the whole
   ladder before acceptance.

Notes for these runs:

- Default per-phase timeouts: CSim 600 s, CSynth 1200 s, CoSim 600 s; the
  soft-float kernels are small and should fit comfortably.
- The run controller persists budgets in `run_ledger.jsonl` per project
  (8 LLM calls, 8 Vitis runs, 4 h wall). `--new-run` starts a fresh budget
  after an intentional reset; never edit the ledger.
- An infra-classified failure (`vitis_hls not found`, license, timeout of the
  transport) means environment, not design — fix the environment; the agent
  will not mutate source over it.

## 4. Recording results (repo policy)

Per `COLLABORATOR_START_HERE.md`, result claims must state scope and carry
artifacts. For each kernel, record in `docs/chstone_results.md` (create it):
kernel, commit SHA of the run, per-phase status (host equivalence,
leveri_trace, csim, csynth, cosim), iterations used, repair count, and the
project's `conversion_report.json` phase block pasted or referenced. Do not
claim a pass without the fresh report. Commit results and any config changes
to this branch and push; run
`bash scripts/team_preflight.sh --check-project build/chstone_<k>` (or the
`.ps1` with `-CheckProject`) before committing to catch hand-edited generated
testbenches.

## 5. Guardrails (unchanged, binding)

- Never bypass the short-circuit ladder or reclassify a failure to claim a
  later-stage success; status vocabulary is `pass/fail/blocked/skipped`
  (+ controller `exhausted`).
- The golden `input.c` and generated oracle testbenches are never edited by
  hand and never shown to the model as repair targets; contract changes go
  through config + regeneration.
- Keep the package dependency-free; every behavior change ships with a test
  and the offline suite stays green.
- CoSim proves RTL↔HLS-C; the host golden-C stage plus the LeVeri trace pair
  carry the equivalence-to-original-C claim. Report both, conflate neither.

## 6. Kickoff prompt for this session

> Check out branch `claude/combined-generation-workflow`, read
> `docs/handoff_local_windows_run.md`, and execute its §3 work queue on this
> machine (Vitis HLS via `C2HLSC_VITIS_BIN`). Record results per §4 and push
> to the same branch.
