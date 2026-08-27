# Agent dogfood fixtures

Hand-written fixtures for exercising the live model-backed agents end to end
(evidence from real runs: `docs/agent_dogfood_evidence.md`).

- `accum.c` / `accum.yaml` — fully-specified contract (lengths, `n` ranged `[0, 4]`).
  Used by `--tb-augment` runs and by `scripts/dogfood_live_agents.py`.
- `unbounded.c` / `unbounded.yaml` — deliberately NO argument metadata: the analyzer
  defaults the pointer bounds and the `count` scalar is unranged. This is the target
  case for `--propose-contract`, and the regression case for the unranged length-like
  scalar stimulus clamp.

```bash
python -m c2hlsc_agent.cli convert --config examples/agent_dogfood/accum.yaml \
  --out build/aug --use-llm --llm-backend claude-cli --llm-model haiku --tb-augment -v

python -m c2hlsc_agent.cli convert --config examples/agent_dogfood/unbounded.yaml \
  --out build/contract --use-llm --llm-backend claude-cli --llm-model haiku --propose-contract -v

python3 scripts/dogfood_live_agents.py   # analyst + repair + memory, two rounds

```
