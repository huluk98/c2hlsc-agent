# Working in this repository

Read `AGENTS.md` for the product invariants. They are not negotiable and this file
does not restate them.

## Read the memory slate first

`docs/memory/slate.yaml` is the settled record: decisions a human has closed,
claims that were actually verified, and the questions still open. **It is the
authority when the prose docs disagree with each other or with the code.** The
repository carries a lot of narrative documentation written at different times;
some of it describes intent that was never implemented. The slate does not.

Before proposing a direction, check whether it is already settled or already
foreclosed:

```bash
python3 scripts/replay_slate.py --list   # decisions and open questions
python3 scripts/replay_slate.py          # re-verify every recorded claim
```

A `DRIFT` result means the code and the record disagree. That is a finding, not a
nuisance: work out which one is now wrong before doing anything else.

## Adding to the slate

Append, never rewrite — the path taken is part of the record.

- A **decision** is closed by a human, never by an agent. Recommending is fine;
  recording your own recommendation as `decided_by: human` is not.
- **Evidence** requires a means of re-verification: a test id, a committed
  artifact, or a command with an expected result. A claim that cannot be re-run
  does not belong in the slate. Prefer adding a regression test and pointing the
  entry at it.
- When a decision is reversed, add a new one that names the old in `supersedes`.
  Do not edit the original.

## Two memory layers, deliberately separate

The slate holds *project* decisions — what this work is for, what was ruled out.
It is not the repair knowledge base that `agent_loop.py` and `audit_memory_agent`
refer to; that is per-repair evidence, keyed by failure family, and its design is
open question `Q004`. Keep them apart: a project decision and a repair card have
different lifetimes, different authors, and different standards of proof.

## Verification

The offline path must keep working without an API key or network:

```bash
python3 -m pytest tests/ -q
python3 -m c2hlsc_agent.cli convert --config examples/vector_add/config.yaml \
  --input examples/vector_add/input.c --out /tmp/va --no-llm --no-run-vitis --new-run
```

Vitis is opt-in and absent from most environments. Host compilation is not CSim,
and a passing host run is not synthesis evidence — `AGENTS.md` is explicit about
this and the reports must stay honest about which phases actually ran.
