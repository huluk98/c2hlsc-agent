"""Shared C++ stimulus generation for every testbench tier.

Both the oracle testbench (:mod:`testgen`) and the HLS-LeVeri paired trace testbenches
(:mod:`leveri_testgen`) must drive the golden C and the generated HLS-C with **exactly the
same schedule** — that synchronized stimulus is what makes a trace divergence mean a
design bug rather than a testbench bug. Keeping the helper templates in one module is how
that stays true: there is one definition of the directed schedule, one random stream, and
one sentinel function, rendered into whichever tier asks for it.

Two things are configurable from :class:`~c2hlsc_agent.config.AgentConfig`:

``directed_tests``
    The ordered directed-pattern schedule. Slot *i* of the run uses pattern *i*; every
    later slot is pseudo-random. This used to be hardcoded at slots 0-3 while the config
    key was silently ignored — it is now the single source of the schedule.

``extra_vectors``
    Concrete input vectors discovered by coverage refinement (KLEE counterexamples, see
    :mod:`coverage_refine`). They run **before** the directed schedule as additional
    leading cases. When the list is empty, the rendered C++ is byte-identical to the
    pre-refinement form, so an ordinary conversion is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .analyze import FunctionArg


#: Directed patterns understood by ``directed_tests``, in no particular order. The value
#: is the C++ expression for one element, given ``T``, ``element_idx`` and ``is_unsigned``.
#: Anything not listed here is rejected at generation time rather than silently ignored.
DIRECTED_PATTERNS: dict[str, str] = {
    "zeros": "static_cast<T>(0)",
    "ones": "static_cast<T>(~static_cast<unsigned long long>(0))",
    "minmax": (
        "is_unsigned ? std::numeric_limits<T>::max()\n"
        "                       : (element_idx % 2 ? std::numeric_limits<T>::max() "
        ": std::numeric_limits<T>::min())"
    ),
    "alternating": "static_cast<T>(element_idx % 2 ? 0xAAAAAAAAULL : 0x55555555ULL)",
    "random": None,  # explicit "leave this slot pseudo-random"
}

#: ``minmax`` only means something for integer types; a float slot falls through to random.
_INTEGER_ONLY = {"minmax"}

#: Bounded scalars get their own corner schedule — low, high, midpoint, then one — capped
#: at the length of the directed schedule so that ``directed_tests: []`` really means
#: "no directed cases anywhere".
_SCALAR_CORNERS = (
    "value = lo;",
    "value = hi;",
    "value = lo + ((hi - lo) / 2);",
    "if (lo <= 1 && hi >= 1) value = 1; else value = lo;",
)


class StimulusError(ValueError):
    """A directed pattern name is not one this generator knows how to emit."""


@dataclass(frozen=True)
class ExtraVector:
    """One concrete input assignment, replayed as a leading directed case.

    ``values`` maps argument name to a list of integers (array arguments, one entry per
    element, padded or truncated to the declared length) or to a single integer (scalars).
    ``origin`` records where it came from so the report can say why the case exists.
    """

    values: dict[str, Any]
    origin: str = "coverage_refinement"

    def to_dict(self) -> dict[str, Any]:
        return {"origin": self.origin, "values": self.values}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtraVector":
        return cls(values=dict(data.get("values", {})), origin=str(data.get("origin", "coverage_refinement")))


def validate_directed(names: Iterable[str]) -> list[str]:
    """Return the schedule as a clean list, rejecting unknown pattern names."""

    schedule = [str(name).strip().lower() for name in names if str(name).strip()]
    unknown = [name for name in schedule if name not in DIRECTED_PATTERNS]
    if unknown:
        supported = ", ".join(sorted(DIRECTED_PATTERNS))
        raise StimulusError(
            f"unknown directed_tests pattern(s): {', '.join(unknown)}; supported: {supported}"
        )
    return schedule


def directed_schedule(config: Any) -> list[str]:
    """The validated directed schedule for a config."""

    return validate_directed(getattr(config, "directed_tests", None) or [])


def extra_vectors(config: Any) -> list[ExtraVector]:
    raw = getattr(config, "extra_vectors", None) or []
    return [item if isinstance(item, ExtraVector) else ExtraVector.from_dict(dict(item)) for item in raw]


def _patterned_body(schedule: list[str], index_var: str) -> str:
    lines: list[str] = []
    for slot, name in enumerate(schedule):
        expression = DIRECTED_PATTERNS[name]
        if expression is None:
            continue  # "random": nothing to emit, the fallthrough handles it
        guard = f"{index_var} == {slot}"
        if name in _INTEGER_ONLY:
            guard += " && std::numeric_limits<T>::is_integer"
        lines.append(f"  if ({guard}) {{  // {name}")
        lines.append(f"    return {expression};")
        lines.append("  }")
    lines.append("  return random_value<T>(rng);")
    return "\n".join(lines)


def _scalar_body(schedule: list[str], index_var: str) -> str:
    corners = _SCALAR_CORNERS[: len(schedule)]
    if not corners:
        return (
            "  const unsigned long long span = static_cast<unsigned long long>(hi - lo) + 1ULL;\n"
            "  value = lo + static_cast<long long>(rng() % span);"
        )
    lines: list[str] = []
    for slot, corner in enumerate(corners):
        keyword = "if" if slot == 0 else "} else if"
        lines.append(f"  {keyword} ({index_var} == {slot}) {{")
        lines.append(f"    {corner}")
    lines.append("  } else {")
    lines.append("    const unsigned long long span = static_cast<unsigned long long>(hi - lo) + 1ULL;")
    lines.append("    value = lo + static_cast<long long>(rng() % span);")
    lines.append("  }")
    return "\n".join(lines)


def render_helpers(config: Any, index_var: str) -> str:
    """Render the stimulus helper templates shared by every testbench tier.

    ``index_var`` is the name of the directed-slot counter in the caller's loop
    (``test_idx`` in the oracle testbench, ``cycle`` in the trace testbenches).
    """

    schedule = directed_schedule(config)
    return f"""template <typename T>
