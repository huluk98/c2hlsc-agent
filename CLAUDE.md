# c2hlsc-agent

Equivalence-first C → Vitis HLS-C converter, repairer, and QoR optimizer. The pipeline is
deterministic end to end; an LLM only ever *proposes* candidate HLS-C, and the verifier
ladder decides. LLM calls shell out to the local Claude Code CLI (`claude -p`) through
`c2hlsc_agent/llm.py::ClaudeCLIClient` (subscription auth, no API key).

## Commands

- Test suite: `python3 -m unittest discover -s tests` — plain `unittest`, no
  `pytest.ini`/`conftest.py`. Don't introduce pytest-only fixtures or markers.
- Always invoke `python3`, never bare `python` (no `python` on this machine's PATH).
- CLI entry point: `c2hlsc-agent = "c2hlsc_agent.cli:main"` (`pyproject.toml`). Subcommands:
  `convert`, `repair`, `optimize`, `ppa`, `cross-reference`.
- CI (`.github/workflows/ci.yml`) deliberately does **not** install the `anthropic` package,
  so the offline / no-LLM deterministic fallback path stays exercised on every push. Any new
  test that assumes `anthropic` is importable needs an `ImportError`/skip guard.

## Architecture invariants — do not break these

- The verifier ladder (host equivalence → paired shift-left traces → gcov/KLEE evidence →
  CSim → CSynth → CoSim) controls acceptance. Host equivalence, paired traces, and CoSim are
  correctness gates; gcov is supporting evidence. KLEE can gate only on a structured,
  named golden-C↔HLS-C relational counterexample. A clean bounded KLEE run means no
  counterexample was found under the declared bounds/non-aliasing/no-hidden-state model,
  never universal equivalence. Relational reports are revision-bound by top/artifact hashes;
  do not accept legacy, stale, unscoped, vacuous, or free-form evidence. Never let LLM output
  bypass the ladder.
- The original golden `input.c` is **never** handed to the repair agent
  (`hlsc_repair_agent`). Repair sees only `src/hls_top.cpp` plus failure evidence.
- The sha256 oscillation guards must not be bypassed or weakened. There are two layers:
  `hlsc_repair_agent._llm_repair` (rejects a candidate whose hash matches any prior state,
  status `oscillation_rejected`) and `cli.run_convert`'s `_project_signature` loop guard.
- `extract_hls_source` / `extract_full_file` / `is_plausible_translation_unit` (`llm.py`) are
  structural safety gates, not incidental parsing — don't remove them to "simplify".
- `qor_optimizer.optimize_project`'s per-round attempt loop is **intentionally sequential**:
  `consider()` appends each scored candidate's pragma strategy to `history`, and the next
  attempt's prompt carries it as "Already-tried candidates (do NOT resubmit these
  strategies)". Parallelizing that loop blinds each attempt to its siblings and costs
  candidate diversity. Best-of-N generation in `convert.py` has no such chaining and *is*
  parallelized (`config.llm_candidate_workers`).

## LLM backend

Four pluggable backends in `llm.py`, chosen by `resolve_backend()`: `none` (deterministic
fallback), `claude-cli` (default when `claude` is on PATH), `openai` (any OpenAI-compatible
endpoint, incl. local Ollama/LM Studio), `anthropic` (direct API, optional dependency).

`ClaudeCLIClient` invokes the CLI leanly and in full isolation:
`-p --model <m> --output-format json --strict-mcp-config --no-session-persistence
--setting-sources "" --tools "" --system-prompt <system>`, with the user prompt on stdin.
`--setting-sources ""` matters: it keeps automated pipeline calls from loading this repo's
own `.claude/` skills/hooks (those are for interactive sessions, not for the thousands of
completion calls the pipeline makes). Responses are parsed from the JSON envelope's `result`
field; `is_error` or non-JSON stdout raises.

## Directories

- `build/` — gitignored but **not** throwaway scratch: it holds real toolchain output
  (Vitis/Bambu/yosys/OpenSTA artifacts) worth inspecting when debugging. Don't blanket-delete
  it during unrelated cleanup.
- `.vscode/` — deliberately git-excluded (hardcodes a local interpreter path). Never create
  or commit it.
- `examples/` — `bit_ops`, `cnn_3x3`, `simple_fir`, `vector_add`: the canonical smoke fixtures.
- `data/hls_nl/` — 10K-record NL corpus (JSONL) plus batch drivers under `scripts/`.
- `scripts/` — batch/corpus tooling. These are the only places with concurrency at the
  "many independent records" level (`--workers`).
- Generated projects contain `verification_knowledge_graph.json`, a deterministic graph of
  contracts, artifacts, verifier phases, evidence references, repairs, and reports. Keep it
  content-safe: no source, log, prompt, or evidence bodies.

## Reference docs

- `AGENT_SUMMARY.md` — the living per-function reference and the live-vs-declarative agent
  seams list. Prefer it over `docs/functional_equivalent_rtl_agent.md`, which is the original
  design blueprint and is now partially stale.
