# Live-agent dogfood evidence

*Captured from real runs against the `claude` CLI (model `haiku`) in the session that
landed commits `10570f2` and `b135842`. These are curated result artifacts kept as
documentation evidence, not generated build output; regenerate with the commands shown.*

Every scenario is reproducible on any machine with the repo, `g++`, `make`, `python3`,
and a logged-in `claude` CLI. The two config fixtures are the `accum` design (arrays of
length 4, `n` ranged `[0, 4]`) and the deliberately unbounded `scale` design (no
argument metadata at all).

---

## Scenario 1 — `--tb-augment` (shift_left_testbench_agent)

```bash
python -m c2hlsc_agent.cli convert --config accum.yaml --out build/aug \
  --use-llm --llm-backend claude-cli --llm-model haiku --tb-augment --verbose
```

Result: exit 0 — `c2hlsc_agent: all 14 tests passed (8 llm-directed), seed=1`.
The model proposed 8 vectors; all 8 passed contract validation, 0 rejected; the
constant tables in `tb/testbench.cpp` match the JSON exactly, and a manual
`make test` rebuild reproduces the pass.

`tb/augmented_vectors.json` (verbatim):

```json
{
  "policy_id": "shift_left_stimulus_augment_v1",
  "model": "haiku",
  "accepted": [
    {
      "in": [
        0,
        0,
        0,
        0
      ],
      "n": 0
    },
    {
      "in": [
        5,
        0,
        0,
        0
      ],
      "n": 1
    },
    {
      "in": [
        10,
        20,
        0,
        0
      ],
      "n": 2
    },
    {
      "in": [
        7,
        8,
        9,
        0
      ],
      "n": 3
    },
    {
      "in": [
        1,
        2,
        3,
        4
      ],
      "n": 4
    },
    {
      "in": [
        -1,
        -2,
        -3,
        -4
      ],
      "n": 4
    },
    {
      "in": [
        1000000,
        500000,
        250000,
        125000
      ],
      "n": 4
    },
    {
      "in": [
        -2147483648,
        0,
        0,
        0
      ],
      "n": 1
    }
  ],
  "rejected": [],
  "note": "Accepted vectors are appended to tb/testbench.cpp after the deterministic tests as constant tables; both the golden oracle and the HLS top receive the same values."
}
```

## Scenario 2 — `--propose-contract` (contract_planner)

```bash
python -m c2hlsc_agent.cli convert --config unbounded.yaml --out build/contract \
  --use-llm --llm-backend claude-cli --llm-model haiku --propose-contract --verbose
```

The **first** run of this scenario (before `b135842`) failed usefully twice over: the
unranged `int count` scalar segfaulted the golden reference, and the model's symbolic
length `"count"` was rejected whole-entry with nothing recorded. After the fixes the
same command exits 0 (`all 4 tests passed`), the testbench contract carries
`count: scalar UNRANGED but length-like; stimulus clamped to [0, 16]`, and the model
proposes exactly the sound refinement — a `range` on `count`:

`contract_proposals.json` (verbatim):

```json
{
  "policy_id": "contract_planner_proposals_v1",
  "applied": false,
  "note": "Proposals are advisory. Copy the ones you agree with into the config as arguments.<name>.direction/length/range, then rerun convert.",
  "model": "haiku",
  "error": null,
  "proposals": [
    {
      "argument": "src",
      "direction": "input",
      "rationale": "Loop accesses src[i] for i in [0, count). True bound is the 'count' parameter, not fixed. Defaulted length=16 is unjustified; constrain 'count' instead."
    },
    {
      "argument": "dst",
      "direction": "output",
      "rationale": "Loop writes dst[i] for i in [0, count). True bound is the 'count' parameter, not fixed. Defaulted length=16 is unjustified; constrain 'count' instead."
    },
    {
      "argument": "count",
      "direction": "input",
      "range": [
        0,
        16
      ],
      "rationale": "Loop guard 'i < count' makes count the array access bound for both src and dst. Range [0, 16] ensures both arrays stay in-bounds during testing."
    }
  ],
  "rejected": []
}
```

## Scenario 3 — failure_analyst + LLM repair + audit_memory

Two full repair rounds on a sabotaged design: round 1 repairs to pass with the analyst
consulted and promotes an audited card; round 2, a fresh project with the same bug,
must receive that card in its repair prompt. The **first** run failed the final check
and exposed the refined-family retrieval bug fixed in `b135842`; the rerun is 7/7:

```json
{
  "scenario": "analyst+repair+memory",
  "driver": "scripts-adjacent dogfood_analyst_memory.py (two full repair rounds against the real claude CLI, model haiku)",
  "checks": {
    "round1_repaired_to_pass": true,
    "analyst_was_consulted": true,
    "repair_prompt_sent": true,
    "round1_memory_empty_in_prompt": true,
    "cards_promoted": true,
    "round2_repaired_to_pass": true,
    "round2_prompt_carried_memory_card": true
  },
  "cards": [
    {
      "family": "host_behavior_mismatch",
      "stage": "software_equivalence",
      "kind": "llm"
    }
  ],
  "passed": true,
  "note": "second run after commit b135842; the first run failed round2_prompt_carried_memory_card, which exposed the refined-family retrieval bug"
}
```

## Scenario 4 — deterministic offline path untouched

- `python -m unittest discover -s tests` -> OK (261 at the time of capture)
- convert with **no** LLM flags: exit 0; no `contract_proposals.json`, no
  `tb/augmented_vectors.json`, no `aug_vec_` tables, no `llm-directed` in the pass
  line; no `~/.c2hlsc` created; `import c2hlsc_agent.cli` clean under `env -i`.

## Adversarial review tally

Three lenses (invariants, correctness, integration) over commit `10570f2`, every
finding independently refute-verified: **12 confirmed, 3 refuted**, all confirmed
findings fixed in `b135842` with a regression test each. Full detail in that commit's
message and `docs/SESSION_HANDOFF.md` section 4d.
