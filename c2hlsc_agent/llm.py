"""LLM client and prompt/parse helpers for the AUTO RTL generator and repair agents.

The model is a *pluggable backend*, so the agent never depends on one specific cloud
API. Three backends are supported:

- ``none``      -- no model; the agents run the conservative deterministic paths.
- ``openai``    -- any OpenAI Chat Completions-compatible endpoint, using only the
  standard library (no extra dependency). This is how a **local** model runs with no
  cloud key: point ``llm_base_url`` at Ollama / LM Studio / llama.cpp / vLLM (e.g.
  ``http://localhost:11434/v1``). The same backend also reaches OpenAI-compatible cloud
  providers.
- ``anthropic`` -- the Anthropic Messages API (lazily imported ``anthropic`` SDK).

Everything stays deterministic and offline by default: if ``--use-llm`` is not requested,
or no backend resolves, the agents fall back to the conservative mechanical paths. The
LLM only ever *proposes* candidate HLS-C; the verifier ladder (host equivalence -> CSim
-> CSynth -> CoSim) remains the gate, and the original C file is never handed to the model.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Protocol

from .analyze import AnalysisResult
from .hlsc_generator import HLSC_GENERATOR_SYSTEM_PROMPT, render_hlsc_generator_task

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_LLM_MODEL = DEFAULT_ANTHROPIC_MODEL  # backward-compatible alias
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_MAX_TOKENS = 8000
_EVIDENCE_LIMIT = 1600
_HTTP_TIMEOUT = 600  # local models can be slow


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


def resolve_backend(config: object) -> str:
    """Resolve the concrete backend name: ``'none'``, ``'anthropic'`` or ``'openai'``.

    Honours an explicit ``llm_backend``; otherwise ``auto`` prefers a configured
    OpenAI-compatible endpoint (covers local models), then Anthropic, then OpenAI cloud.
    """

    requested = (getattr(config, "llm_backend", "auto") or "auto").lower()
    if requested in {"anthropic", "openai", "none"}:
        return requested
    if getattr(config, "llm_base_url", None) or _env("C2HLSC_LLM_BASE_URL", "OPENAI_BASE_URL"):
        return "openai"
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
            "no LLM backend resolved: point --llm-backend openai at a local model "
            "(e.g. --llm-base-url http://localhost:11434/v1 for Ollama), or install "
            "'anthropic' and set ANTHROPIC_API_KEY"
        )
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
    if backend == "anthropic":
        if not _anthropic_installed() or not _env("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            return None
        model = getattr(config, "llm_model", None) or DEFAULT_ANTHROPIC_MODEL
        return AnthropicLLMClient(model=model)
    if backend == "openai":
        base_url = _openai_base_url(config)
        model = getattr(config, "llm_model", None) or _env("C2HLSC_LLM_MODEL") or DEFAULT_OPENAI_MODEL
        api_key = _env("C2HLSC_LLM_API_KEY", "OPENAI_API_KEY")
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


def build_generator_user_prompt(analysis: AnalysisResult, original_source: str) -> str:
    fn = analysis.function
    return f"""{render_hlsc_generator_task(original_source)}

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


def build_repair_prompt(
    analysis: AnalysisResult,
    decision: object,
    phase: str,
    evidence: str,
    target_rel: str,
    current_text: str,
) -> tuple[str, str]:
    fn = analysis.function
    excerpt = (evidence or "").strip()[:_EVIDENCE_LIMIT] or "(no captured evidence)"
    user = f"""Failing stage: {phase}
Failure family: {getattr(decision, 'family', 'unknown')}
Repair intent: {getattr(decision, 'next_action', '')}
Repair scope: {getattr(decision, 'repair_scope', '')}
Must-preserve top-function signature: `{fn.signature}`

Earliest-failure evidence (truncated):
```
{excerpt}
```

Current `{target_rel}` to repair:
```cpp
{current_text.rstrip()}
```

Return the full corrected `{target_rel}` in one ```cpp block. Change as little as possible."""
    return REPAIR_SYSTEM_PROMPT, user


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