T random_value(std::mt19937_64& rng) {{
  if (std::numeric_limits<T>::is_integer) {{
    return static_cast<T>(rng());
  }}
  return static_cast<T>((rng() % 20001) - 10000) / static_cast<T>(100);
}}

// Directed schedule from config.directed_tests: {', '.join(schedule) or '(none — all random)'}
template <typename T>
T bounded_scalar(int {index_var}, std::mt19937_64& rng, long long lo, long long hi) {{
  if (hi < lo) return static_cast<T>(lo);
  long long value = lo;
{_scalar_body(schedule, index_var)}
  return static_cast<T>(value);
}}

template <typename T>
T patterned_value(int {index_var}, int element_idx, std::mt19937_64& rng, bool is_unsigned) {{
{_patterned_body(schedule, index_var)}
}}

template <typename T>
T output_sentinel(int {index_var}, int element_idx) {{
  unsigned long long value = 0x9E3779B97F4A7C15ULL;
  value ^= static_cast<unsigned long long>({index_var} + 1) * 0xBF58476D1CE4E5B9ULL;
  value ^= static_cast<unsigned long long>(element_idx + 1) * 0x94D049BB133111EBULL;
  return static_cast<T>(value);
}}
"""


# --------------------------------------------------------------------------- #
# Coverage-refinement vectors
# --------------------------------------------------------------------------- #


def _element_values(vector: ExtraVector, arg: FunctionArg) -> list[int]:
    raw = vector.values.get(arg.name)
    length = arg.length or 1
    if raw is None:
        return [0] * length
    if isinstance(raw, (list, tuple)):
        values = [int(item) for item in raw][:length]
        return values + [0] * (length - len(values))
    return [int(raw)] * length


def _scalar_value(vector: ExtraVector, arg: FunctionArg) -> int:
    raw = vector.values.get(arg.name)
    if raw is None:
        return 0
    if isinstance(raw, (list, tuple)):
        return int(raw[0]) if raw else 0
    return int(raw)


def render_extra_tables(args: list[FunctionArg], vectors: list[ExtraVector]) -> str:
    """File-scope constant tables holding the refinement vectors, or ``""`` when none."""

    if not vectors:
        return ""
    lines = [
        f"// {len(vectors)} coverage-refinement vector(s) replayed before the directed schedule.",
        f"static const int c2hlsc_extra_count = {len(vectors)};",
    ]
    for index, vector in enumerate(vectors):
        lines.append(f"// vector {index}: {vector.origin}")
    for arg in args:
        if arg.is_pointer_like and arg.direction == "output":
            continue  # outputs are sentinel-filled, never driven from a vector
        if arg.is_pointer_like:
            rows = []
            for vector in vectors:
                values = ", ".join(str(value) for value in _element_values(vector, arg))
                rows.append(f"{{{values}}}")
            lines.append(
                f"static const long long c2hlsc_extra_{arg.name}[{len(vectors)}][{arg.length or 1}] = "
                f"{{{', '.join(rows)}}};"
            )
        else:
            values = ", ".join(str(_scalar_value(vector, arg)) for vector in vectors)
            lines.append(f"static const long long c2hlsc_extra_{arg.name}[{len(vectors)}] = {{{values}}};")
    return "\n".join(lines) + "\n"


def total_iterations(config: Any) -> int:
    """Directed + random tests, plus any refinement vectors replayed ahead of them."""

    return int(getattr(config, "num_tests", 0)) + len(extra_vectors(config))


def directed_index_decl(config: Any, index_var: str) -> str:
    """The line that maps the loop counter onto the directed schedule.

    With no refinement vectors this is the identity, and the emitted C++ is unchanged from
    the pre-refinement generator.
    """

    if not extra_vectors(config):
        return ""
    return f"    const int directed_idx = {index_var} - c2hlsc_extra_count;"


def directed_var(config: Any, index_var: str) -> str:
    return "directed_idx" if extra_vectors(config) else index_var


def extra_guard(config: Any, index_var: str) -> str:
    """The ``if`` condition selecting a refinement vector, or ``""`` when there are none."""

    if not extra_vectors(config):
        return ""
    return f"{index_var} < c2hlsc_extra_count"
