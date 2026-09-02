"""LLM client and prompt/parse helpers for the AUTO RTL generator and repair agents.

The model is a *pluggable backend*, so the agent never depends on one specific cloud
API. Four backends are supported:

- ``none``       -- no model; the agents run the conservative deterministic paths.
- ``claude-cli`` -- the local Claude Code CLI (``claude -p``). Subscription auth, no
  API key, no extra dependency. This is the preferred default when the ``claude``
  binary is on PATH.
- ``openai``     -- any OpenAI Chat Completions-compatible endpoint, using only the
  standard library (no extra dependency). This is how a **local** model runs with no
  cloud key: point ``llm_base_url`` at Ollama / LM Studio / llama.cpp / vLLM (e.g.
  ``http://localhost:11434/v1``). The same backend also reaches OpenAI-compatible cloud
  providers.
- ``anthropic``  -- the Anthropic Messages API (lazily imported ``anthropic`` SDK).

Everything stays deterministic and offline by default: if ``--use-llm`` is not requested,
or no backend resolves, the agents fall back to the conservative mechanical paths. The
LLM only ever *proposes* candidate HLS-C; the verifier ladder (host equivalence -> CSim
-> CSynth -> CoSim) remains the gate, and the original C file is never handed to the model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Protocol

from .analyze import AnalysisResult
from .hlsc_generator import HLSC_GENERATOR_SYSTEM_PROMPT, render_hlsc_generator_task

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_LLM_MODEL = DEFAULT_ANTHROPIC_MODEL  # backward-compatible alias
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_CLI_MODEL = "opus"
_DEFAULT_MAX_TOKENS = 8000
_HTTP_TIMEOUT = 600  # local models can be slow
_CLI_TIMEOUT = 900  # claude CLI runs with thinking; give it room


class LLMClient(Protocol):
    """Minimal text-completion contract used by the generator and repair agents."""

    model: str

    def complete(self, system: str, user: str, *, max_tokens: int = _DEFAULT_MAX_TOKENS) -> str:  # pragma: no cover - protocol
        ...


class AnthropicLLMClient:
    """Thin wrapper over the Anthropic Messages API.

    Uses adaptive thinking + ``high`` effort for code generation, and transparently
    retries without those parameters if an older SDK or model rejects them.
    """

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL, api_key: str | None = None) -> None:
        import anthropic  # lazy: keeps the package optional

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model

    def complete(self, system: str, user: str, *, max_tokens: int = _DEFAULT_MAX_TOKENS) -> str:
        base = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        bad_request = getattr(self._anthropic, "BadRequestError", None)
        retry_errors: tuple[type[BaseException], ...] = (
            (TypeError,) if bad_request is None else (TypeError, bad_request)
        )
        try:
            response = self._client.messages.create(
                **base,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
            )
        except retry_errors:
            # Older SDK (unknown kwargs -> TypeError) or a model that rejects the
            # adaptive-thinking / effort surface (-> BadRequestError). Retry plain.
            response = self._client.messages.create(**base)
        return _text_from_response(response)


class OpenAICompatibleLLMClient:
    """OpenAI Chat Completions-compatible client (local servers or cloud).

    Works with Ollama, LM Studio, llama.cpp's server, vLLM, and OpenAI-compatible cloud
    endpoints. Uses only the standard library, so no extra dependency is required, and a
    local server typically needs no API key.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: int = _HTTP_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout

    def complete(self, system: str, user: str, *, max_tokens: int = _DEFAULT_MAX_TOKENS) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return _openai_text(body)


