# Claude <-> Codex relay protocol

Purpose: get **pass@1 and pass@5** finished across the RTLLM arms when neither agent can
run to completion in one sitting, because the Claude CLI backend saturates long before the
work does. The relay is a file protocol, not a conversation: either agent can act on it
without the other being live.

## The one fact that governs the plan

`pass@k` (Chen et al. 2021, `1 - C(n-c,k)/C(n,k)`) is **defined only for k <= n**.

- The six existing arms are `--samples 2`. They yield **pass@1 and pass@2 only**.
- **pass@5 requires a run at `--samples 5`.** It cannot be derived from the n=2 arms by any
  arithmetic, and resuming an n=2 directory at `--samples 5` merges both into one basis
  reported under the last invocation's settings -- the case `check_resume_compatible`
  refuses. **pass@5 runs go to a NEW `--out-dir`.**

So the work is two separate things, and the relay tracks them separately:

| goal | needs | where |
| --- | --- | --- |
| pass@1 ablation across arms | the six n=2 arms complete and uncontaminated | `rtllm_*/` |
| pass@5 on the headline arm | a fresh `--samples 5` run | `rtllm_baseline_n5/` |

## Baton file

`runs/paper_20260831/RELAY.json`. Whoever is running owns the baton; whoever is idle polls
it. Schema:

```json
{
  "holder": "claude" | "codex" | "none",
  "state": "running" | "limit_hit" | "handoff" | "done",
  "since": "ISO-8601 UTC",
  "next_command": "the exact command the next agent should run",
  "reason": "why the baton moved",
  "verified_alive": "how the holder confirmed its own processes were running"
}
```

**A `holder`/`running` claim is worthless unless the processes are verified alive.** This
protocol exists partly because the register once claimed a RUNNING sweep for hours after it
had died, and the other agent stood off a lane nobody was driving. Before trusting
`state: running`, run:

```
tasklist | grep -c python.exe        # or: (Get-Process python -EA SilentlyContinue).Count
```

Zero processes plus `state: running` means the holder died. Take the baton.

## When Claude hits its limit

Claude writes `RELAY.json` with `state: "limit_hit"`, `holder: "none"`, and the exact
resume command, then stops. Codex picks it up by polling. Every runner is `--resume`-safe:
backend-error rows are backed up to `results.jsonl.backend-errors.*.bak`, dropped, and
regenerated, so a handoff mid-arm loses no completed design.

Codex should re-invoke Claude once the limit resets rather than finishing the sweep itself,
**only** if the sweep still needs Claude's backend. If Codex can drive the same runner, it
should just run it -- the arms are agent-agnostic as long as the generation contract holds.

## Non-negotiable: the generation contract

Before and after any sweep, both agents run:

```
python scripts/check_generation_parity.py runs/paper_20260831
```

Exit 1 means two arms were generated under different invariant settings (`model`,
`backend`, `benchmark`, `apply_shims`, `sim_timeout`, `compile_timeout`, `llm_retries`) and
are not comparable; no downstream analysis recovers that. Regenerate the odd arm into a
fresh `--out-dir`.

A pass@5 arm at n=5 sitting beside n=2 arms is allowed, but a **cross-arm** pass@k
comparison is then valid only at `k <= min(n)`. Report the n=5 arm's pass@5 as a
single-arm depth result, not as an ablation delta.

## Concurrency ceiling

The Claude CLI backend saturates around **12 concurrent processes**; that produced 36
`llm_error` rows per arm on the first attempt. **Keep total concurrency at or below 6
across BOTH agents.** Two agents each running "just three workers" is six, which is the
whole budget -- coordinate through the baton rather than assuming the other is idle.

## Durability, learned the hard way

Sweeps launched as agent-session background jobs die when that session's job wrapper goes
away. Three launches were lost this way. A PowerShell `Start-Process` detach did not survive
either. If a run must outlive the agent, prefer a scheduled task or a console the agent does
not own, and **always verify liveness afterwards** rather than trusting the launch.
