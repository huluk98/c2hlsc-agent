# The memory slate

A record of what was decided and what was measured, built so that starting over
lands in the same place — or, where it doesn't, makes the divergence visible
instead of silent.

## Why this exists

Long-running work with an agent loses its reasoning between sessions. The code
survives; the *why* does not. Six weeks later nobody can say whether a corpus was
dropped because it was redundant or because its testbench asserted nothing, and
the question gets relitigated from scratch — often to a different answer.

Prose documentation does not solve this, because prose records intent at the
moment of writing and then rots quietly. This repository already carries a lot of
it, some describing behaviour that was never implemented. The slate is different
in one specific way: **every claim in it names how to check itself**, and a script
checks them all on demand.

## Layout

| Path | What it holds |
|---|---|
| `slate.yaml` | Decisions, evidence, and the open frontier |
| `artifacts/` | Committed measurement outputs that evidence entries point at |
| `../../scripts/replay_slate.py` | Re-runs every evidence entry |
| `../../scripts/run_hls_eval_sweep.py` | Produces the sweep numbers behind `E005`-`E008` |
| `../../CLAUDE.md` | Wiring: makes a fresh session read the slate before acting |

## Using it

```bash
python3 scripts/replay_slate.py --list      # decisions and open questions
python3 scripts/replay_slate.py             # re-verify every recorded claim
python3 scripts/replay_slate.py --id E003   # one entry
```

Exit status is 0 while the record holds and 1 when something drifted. Entries are
checked concurrently; pass `--workers 1` when you want a clean failure trace.

Re-measuring the sweep needs the HLS-Eval corpus, which is not vendored here:

```bash
git clone https://github.com/sharc-lab/hls-eval /tmp/hls-eval
python3 scripts/run_hls_eval_sweep.py --data-root /tmp/hls-eval/hls_eval_data \
  --out /tmp/sweep.json --workers 4          # add --raw for the un-preprocessed mode
```

Rows are sorted by design id before writing, so two runs of the same command
produce byte-identical output no matter what order the workers finished in. That
property is what lets a result file serve as evidence rather than just output.

## The three entry kinds

**Decisions** are closed by a human. An agent may recommend and must record the
recommendation as a recommendation; writing `decided_by: human` for a conclusion
the human never gave is the one failure mode that makes the whole slate
untrustworthy. Each decision carries what it *forecloses* — the paths it takes off
the table — because that is the part which otherwise gets quietly reopened.

**Evidence** is a claim that was verified, plus the means to verify it again:
a test id, a committed artifact, or a command with an expected result. Anything
that cannot be re-run does not belong here. In practice the best evidence entry
points at a regression test, because then the record is enforced by CI rather
than by anyone remembering to replay it.

**Frontier** entries are questions still open, each with the context needed to
answer it without re-deriving the situation. An empty frontier means the design
tree has been walked to its leaves; it is rarely empty for long.

## Rules that keep it worth reading

- **Append, never rewrite.** A reversed decision becomes a new entry naming the
  old one in `supersedes`. The path taken is itself information — knowing a
  question was answered one way and later reversed is worth more than seeing only
  the final answer.
- **Pin claims to commits.** `fixed_in` and `measured_at` are what let a later
  reader tell a stale record from a real regression.
- **Record caveats with results.** `E005` carries the scope of its own number —
  conservative path, host-side only, no Vitis. A measurement quoted without its
  limits eventually gets quoted as something it never was.

## What this is not

It is not the repair knowledge base. `agent_loop.py` names retrieved repair cards
as a repair input and `audit_memory_agent` claims to promote audited successes;
neither is implemented, and their design is open question `Q004`. That layer is
per-repair evidence keyed by failure family, written by the agent, at machine
scale. This layer is project decisions, closed by a human, at human scale. They
share a motivation and nothing else — keeping them in one store would mean either
letting an agent close design decisions or making every repair card wait on human
review.