class ClaudeCLIClient:
    """Drive the local Claude Code CLI (``claude -p``) as a completion backend.

    Uses subscription auth (whatever ``claude`` is logged in as), so no API key is
    required and calls are not billed per token. ``cli_cmd`` may be a multi-word command
    (e.g. ``"ssh you@mac claude"``) so the CLI can live on another machine, though the
    intended setup keeps every LLM call local and ships only Vitis over SSH.
    """

    def __init__(
        self,
        model: str = DEFAULT_CLI_MODEL,
        cli_cmd: str = "claude",
        timeout: int = _CLI_TIMEOUT,
    ) -> None:
        import shlex

        self._base = shlex.split(cli_cmd) + ["-p", "--model", model]
        self.model = model
        self._timeout = timeout

    def complete(self, system: str, user: str, *, max_tokens: int = _DEFAULT_MAX_TOKENS) -> str:
        del max_tokens  # the CLI manages its own output budget
        proc = subprocess.run(
            self._base,
            input=f"{system}\n\n{user}",
            text=True,
            capture_output=True,
            timeout=self._timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed (rc={proc.returncode}): {proc.stderr[-800:]}")
        return proc.stdout


def _text_from_response(response: object) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(part for part in parts if part)


def _openai_text(body: dict) -> str:
    choices = body.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some servers return structured content parts
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _anthropic_installed() -> bool:
    try:
        import anthropic  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def _is_local_url(base_url: str) -> bool:
    lowered = (base_url or "").lower()
    return any(host in lowered for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"))


def _cli_argv0(config: object) -> str | None:
    """First token of ``llm_cli_cmd`` (shlex-parsed, matching ClaudeCLIClient), or None.

    Uses shlex.split so a quoted space-containing executable path resolves the same way
    the client will actually invoke it; returns None for an empty/whitespace-only value.
    """

    import shlex

    try:
        tokens = shlex.split(getattr(config, "llm_cli_cmd", None) or "claude")
    except ValueError:
        return None
    return tokens[0] if tokens else None


def _cli_available(config: object) -> bool:
    argv0 = _cli_argv0(config)
    return bool(argv0) and shutil.which(argv0) is not None


def _explicit_base_url(config: object) -> bool:
    return bool(getattr(config, "llm_base_url", None) or _env("C2HLSC_LLM_BASE_URL", "OPENAI_BASE_URL"))


def resolve_backend(config: object) -> str:
    """Resolve the concrete backend: ``'none'``, ``'claude-cli'``, ``'anthropic'`` or ``'openai'``.

    Honours an explicit ``llm_backend``; otherwise ``auto`` respects an explicitly
    configured OpenAI-compatible endpoint first (the user pointed us at a specific model),
    then prefers the local Claude Code CLI (subscription auth, no per-token billing), then
    Anthropic, then OpenAI cloud.
    """

    requested = (getattr(config, "llm_backend", "auto") or "auto").lower()
    if requested in {"claude-cli", "anthropic", "openai", "none"}:
        return requested
    if _explicit_base_url(config):
        return "openai"  # an explicitly configured endpoint is an explicit choice; honour it
    if _cli_available(config):
        return "claude-cli"
    if _anthropic_installed() and _env("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    if _env("OPENAI_API_KEY"):
        return "openai"
    return "none"


def _openai_base_url(config: object) -> str:
    return (
        getattr(config, "llm_base_url", None)
        or _env("C2HLSC_LLM_BASE_URL", "OPENAI_BASE_URL")
        or DEFAULT_OPENAI_BASE_URL
    )


def missing_llm_reason(config: object) -> str | None:
    """Return a human-readable reason the LLM path is unavailable, or ``None``."""

    if not getattr(config, "use_llm", False):
        return "LLM not requested (pass --use-llm)"
    backend = resolve_backend(config)
    if backend == "none":
        return (
            "no LLM backend resolved: install the Claude Code CLI ('claude' on PATH, "
            "subscription auth), point --llm-backend openai at a local model "
            "(e.g. --llm-base-url http://localhost:11434/v1 for Ollama), or install "
            "'anthropic' and set ANTHROPIC_API_KEY"
        )
    if backend == "claude-cli" and not _cli_available(config):
        cli_cmd = _cli_argv0(config) or (getattr(config, "llm_cli_cmd", None) or "claude")
        return f"the Claude Code CLI {cli_cmd!r} is not on PATH"
    if backend == "anthropic":
        if not _anthropic_installed():
            return "the 'anthropic' package is not installed (pip install 'c2hlsc-agent[llm]')"
        if not _env("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            return "ANTHROPIC_API_KEY is not set"
    if backend == "openai":
        base_url = _openai_base_url(config)
        if not _is_local_url(base_url) and not _env("C2HLSC_LLM_API_KEY", "OPENAI_API_KEY"):
            return f"no API key for the OpenAI-compatible endpoint {base_url} (set OPENAI_API_KEY, or use a local --llm-base-url)"
    return None


def build_llm_client(config: object) -> LLMClient | None:
    """Construct the configured LLM backend client, or ``None`` when unavailable.

    Returns ``None`` (deterministic fallback) unless ``config.use_llm`` is set and a
    backend resolves. Constructing a client never makes a network call -- only
    :meth:`complete` does -- so resolution stays cheap and side-effect free.
    """

    if not getattr(config, "use_llm", False):
        return None
    backend = resolve_backend(config)
    if backend == "claude-cli":
        if not _cli_available(config):
            return None
        model = getattr(config, "llm_model", None) or DEFAULT_CLI_MODEL
        cli_cmd = getattr(config, "llm_cli_cmd", None) or "claude"
        return ClaudeCLIClient(model=model, cli_cmd=cli_cmd)
    if backend == "anthropic":
        if not _anthropic_installed() or not _env("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            return None
        model = getattr(config, "llm_model", None) or DEFAULT_ANTHROPIC_MODEL
        return AnthropicLLMClient(model=model)
    if backend == "openai":
        base_url = _openai_base_url(config)
        api_key = _env("C2HLSC_LLM_API_KEY", "OPENAI_API_KEY")
        # A non-local endpoint with no key is doomed to 401; return None so the caller
        # takes the deterministic fallback and surfaces missing_llm_reason (mirrors the
        # anthropic branch), instead of building a client that fails on first call.
        if not _is_local_url(base_url) and not api_key:
            return None
        model = getattr(config, "llm_model", None) or _env("C2HLSC_LLM_MODEL") or DEFAULT_OPENAI_MODEL
        return OpenAICompatibleLLMClient(base_url=base_url, model=model, api_key=api_key)
    return None


# --------------------------------------------------------------------------- #
# Prompt builders
# --------------------------------------------------------------------------- #


def _argument_lines(analysis: AnalysisResult) -> str:
    lines: list[str] = []
    for arg in analysis.function.args:
        if arg.is_pointer_like:
            shape = f"array length={arg.length}"
        else:
            shape = "scalar"
        extra = f" range={list(arg.scalar_range)}" if arg.scalar_range else ""
        lines.append(f"  - {arg.name}: type={arg.c_type} direction={arg.direction} {shape}{extra}")
    return "\n".join(lines) or "  - (no arguments)"


def _diagnostic_lines(analysis: AnalysisResult) -> str:
    items = getattr(analysis.diagnostics, "items", [])
    lines = [f"  - [{d.severity}] {d.code}: {d.message}" for d in items]
    return "\n".join(lines) or "  - none"


def _nl_spec_section(nl_spec: str | None) -> str:
    if not nl_spec:
        return ""
    return f"""
Design intent from the user (natural language). Honour it wherever it does not conflict
with functional equivalence to the original C:
\"\"\"
{nl_spec.strip()}
\"\"\"
"""


def build_generator_user_prompt(
    analysis: AnalysisResult,
    original_source: str,
    nl_spec: str | None = None,
) -> str:
    fn = analysis.function
    return f"""{render_hlsc_generator_task(original_source)}
{_nl_spec_section(nl_spec)}
Top function: `{fn.name}`  (signature: `{fn.signature}`)
Argument contract (preserve exactly):
{_argument_lines(analysis)}

Static analyzer notes:
{_diagnostic_lines(analysis)}

Hard requirements for AUTO RTL machine integration:
- Keep the EXACT top-function signature: `{fn.signature}`.
- Section 4 ("Vitis HLS annotated code") MUST contain a single complete, self-contained
  C++ translation unit: it must `#include "hls_top.hpp"` and define `{fn.name}` with that
  signature. Put it inside one ```cpp fenced block.
- Preserve functional equivalence with the original C. An automated golden-C testbench
  compares your output against the original under shared stimulus, then runs Vitis CSim,
  CSynth, and C/RTL CoSim. Only add pragmas that are equivalence-preserving.
- Do not change observable outputs, argument meanings, or declared array lengths.
"""


REPAIR_SYSTEM_PROMPT = """You are hlsc_repair_agent in an equivalence-first C-to-HLS-C verifier loop.

You receive ONE candidate source file that failed a specific verification stage, the
earliest-failure evidence, and the must-preserve top-function contract.

Rules:
- Produce the MINIMAL change that fixes the reported failure.
- Preserve functional equivalence with the original C and the exact top-function signature.
- Do not change observable outputs, argument meanings, declared array lengths, or the golden oracle.
- Keep the file synthesizable for AMD/Xilinx Vitis HLS; keep only equivalence-preserving pragmas.
- Return the COMPLETE corrected file in a single ```cpp fenced block, and nothing else of substance.
"""


def _history_section(history: list[object] | None) -> str:
    """Summarize prior repair attempts so the model does not repeat failed strategies."""

    if not history:
        return ""
    lines: list[str] = []
    for outcome in history[-3:]:
        stage = getattr(outcome, "stage", "?")
        family = getattr(outcome, "family", "?")
        status = getattr(outcome, "status", "?")
        summary = getattr(outcome, "summary", "")
        lines.append(f"- iteration {getattr(outcome, 'iteration', '?')} [{stage}/{family}] {status}: {summary}")
        for change in getattr(outcome, "changes", ()) or ():
            diff = getattr(change, "diff", "")
            diff_lines = [l for l in diff.splitlines() if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
            if diff_lines:
                excerpt = "\n".join(diff_lines[:30])
                lines.append(f"  changed {getattr(change, 'path', '?')}:\n```\n{excerpt}\n```")
    return f"""
Previous repair attempts on this candidate (the failure persisted afterwards).
Do NOT resubmit any of these changes or revert to a previously tried version;
try a genuinely different fix:
{chr(10).join(lines)}
"""


def build_repair_prompt(
    analysis: AnalysisResult,
    decision: object,
    phase: str,
    evidence: str,
    target_rel: str,
    current_text: str,
    history: list[object] | None = None,
    nl_spec: str | None = None,
) -> tuple[str, str]:
    fn = analysis.function
    # The caller passes distilled evidence (evidence_context.build_repair_evidence),
    # which already enforces the character budget; render it verbatim.
    excerpt = (evidence or "").strip() or "(no captured evidence)"
    user = f"""Failing stage: {phase}
Failure family: {getattr(decision, 'family', 'unknown')}
Repair intent: {getattr(decision, 'next_action', '')}
Repair scope: {getattr(decision, 'repair_scope', '')}
Must-preserve top-function signature: `{fn.signature}`
{_nl_spec_section(nl_spec)}{_history_section(history)}
Earliest-failure evidence (distilled, mismatches first):
```
{excerpt}
```

Current `{target_rel}` to repair:
```cpp
{current_text.rstrip()}
```

Return the full corrected `{target_rel}` in one ```cpp block. Change as little as possible."""
    return REPAIR_SYSTEM_PROMPT, user


QOR_OPTIMIZER_SYSTEM_PROMPT = """You are rtl_optimizer_agent in an equivalence-first C-to-HLS flow.

The design already PASSES the full verification ladder (host equivalence -> CSim ->
CSynth -> CoSim). Your job is post-equivalence QoR optimization ONLY: improve the given
objective (latency / area / balanced) using the Vitis synthesis report as evidence,
while preserving exact functional equivalence.

Rules:
- Keep the EXACT top-function signature; do not change observable outputs, argument
  meanings, or declared array lengths. The golden-C testbench re-verifies every candidate.
- Prefer pragma-level changes (PIPELINE, UNROLL, ARRAY_PARTITION, DATAFLOW, INLINE,
  DEPENDENCE, LATENCY, BIND_STORAGE/BIND_OP) tied to a specific loop/array and the
  specific bottleneck visible in the report. Small equivalence-preserving refactors
  (loop interchange, local buffering) are allowed when a pragma alone cannot help.
- Every pragma must be justified: add a short // comment on the line above each change
  explaining the expected effect on the objective.
- Respect the resource budget: do not exceed the available resources in the report, and
  do not trade a small latency win for an estimated clock that misses the target period.
- Do NOT repeat a strategy listed as already tried; propose a genuinely different point
  in the design space.
- Return the COMPLETE optimized translation unit in a single ```cpp fenced block (it must
  #include "hls_top.hpp" and define the top function), and nothing else of substance.
"""


def build_qor_prompt(
    analysis: AnalysisResult,
    current_source: str,
    metrics_text: str,
    objective: str,
    history: list[dict[str, object]] | None = None,
    nl_spec: str | None = None,
    attempt: int = 0,
    targets_text: str = "",
) -> tuple[str, str]:
    """Prompt for one QoR-optimization candidate, grounded in the csynth report."""

    fn = analysis.function
    history_lines: list[str] = []
    for item in history or []:
        history_lines.append(
            f"- candidate {item.get('index')}: {item.get('kind')} -> {item.get('status')}"
            + (f", score {item.get('score')}" if item.get("score") is not None else "")
            + (f" ({item.get('note')})" if item.get("note") else "")
        )
    history_text = (
        "\nAlready-tried candidates (do NOT resubmit these strategies):\n" + "\n".join(history_lines) + "\n"
        if history_lines
        else ""
    )
    if attempt:
        history_text += (
            f"\nThis is independent optimization attempt #{attempt + 1}: explore a different "
            "point in the design space than the attempts above.\n"
        )
    user = f"""Objective: minimize {objective}.
{targets_text}Must-preserve top-function signature: `{fn.signature}`
{_nl_spec_section(nl_spec)}
Current Vitis synthesis report (baseline for this attempt):
```
{metrics_text}
```
{history_text}
Current `src/hls_top.cpp` (functionally verified):
```cpp
{current_source.rstrip()}
```

Return ONE optimized complete `src/hls_top.cpp` in a single ```cpp block."""
    return QOR_OPTIMIZER_SYSTEM_PROMPT, user


NL_REFERENCE_SYSTEM_PROMPT = """You are the reference-model author in an equivalence-first C-to-HLS flow.

From a natural-language hardware/algorithm specification you write ONE plain, portable
C99 file that becomes the GOLDEN REFERENCE ORACLE: an automatically generated testbench
will compare an HLS implementation against it element-by-element, and Vitis CSim/CoSim
will re-run it. Correctness and analyzability matter more than performance.

Rules:
- Define exactly one externally visible function with the EXACT name the user gives; any
  helpers must be `static`.
- Plain C only: no dynamic memory, no recursion, no file or console I/O, no OS calls,
  no HLS pragmas, no ap_int/ap_fixed types.
- Every loop bound must be a compile-time constant. Bake array sizes into the code with a
  `#define` (e.g. `#define N 256`) and index only within `[0, N)`.
- DO NOT take a runtime element-count/length parameter (no `int n` that bounds a loop):
  the automated testbench passes a random value for such a scalar, which would read past a
  fixed-size buffer. If the spec mentions a count, encode it as a compile-time constant and
  make every array parameter exactly that fixed size.
- Keep the signature simple and synthesizable: scalars, fixed-size arrays via pointers,
  and a scalar (or void) return.
- State every assumption you make (especially the chosen array sizes) as a // comment at
  the top of the file.
- Return ONLY the complete C file in a single ```c fenced block.
"""


def build_nl_reference_prompt(nl_spec: str, top_name: str) -> tuple[str, str]:
    user = f"""Natural-language specification:
\"\"\"
{nl_spec.strip()}
\"\"\"

Write the golden C reference implementation. The externally visible function MUST be
named exactly `{top_name}`. Return one complete C file in a single ```c block."""
    return NL_REFERENCE_SYSTEM_PROMPT, user


def extract_reference_c(text: str, top_name: str) -> str | None:
    """Extract the golden reference C file from an NL-reference response."""

    code = extract_full_file(text, must_contain=f"{top_name}(")
    if not code or not is_plausible_translation_unit(code, top_name):
        return None
    return code


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #

# Fence-length aware: an N-backtick fence is closed only by the same N backticks, so a
# 4-backtick block wrapping inline triple-backtick examples is not truncated mid-body.
_FENCE = re.compile(r"(`{3,})[ \t]*([A-Za-z0-9_+\-]*)[ \t]*\r?\n(.*?)\r?\n?\1", re.S)
_CODE_LANGS = {"", "c", "cc", "cpp", "c++", "cxx", "h", "hpp", "hxx"}


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Return ``(language, body)`` for every fenced code block in ``text``."""

    return [(lang.lower(), body) for _ticks, lang, body in _FENCE.findall(text or "")]


def _defines_function(code: str, name: str) -> bool:
    pattern = re.compile(rf"\b{re.escape(name)}\s*\([^;{{}}]*\)\s*\{{", re.S)
    return bool(pattern.search(code))


def _braces_balanced(code: str) -> bool:
    return bool(code) and "}" in code and code.count("{") == code.count("}")


def _is_code_lang(lang: str) -> bool:
    return lang in _CODE_LANGS


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def is_plausible_translation_unit(code: str, top_name: str) -> bool:
    """Cheap structural gate: defines the top function and has balanced braces.

    Rejects truncated/prose output before it can be written, so the caller falls back
    instead of emitting a non-compiling file.
    """

    return bool(code) and _braces_balanced(code) and _defines_function(code, top_name)


def extract_hls_source(
    text: str,
    top_name: str,
    original_source: str,
    header_include: str = '#include "hls_top.hpp"',
) -> str | None:
    """Extract the synthesizable HLS-C translation unit from a generator response.

    Considers only C/C++-tagged fenced blocks that define ``top_name``. Prefers blocks
    after the "Vitis HLS annotated code" marker, and among the candidates chooses the last
    one that is not a verbatim echo of the original source (so a restated "Original code"
    block is skipped). The chosen unit must pass :func:`is_plausible_translation_unit`;
    otherwise ``None`` is returned and the caller falls back to the conservative copy.
    """

    def _candidates(blocks: list[tuple[str, str]]) -> list[str]:
        return [body for lang, body in blocks if _is_code_lang(lang) and _defines_function(body, top_name)]

    candidates: list[str] = []
    marker = re.search(r"vitis hls annotated code", text or "", re.I)
    if marker:
        candidates = _candidates(extract_code_blocks(text[marker.end():]))
    if not candidates:
        candidates = _candidates(extract_code_blocks(text))
    if not candidates:
        return None

    normalized_original = _normalize(original_source)
    chosen: str | None = None
    for body in candidates:
        if body.strip() and _normalize(body) != normalized_original:
            chosen = body  # last non-echo defining block (section 4 / aggressive option)
    if chosen is None:
        chosen = candidates[-1]

    if not is_plausible_translation_unit(chosen, top_name):
        return None

    chosen = chosen.rstrip() + "\n"
    if "hls_top.hpp" not in chosen:
        chosen = f"{header_include}\n\n{chosen}"
    return chosen


def extract_full_file(text: str, must_contain: str | None = None) -> str | None:
    """Extract a complete file body, preferring C/C++-tagged blocks.

    Filters to blocks whose language tag is a C/C++ family tag (or untagged) so a prose
    or log block cannot be selected, then narrows by ``must_contain`` and returns the
    longest remaining block. Returns ``None`` when nothing usable matches.
    """

    blocks = [(lang, body) for lang, body in extract_code_blocks(text) if body.strip()]
    pool = [(lang, body) for lang, body in blocks if _is_code_lang(lang)]
    if must_contain:
        filtered = [(lang, body) for lang, body in pool if must_contain in body]
        if filtered:
            pool = filtered
    if not pool:
        return None
    body = max((b for _lang, b in pool), key=len)
    return body.rstrip() + "\n"
