"""Natural-language -> RTL multi-agent loop for the RTLLM v2.0 benchmark.

This is the loop of ``docs/functional_equivalent_rtl_agent.md`` retargeted from
C -> HLS-C to natural-language -> Verilog. The same vocabulary applies, one rung
lower on the abstraction ladder:

- ``rtl_planner``       -- the contract planner. Turns ``design_description.txt`` into an
  explicit interface contract (module name, every port with width and direction, clock and
  reset polarity, sequential vs combinational, state/latency expectations, edge cases).
  Skipped when :attr:`RtllmAgentConfig.plan` is False.
- ``rtl_generator``     -- emits the Verilog module from the description (+ contract).
- ``verifier``          -- :func:`c2hlsc_agent.rtllm_bench.evaluate_rtl`: ``iverilog`` then
  ``vvp``, with the benchmark's own testbench as the oracle. The verifier is the gate; the
  model only ever *proposes* RTL.
- ``failure_analyst``   -- :func:`build_evidence`: compact, tail-sliced evidence from the
  EARLIEST failing stage only, plus the failure family and a one-line repair intent.
- ``rtl_repair_agent``  -- minimal patch of the current candidate given only that evidence.

Benchmark-integrity rules (these are the whole point of the harness; breaking one
invalidates every number it prints):

1. No prompt this module builds contains the golden reference RTL (``verified_*.v``) or the
   testbench source: the model's inputs are the natural-language description, the contract
   it wrote itself, its own prior RTL, and tool output from its own failing run. No prompt
   builder here reads :attr:`RtllmDesign.testbench` or :attr:`RtllmDesign.reference_files`.

   This is a guarantee about *prompts*, and prompts are not the only channel. An agentic
   backend with filesystem tools can read the oracle off disk no matter what the prompt
   says, so :class:`c2hlsc_agent.llm.ClaudeCLIClient` runs its subprocess tool-less and in
   an empty scratch directory. If you add a backend that can call tools, sandbox it the
   same way or stop calling the numbers clean.
2. ``evidence_policy="none"`` reduces repair to a blind retry -- no tool output, no failure
   family, no stage -- so a user can measure what the feedback loop is actually worth.
   ``"logs"`` (the default) passes the compile/sim tail. The policy is recorded in every
   :class:`SampleResult` so a report cannot misrepresent the setting.
3. :func:`extract_verilog` never renames the model's module. If the required module name is
   absent, the candidate goes to the verifier unchanged and is scored as
   ``missing_module`` -- that is a real benchmark failure, not something to paper over.
   It equally never *edits* the candidate: a module containing ``$display`` or ``$finish``
   is passed through and refused by the verifier as ``illegal_system_task``, because a
   candidate able to write to the simulator's stdout can print the oracle's pass marker
   itself. Rejecting is the honest score; silently stripping would hide a real failure.

The module is deterministic and side-effect-light: it writes only inside the ``workdir``
it is handed, keeps no global state, and prints nothing (pass a ``log`` callback if you
want progress). It is therefore safe to run one design per worker thread.

Additive note for the frozen cross-module contract: :class:`SampleResult` carries two extra
fields beyond the agreed set (``evidence_policy`` and ``plan_error``), both keyword-optional
with defaults, so existing positional construction and dict consumers keep working.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .llm import LLMClient, extract_code_blocks
from .rtllm_bench import (
    DEFAULT_COMPILE_TIMEOUT,
    DEFAULT_SIM_TIMEOUT,
    DIAG_WALL_CLOCK_S,
    RtllmDesign,
    SimResult,
    diagnose_timeout,
    evaluate_rtl,
    oracle_behaviour_diff,
    run_self_trace,
    trace_digest,
)

# Evidence is tail-sliced for the same reason as llm.py's _EVIDENCE_LIMIT: the failure
# signature in a tool log is at the END, after the banner, the file list, and the noise.
# Keep the two limits equal so repair prompts stay comparable across both agent loops.
EVIDENCE_LIMIT = 4000

PLANNER_ROLE = "rtl_planner"
GENERATOR_ROLE = "rtl_generator"
REPAIR_ROLE = "rtl_repair_agent"

#: What the failure_analyst is allowed to put in front of the repair agent, weakest first.
#: The ladder is the ablation: each rung adds exactly one channel, so the difference between
#: two runs is attributable to that channel and nothing else.
#:
#: ``none``    blind retry. No tool output, no failure family, no stage.
#: ``logs``    the compile/sim tail from the earliest failing stage (the default).
#: ``self``    ``logs`` plus a trace of the candidate's OWN signals, obtained by running an
#:             instrumented, NON-SCORED copy of it. Strict: nothing the golden RTL or the
#:             testbench source knows can enter this path, so it stays publishable.
#: ``oracle``  ``logs`` plus where the candidate's stdout first diverges from the reference
#:             RTL's. UPPER BOUND ONLY -- see ORACLE_DERIVED_POLICIES.
EVIDENCE_POLICIES = ("none", "logs", "self", "oracle")

#: Policies that show the model something derived from the answer key. A run using one of
#: these is NOT comparable to a published RTLLM number or to the strict track, and every
#: artifact it produces is stamped: :attr:`SampleResult.oracle_derived_evidence`, the
#: ``oracle_derived_evidence`` key in every result row and report, a line in report.md's
#: Configuration block, and a warning from the driver. Widening this set without widening
#: those stamps is how an upper bound gets quoted as a headline.
ORACLE_DERIVED_POLICIES = frozenset({"oracle"})

_RETRY_BASE_DELAY = 2.0  # 2s, 4s, 8s ... between failed client.complete calls
_RETRYABLE = (RuntimeError, OSError, subprocess.TimeoutExpired)


RTL_PLANNER_SYSTEM_PROMPT = """You are rtl_planner in a verification-first natural-language-to-RTL loop.

You receive ONE natural-language hardware design description. You do NOT write RTL. You
write the interface contract that the rtl_generator agent must implement and that a hidden
testbench will hold the design to. Getting the interface wrong makes every later stage fail,
so be exhaustive and literal: prefer what the description says over what a typical design
would do.

Produce a compact contract with exactly these sections:

Module: the exact module name the description gives (copy it character for character).
Ports: one line per port -- `name | input|output | width (e.g. [7:0] or 1-bit) | meaning`.
  List EVERY port the description names, in the order it names them, with the exact
  identifiers. Note which outputs must be registered.
Clocking: is there a clock? which edge? is the design sequential, combinational, or mixed?
Reset: name, synchronous or asynchronous, active-high or active-low, and the exact reset
  value of every register. If the description does not say, state the assumption you chose.
Behavior: the input-to-output function in 3-8 precise lines, including any per-cycle
  schedule (what is valid on which cycle) and the latency from input to output.
State: registers/counters needed, their widths, and the state-machine states if any.
Edge cases: overflow/saturation, division or shift by zero, empty/full, first cycle after
  reset, back-to-back requests, and any wrap-around behavior.
Ambiguities: anything the description leaves open, and the single resolution you picked.

Keep it under 40 lines. Output the contract as plain text only -- no code fences, no Verilog.
"""


RTL_GENERATOR_SYSTEM_PROMPT = """You are rtl_generator in a verification-first natural-language-to-RTL loop.

From a natural-language description (and, when present, an interface contract) you write ONE
complete, synthesizable Verilog design file. A hidden testbench you will never see
instantiates your module and compares its outputs against a golden model; it is the only
judge. Correctness under that testbench beats elegance.

Hard rules:
- Name the top module EXACTLY as the description says, and declare EXACTLY the ports it
  names, with the same identifiers, directions, and bit widths. The testbench binds ports by
  name: one renamed or missing port fails the whole design.
- Target Verilog-2001 that Icarus Verilog (iverilog 12) accepts. Do NOT use SystemVerilog-only
  constructs: no `logic`, `always_ff`/`always_comb`/`always_latch`, `typedef`, `struct`,
  `enum`, `interface`, `unique`/`priority`, `break`/`continue`, `++`, or assignment-pattern
  array initializers. iverilog rejects several of these with "sorry: ... not supported".
- Declare every output assigned inside an `always` block as `reg` (or `output reg`).
- Sequential logic: non-blocking `<=`, one clock edge per block, and honour the described
  reset polarity and synchronicity. Combinational logic: blocking `=`, complete sensitivity
  (`always @(*)`), and assign every output on every path so no latch is inferred.
- Reset every register to a defined value; do not rely on `initial` blocks for design state.
- Helper submodules are welcome (e.g. a `full_adder` under an 8-bit adder). Define them in
  the SAME file, below the top module.
- Do NOT write a testbench, a `testbench`/`*_tb` module, or any simulation output or control
  system task: no `$display`, `$write`, `$monitor`, `$strobe`, `$fdisplay`, `$finish`,
  `$stop`, `$dumpvars`. This is enforced, not advisory: the harness refuses such a file
  without compiling it and scores the design as failed. Only the hidden testbench may print.
  (`$signed`, `$unsigned`, `$clog2` and friends are fine.)
- No `include of other files. The file must be self-contained.

Output ONLY the complete Verilog file in ONE ```verilog fenced block. No prose outside it.
"""


RTL_REPAIR_SYSTEM_PROMPT = """You are rtl_repair_agent in a verification-first natural-language-to-RTL loop.

You receive ONE Verilog candidate that failed verification, plus compact evidence from the
EARLIEST failing stage (compile if it did not compile, otherwise simulation). You never see
the testbench or any reference design -- only the description, the candidate, and the tool
output the candidate produced.

Rules:
- Make the MINIMAL change that fixes the reported failure. Do not rewrite working logic, do
  not restyle, do not add features the description does not ask for.
- Keep the module name and the declared port list unless the evidence proves the interface
  itself is wrong (e.g. the harness names a port the module does not declare).
- Diagnose before editing: name the mechanism in one line, then fix that mechanism. A wrong
  value is usually a width, sign, reset value, or off-by-one-cycle timing bug; a hang is
  usually a state that is never left or a done/valid pulse that is never asserted.
- Stay in the Verilog-2001 subset Icarus Verilog accepts, keep the file self-contained, and
  add no testbench module and no simulation output/control system task
  (`$display`/`$write`/`$monitor`/`$strobe`/`$fdisplay`/`$finish`/`$stop`/`$dump*`). The
  harness refuses such a file without compiling it.
- Return the COMPLETE corrected file in a single ```verilog fenced block, and nothing else of
  substance.
"""


# One-line repair intent per rtllm_bench failure family -- the failure_analyst's verdict on
# what the repair agent should actually go and do.
REPAIR_INTENTS: dict[str, str] = {
    "compile_error": (
        "iverilog rejected the source: fix exactly the syntax/elaboration errors it names, "
        "keeping the declared interface."
    ),
    "missing_module": (
        "The harness could not find the module it must instantiate: define the top module "
        "with EXACTLY the required name."
    ),
    "port_mismatch": (
        "The harness connected ports this module does not declare (or declares with the "
        "wrong direction/width): re-derive the port list from the description."
    ),
    "functional_mismatch": (
        "The design elaborates and runs but computes wrong values: fix the logic, reset "
        "values, or cycle timing -- not the interface."
    ),
    "timeout": (
        "The simulation never finished: find the state that is never left, the done/valid "
        "pulse that is never asserted, or the combinational feedback loop."
    ),
    "no_output": (
        "The run produced no pass/fail marker: the design most likely never drove its "
        "outputs, or the run died before the harness could report."
    ),
    "simulator_unsupported": (
        "iverilog rejected a construct as unsupported ('sorry: ...'): rewrite that logic in "
        "the plain Verilog-2001 subset."
    ),
    "missing_golden_data": (
        "The harness is missing a golden data file it needs; this is a benchmark defect and "
        "no RTL change can fix it."
    ),
    "illegal_system_task": (
        "The candidate was refused before compiling: a design file may not contain "
        "simulation output or control system tasks. Delete every $display/$write/$monitor/"
        "$strobe/$fdisplay/$finish/$stop/$dump* and drive the outputs with real logic."
    ),
    "runaway_output": (
        "The simulation was killed for flooding its output: find the loop or clock "
        "generator that never advances simulation time and never terminates."
    ),
}

_DEFAULT_INTENT = (
    "Verification failed without a recognised signature: re-read the description, then fix "
    "the most likely mechanism."
)


# Per-family repair procedure. ``REPAIR_INTENTS`` says WHAT went wrong in one line; this says
# HOW to attack it. A generic "fix the bug" instruction wastes the repair round on the wrong
# layer -- a port mismatch is not repaired by re-deriving the arithmetic, and a hang is not
# repaired by adjusting a constant. Kept to a handful of lines each: this text competes with
# the tool output for the model's attention, and the tool output is the evidence.
FAMILY_REPAIR_INSTRUCTIONS: dict[str, str] = {
    "missing_module": (
        "How to repair this family (INTERFACE CONTRACT VIOLATED -- do not touch the logic "
        "until the interface is right):\n"
        "1. The harness instantiates one module by name and found none. Declare it.\n"
        "2. Copy the module name and every port identifier CHARACTER FOR CHARACTER from the "
        "description below. Do not pluralise, abbreviate, or re-case them.\n"
        "3. Keep helper submodules in the same file, below the top module."
    ),
    "port_mismatch": (
        "How to repair this family (INTERFACE CONTRACT VIOLATED -- do not touch the logic "
        "until the interface is right):\n"
        "1. The harness connected a port this module does not declare, or declares with the "
        "wrong direction or width. The harness is fixed; the module is what changes.\n"
        "2. Re-derive the ENTIRE port list from the description: every identifier, its "
        "direction, and its exact bit range. A port the description names must exist even if "
        "your implementation does not need it.\n"
        "3. Do not rename a port to match your logic -- rename your logic."
    ),
    "compile_error": (
        "How to repair this family:\n"
        "1. Read the FIRST error the compiler reports; later ones are usually its "
        "consequences.\n"
        "2. Fix exactly what it names at the line it names. Do not rewrite unrelated code.\n"
        "3. Keep to the Verilog-2001 subset iverilog accepts: no `logic`, `always_ff`, "
        "`typedef`, `enum`, `break`, `++`, or array assignment patterns.\n"
        "4. Every output assigned inside an `always` block must be declared `reg`."
    ),
    "simulator_unsupported": (
        "How to repair this family:\n"
        "1. iverilog said 'sorry: ...' -- the construct is legal Verilog it does not "
        "implement, so no amount of syntax fixing helps.\n"
        "2. Rewrite that one construct in plain Verilog-2001 (loops instead of array "
        "assignment patterns, `disable` instead of `break`, explicit widths instead of "
        "assignment patterns) and change nothing else."
    ),
    "functional_mismatch": (
        "How to repair this family (BEHAVIOUR, not interface -- keep the port list exactly as "
        "it is):\n"
        "1. From the evidence, name the first input pattern whose output is wrong.\n"
        "2. Hand-compute what the description requires for that pattern and compare it with "
        "what the design produced. State the mechanism in one line before editing.\n"
        "3. The usual mechanisms, in order of frequency: wrong bit width or truncation, "
        "signed vs unsigned, an off-by-one-cycle registered output, a reset value, or a "
        "boundary case (zero, maximum, overflow) the logic never handles."
    ),
    "timeout": (
        "How to repair this family (LIVENESS -- the design never finished, so no output was "
        "ever compared):\n"
        "1. Look for a combinational loop: an `always @(*)` block (or `assign`) whose output "
        "feeds back into its own sensitivity list. Simulation time cannot advance through "
        "one.\n"
        "2. Look for a missing clock edge: a state register updated in a block with no "
        "`posedge`/`negedge`, or a state machine with a state that has no exit transition.\n"
        "3. Look for a terminating condition that is never reached: a counter that never "
        "hits its terminal value, or a done/valid/ready output that is never asserted "
        "because its guard is unreachable.\n"
        "4. Check reset: a register left at X propagates X through every comparison, so no "
        "condition guarded by it can ever be true."
    ),
    "illegal_system_task": (
        "How to repair this family:\n"
        "1. Delete every $display/$write/$monitor/$strobe/$fdisplay/$finish/$stop/$dump* "
        "from the design file. There is no acceptable use of them in a design.\n"
        "2. Drive the outputs with real logic instead -- whatever those tasks were reporting "
        "is what the outputs are supposed to carry.\n"
        "3. $signed/$unsigned/$clog2 are fine and do not need removing."
    ),
    "runaway_output": (
        "How to repair this family:\n"
        "1. The run was killed for flooding its output, which means a loop is executing "
        "without ever advancing simulation time.\n"
        "2. Find the `forever`/`while`/`always` block with no delay and no edge control, and "
        "give it a real termination or a real clock edge."
    ),
    "no_output": (
        "How to repair this family:\n"
        "1. The run produced nothing at all, so the design most likely never drove its "
        "outputs off their initial X.\n"
        "2. Check that every output is assigned on every path and reset to a defined value."
    ),
}

#: Headers that open the interface section of an RTLLM description. Their indented bodies are
#: what gets restated verbatim to a model that broke the interface contract.
_INTERFACE_HEADER_RE = re.compile(
    r"^\s*(?:module name|input ports?|output ports?|inout ports?|parameters?)\s*:", re.IGNORECASE
)

#: Cap on the restated interface block, so it cannot crowd out the tool output.
_INTERFACE_RESTATEMENT_LIMIT = 1500


def interface_restatement(design: RtllmDesign) -> str:
    """Re-quote the module name and port list *from the description* for an interface failure.

    Purely description-derived -- the same text the model already received, pulled to the
    front because a model that got the interface wrong read past it. Nothing is read from
    the testbench or the reference, so this is admissible under every evidence policy
    (including ``none``, though ``none`` shows no instructions at all).
    """

    kept: list[str] = []
    capturing = False
    for line in (design.description or "").splitlines():
        if _INTERFACE_HEADER_RE.match(line):
            capturing = True
            kept.append(line.rstrip())
            continue
        if not capturing:
            continue
        if not line.strip() or line[:1] not in " \t":
            capturing = False  # a blank line or an unindented line ends the block
            continue
        kept.append(line.rstrip())

    header = f"The interface the harness requires (restated from the description):\nModule name: {design.name}"
    if not kept:
        return header
    return header + "\n" + "\n".join(kept)[:_INTERFACE_RESTATEMENT_LIMIT]


#: Families whose failure IS the interface, so the port list is restated in full.
_INTERFACE_FAMILIES = frozenset({"missing_module", "port_mismatch"})


def repair_instructions(design: RtllmDesign, family: "str | None") -> str:
    """The family-specific repair procedure, plus the interface restatement when it applies."""

    block = FAMILY_REPAIR_INSTRUCTIONS.get(family or "")
    if family in _INTERFACE_FAMILIES:
        restated = interface_restatement(design)
        return f"{block}\n\n{restated}" if block else restated
    return block or ""

# Families whose failure lives in the benchmark harness, not in the candidate RTL. Repairing
# against them burns LLM calls on a design that cannot pass however good the RTL is, so the
# repair loop stops instead (the failure is still reported, unchanged).
UNREPAIRABLE_FAMILIES = frozenset({"missing_golden_data"})


class StopSignal(Protocol):
    """Anything answering "should I wind down?" -- :class:`threading.Event` satisfies it."""

    def is_set(self) -> bool:  # pragma: no cover - protocol
        ...


class LlmCallError(RuntimeError):
    """Raised when an agent's ``client.complete`` still fails after every retry."""


@dataclass
class RtllmAgentConfig:
    """Knobs for one benchmark run of the multi-agent loop."""

    max_repair_rounds: int = 2
    samples: int = 1
    plan: bool = True  # run the rtl_planner contract agent before generation
    evidence_policy: str = "logs"  # one of EVIDENCE_POLICIES
    sim_timeout: int = DEFAULT_SIM_TIMEOUT
    compile_timeout: int = DEFAULT_COMPILE_TIMEOUT
    apply_shims: bool = True
    llm_retries: int = 2

    def __post_init__(self) -> None:
        # Validated, not normalised-away: a typo like "sef" must not silently degrade to
        # "logs" and be reported as a self-derived measurement.
        policy = (self.evidence_policy or "logs").lower()
        if policy not in EVIDENCE_POLICIES:
            raise ValueError(
                "evidence_policy must be one of %s, got %r"
                % (", ".join(EVIDENCE_POLICIES), self.evidence_policy)
            )
        self.evidence_policy = policy

    @property
    def oracle_derived_evidence(self) -> bool:
        """True when this configuration shows the model something derived from the answer key."""

        return self.evidence_policy in ORACLE_DERIVED_POLICIES

    def to_dict(self) -> dict[str, object]:
        return {
            "max_repair_rounds": self.max_repair_rounds,
            "samples": self.samples,
            "plan": self.plan,
            "evidence_policy": self.evidence_policy,
            "oracle_derived_evidence": self.oracle_derived_evidence,
            "sim_timeout": self.sim_timeout,
            "compile_timeout": self.compile_timeout,
            "apply_shims": self.apply_shims,
            "llm_retries": self.llm_retries,
        }


@dataclass
class AttemptRecord:
    """One generate-or-repair -> verify round."""

    round: int
    role: str  # "rtl_generator" | "rtl_repair_agent"
    sim: SimResult
    rtl: str
    llm_error: str | None = None
    #: Which evidence channels fed the prompt that produced THIS round's RTL. Empty for a
    #: generation round (nothing had failed yet). Recorded so a report can state what the
    #: model was actually shown instead of inferring it from the configured policy, which
    #: only says what was *permitted*.
    evidence_sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "round": self.round,
            "role": self.role,
            "sim": self.sim.to_dict(),
            "rtl": self.rtl,
            "llm_error": self.llm_error,
            "evidence_sources": list(self.evidence_sources),
        }


@dataclass
class SampleResult:
    """One independent attempt at a design (generation + up to N repair rounds)."""

    design: str
    sample: int
    syntax_pass: bool
    func_pass: bool
    func_pass_strict: bool
    rounds: list[AttemptRecord] = field(default_factory=list)
    contract: str | None = None
    final_rtl: str = ""
    evidence_policy: str = "logs"
    plan_error: str | None = None
    #: True when this sample's repair prompts could contain oracle-derived evidence. Stamped
    #: from the policy, not from whether a diff happened to fire, so a design that passed at
    #: round 0 in an oracle-track sweep still carries the mark of the track it belongs to.
    oracle_derived_evidence: bool = False

    @property
    def syntax_pass_round(self) -> int | None:
        return next((r.round for r in self.rounds if r.sim.syntax_pass), None)

    @property
    def func_pass_round(self) -> int | None:
        return next((r.round for r in self.rounds if r.sim.func_pass), None)

    @property
    def llm_error(self) -> str | None:
        return next((r.llm_error for r in reversed(self.rounds) if r.llm_error), None)

    def to_dict(self) -> dict[str, object]:
        return {
            "design": self.design,
            "sample": self.sample,
            "syntax_pass": self.syntax_pass,
            "func_pass": self.func_pass,
            "func_pass_strict": self.func_pass_strict,
            "syntax_pass_round": self.syntax_pass_round,
            "func_pass_round": self.func_pass_round,
            "repair_rounds": max(len(self.rounds) - 1, 0),
            "rounds": [r.to_dict() for r in self.rounds],
            "contract": self.contract,
            "plan_error": self.plan_error,
            "evidence_policy": self.evidence_policy,
            "llm_error": self.llm_error,
            "final_rtl": self.final_rtl,
        }


@dataclass
class DesignResult:
    """Every sample for one benchmark design, with RTLLM's pass@k accounting."""

    design: str
    category: str
    samples: list[SampleResult] = field(default_factory=list)
    syntax_success: int = 0  # samples that compiled
    func_success: int = 0  # samples that func-passed

    def to_dict(self) -> dict[str, object]:
        return {
            "design": self.design,
            "category": self.category,
            "n_samples": len(self.samples),
            "syntax_success": self.syntax_success,
            "func_success": self.func_success,
            "evidence_policy": self.samples[0].evidence_policy if self.samples else None,
            "samples": [s.to_dict() for s in self.samples],
        }


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #

_VERILOG_LANGS = {"", "verilog", "systemverilog", "sv", "v", "vlog"}

#: Hard cap on the response text handed to the parser. A degenerate model response (the
#: classic line-repetition loop) is otherwise unbounded, and no RTLLM design needs anything
#: close to this. Truncation is recorded by the caller and the candidate simply fails.
MAX_RESPONSE_CHARS = 512_000

# Module units, matched at line starts so the word "module" inside a comment or a $display
# string cannot open a bogus unit. Verilog modules do not nest, so pairing each line-initial
# `module` with the next line-initial `endmodule` is exact.
#
# Two linear scans, deliberately NOT one `module ... .*? endmodule` regex: a lazy body under
# (?ms) rescans to end-of-input from every `module` that has no `endmodule` after it, which
# is quadratic. Measured on the degenerate case (`module foo(...);` repeated, no endmodule):
# 250/500/1000/2000 repeats took 0.15/0.61/2.27/8.90 s before this change, and a ~1 MB
# response stalled a worker thread for tens of minutes with no timeout and no cancellation.
_MODULE_START_RE = re.compile(
    r"(?m)^[ \t]*module\b[ \t\r\n]+(?P<name>\\\S+|[A-Za-z_][A-Za-z0-9_$]*)"
)
_MODULE_END_RE = re.compile(r"(?m)^[ \t]*endmodule\b[^\n]*")
# Only compiler directives and comments survive from the text before the first module; any
# other line there is prose the model wrapped around its code.
_PREAMBLE_KEEP_RE = re.compile(r"^[ \t]*(`|//)")


class _ModuleUnit:
    """One ``module ... endmodule`` span located by :func:`_module_units`."""

    __slots__ = ("name", "start", "end")

    def __init__(self, name: str, start: int, end: int) -> None:
        self.name = name
        self.start = start
        self.end = end


def _module_units(text: str) -> list[_ModuleUnit]:
    """Pair line-initial ``module`` headers with their ``endmodule`` in O(n).

    Reproduces the semantics of scanning with a single non-greedy regex -- each unit runs
    to the first ``endmodule`` after its header, and headers swallowed by a preceding unit
    are skipped -- without that pattern's quadratic backtracking.
    """

    starts = list(_MODULE_START_RE.finditer(text))
    ends = list(_MODULE_END_RE.finditer(text))
    units: list[_ModuleUnit] = []
    start_index = end_index = 0
    while start_index < len(starts):
        start = starts[start_index]
        while end_index < len(ends) and ends[end_index].start() < start.end():
            end_index += 1
        if end_index >= len(ends):
            break  # truncated block: no `endmodule` left to close this header
        end = ends[end_index]
        end_index += 1
        units.append(_ModuleUnit(start.group("name"), start.start(), end.end()))
        start_index += 1
        while start_index < len(starts) and starts[start_index].start() < end.end():
            start_index += 1  # nested/bogus header already inside the unit just closed
    return units


def _looks_like_testbench(name: str, module_name: str) -> bool:
    """True for a module the model volunteered as a testbench (never for the required top)."""

    if name == module_name:
        return False
    lowered = name.lower()
    if lowered in {"testbench", "tb", "test", "top_tb", "tb_top"}:
        return True
    return lowered.endswith(("_tb", "_testbench", "_test")) or lowered.startswith("tb_")


def _code_pool(text: str) -> str:
    """Concatenate the fenced blocks that plausibly hold Verilog, else the raw text.

    The response is capped at :data:`MAX_RESPONSE_CHARS` first: nothing downstream should
    have to be robust against an unbounded string, and a response that long is degenerate
    output, not a design.
    """

    text = (text or "")[:MAX_RESPONSE_CHARS]
    blocks = extract_code_blocks(text)
    pool = [body for lang, body in blocks if lang in _VERILOG_LANGS and "module" in body]
    if not pool:  # mislabeled fence (```text, ```rtl, ...): fall back on content
        pool = [body for lang, body in blocks if "module" in body and "endmodule" in body]
    kept = [body.strip() for body in pool if body.strip()]
    if kept:
        return "\n\n".join(kept)
    return text or ""


def extract_verilog(text: str, module_name: str) -> str:
    """Pull the design file out of a model response.

    Prefers fenced ```verilog / ```systemverilog / bare ``` blocks, falls back to slicing
    from the first ``module`` to the last ``endmodule``, drops any testbench module the model
    volunteered, and keeps helper submodules (``adder_8bit`` legitimately needs
    ``full_adder``). Duplicate definitions of the same module collapse to the last one, which
    is the refined version when a model shows a draft first.

    The model's module name is never rewritten: if ``module_name`` is absent the text is
    returned as-is so the verifier reports ``missing_module``, which is the truthful score.
    Returns ``""`` for an empty response, a refusal, or a response with no module at all.
    """

    pool = _code_pool(text)
    if "module" not in pool:
        return ""

    units = _module_units(pool)
    if not units:
        # `endmodule` was not at a line start (or the block is truncated): coarse slice.
        start = pool.find("module")
        end = pool.rfind("endmodule")
        if start < 0 or end < start:
            return ""
        return pool[start : end + len("endmodule")].strip() + "\n"

    kept: dict[str, str] = {}
    for unit in units:
        if _looks_like_testbench(unit.name, module_name):
            continue
        # last definition wins, first position kept
        kept[unit.name] = pool[unit.start : unit.end].strip()
    if not kept:
        return ""

    preamble = "\n".join(
        line for line in pool[: units[0].start].splitlines() if _PREAMBLE_KEEP_RE.match(line)
    ).strip()
    body = "\n\n".join(kept.values())
    return (f"{preamble}\n\n{body}" if preamble else body).strip() + "\n"


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)


def _complete(
    client: LLMClient | None,
    system: str,
    user: str,
    config: RtllmAgentConfig,
    *,
    role: str,
    log: Callable[[str], None] | None = None,
) -> str:
    """``client.complete`` with retry-with-backoff; raises :class:`LlmCallError` at the end.

    Only transport-shaped failures are retried (a dead CLI, a socket error, a timeout).
    The caller turns the final failure into an ``llm_error`` on the attempt record instead
    of letting one flaky call abort a 50-design sweep.
    """

    if client is None:
        raise LlmCallError(f"{role}: no LLM client (build_llm_client returned None)")
    attempts = max(1, int(config.llm_retries) + 1)
    delay = _RETRY_BASE_DELAY
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return client.complete(system, user) or ""
        except _RETRYABLE as exc:  # noqa: PERF203 - retry is the point
            last = exc
            if attempt + 1 >= attempts:
                break
            _log(log, f"{role}: attempt {attempt + 1}/{attempts} failed ({exc}); retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
    raise LlmCallError(f"{role} failed after {attempts} attempt(s): {last}")


def _description_block(design: RtllmDesign) -> str:
    """The ONLY specification the model ever sees. Never the testbench, never the reference."""

    return (
        f"Design name (the top module MUST be named exactly `{design.name}`): {design.name}\n"
        f"Benchmark category: {design.category}\n\n"
        "Natural-language design description (verbatim, and the only specification you get):\n"
        '"""\n'
        f"{design.description.strip()}\n"
        '"""\n'
    )


def plan_contract(
    client: LLMClient | None,
    design: RtllmDesign,
    config: RtllmAgentConfig,
    *,
    log: Callable[[str], None] | None = None,
) -> str:
    """rtl_planner: turn the description into an explicit interface contract."""

    user = (
        f"{_description_block(design)}\n"
        "Write the interface contract for this design. Do not write any Verilog."
    )
    return _complete(client, RTL_PLANNER_SYSTEM_PROMPT, user, config, role=PLANNER_ROLE, log=log).strip()


def generate_rtl(
    client: LLMClient | None,
    design: RtllmDesign,
    contract: str | None,
    config: RtllmAgentConfig,
    *,
    log: Callable[[str], None] | None = None,
) -> str:
    """rtl_generator: emit the Verilog module from the description (+ contract)."""

    contract_block = ""
    if contract and contract.strip():
        contract_block = (
            "\nInterface contract from rtl_planner (implement it exactly; if it contradicts the\n"
            "description above, the description wins):\n"
            '"""\n'
            f"{contract.strip()}\n"
            '"""\n'
        )
    user = (
        f"{_description_block(design)}{contract_block}\n"
        f"Write the complete Verilog file. The top module must be named `{design.name}`.\n"
        "Return it in ONE ```verilog block."
    )
    response = _complete(client, RTL_GENERATOR_SYSTEM_PROMPT, user, config, role=GENERATOR_ROLE, log=log)
    return extract_verilog(response, design.name)


def build_evidence(sim: SimResult, config: RtllmAgentConfig) -> str:
    """failure_analyst: compact evidence from the EARLIEST failing stage only.

    Compile output when the candidate did not compile, otherwise simulation output; never
    both, so the repair agent works one failure at a time. Tail-sliced to
    :data:`EVIDENCE_LIMIT` because the signature sits at the end of a tool log, after the
    banner and the file list.

    Under ``evidence_policy="none"`` this returns a fixed notice with no tool output, no
    failure family, and no stage -- the blind-retry ablation.
    """

    if (config.evidence_policy or "logs").lower() == "none":
        return (
            "Verification result: FAILED.\n"
            "Tool output: withheld (evidence_policy=none -- this is a blind retry).\n"
            "Repair intent: you get no diagnostics; re-derive the design from the description "
            "and produce a materially different implementation."
        )

    if not sim.syntax_pass:
        stage = "compile (iverilog)"
        raw = sim.compile_log or sim.sim_log
    else:
        stage = "simulate (vvp)"
        raw = sim.sim_log or sim.compile_log
    if sim.timed_out:
        stage += " -- KILLED BY WATCHDOG"

    family = sim.failure_family or "unknown"
    intent = REPAIR_INTENTS.get(sim.failure_family or "", _DEFAULT_INTENT)
    # Tail slice: the failure signature is at the END of the log.
    excerpt = (raw or "").strip()[-EVIDENCE_LIMIT:] or "(no captured tool output)"
    return (
        f"Earliest failing stage: {stage}\n"
        f"Failure family: {family}\n"
        f"Repair intent: {intent}\n\n"
        f"Tool output (tail, at most {EVIDENCE_LIMIT} chars):\n"
        f"```\n{excerpt}\n```"
    )


def repair_rtl(
    client: LLMClient | None,
    design: RtllmDesign,
    rtl: str,
    sim: SimResult,
    config: RtllmAgentConfig,
    *,
    log: Callable[[str], None] | None = None,
) -> str:
    """rtl_repair_agent: minimal patch of the current candidate from compact evidence only."""

    user = (
        f"{_description_block(design)}\n"
        f"{build_evidence(sim, config)}\n\n"
        f"Current `{design.name}.v` to repair:\n"
        "```verilog\n"
        f"{(rtl or '').rstrip()}\n"
        "```\n\n"
        f"Return the full corrected file in one ```verilog block. The top module must still be "
        f"named `{design.name}`. Change as little as possible."
    )
    response = _complete(client, RTL_REPAIR_SYSTEM_PROMPT, user, config, role=REPAIR_ROLE, log=log)
    return extract_verilog(response, design.name)


# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #


def _llm_error_sim(design: RtllmDesign) -> SimResult:
    """A non-result for a round where the model never answered.

    ``failure_family`` stays ``None``: no benchmark family describes "the backend was down",
    and mislabeling it would corrupt the failure histogram.
    """

    return SimResult(
        design=design.name,
        syntax_pass=False,
        func_pass=False,
        func_pass_strict=False,
        timed_out=False,
        compile_log="",
        sim_log="",
        duration_s=0.0,
        failure_family=None,
    )


def _attempt_dir(workdir: Path, sample: int, round_index: int) -> Path:
    """One clean directory per attempt, so a stale binary can never fake a syntax pass."""

    path = Path(workdir) / f"sample{sample:02d}" / f"round{round_index}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _verify(
    design: RtllmDesign,
    rtl: str,
    config: RtllmAgentConfig,
    workdir: Path,
    sample: int,
    round_index: int,
) -> SimResult:
    return evaluate_rtl(
        design,
        rtl,
        _attempt_dir(workdir, sample, round_index),
        compile_timeout=config.compile_timeout,
        sim_timeout=config.sim_timeout,
        apply_shims=config.apply_shims,
    )


def _stopped(stop: "StopSignal | None") -> bool:
    """True when the caller has asked the loop to wind down (SIGINT, deadline, ...)."""

    return bool(stop is not None and stop.is_set())


def _run_sample(
    design: RtllmDesign,
    client: LLMClient | None,
    config: RtllmAgentConfig,
    workdir: Path,
    sample: int,
    *,
    log: Callable[[str], None] | None = None,
    stop: "StopSignal | None" = None,
) -> SampleResult:
    rounds: list[AttemptRecord] = []
    contract: str | None = None
    plan_error: str | None = None

    if config.plan:
        try:
            contract = plan_contract(client, design, config, log=log) or None
        except LlmCallError as exc:
            # A planner outage is not fatal: generation can still run blind on the
            # description. If the backend really is down, the generator call below fails
            # too and ends the sample cleanly with an llm_error.
            plan_error = str(exc)
            _log(log, f"{design.name}[{sample}] {PLANNER_ROLE} unavailable: {exc}")

    try:
        rtl = generate_rtl(client, design, contract, config, log=log)
    except LlmCallError as exc:
        rounds.append(
            AttemptRecord(round=0, role=GENERATOR_ROLE, sim=_llm_error_sim(design), rtl="", llm_error=str(exc))
        )
        return _finish_sample(design, sample, rounds, contract, "", config, plan_error)

    sim = _verify(design, rtl, config, workdir, sample, 0)
    rounds.append(AttemptRecord(round=0, role=GENERATOR_ROLE, sim=sim, rtl=rtl))
    _log(log, f"{design.name}[{sample}] round 0: syntax={sim.syntax_pass} func={sim.func_pass}")

    repairs = max(0, int(config.max_repair_rounds))
    round_index = 0
    while not sim.func_pass and round_index < repairs:
        if _stopped(stop):
            # A repair round is one more multi-minute model call; an interrupt must shorten
            # in-flight work, not merely stop scheduling new designs. What ran is kept.
            _log(log, f"{design.name}[{sample}]: stop requested; ending after round {round_index}")
            break
        if sim.failure_family in UNREPAIRABLE_FAMILIES:
            _log(log, f"{design.name}[{sample}]: {sim.failure_family} is a harness defect; not repairing")
            break
        round_index += 1
        try:
            rtl = repair_rtl(client, design, rtl, sim, config, log=log)
        except LlmCallError as exc:
            rounds.append(
                AttemptRecord(
                    round=round_index,
                    role=REPAIR_ROLE,
                    sim=_llm_error_sim(design),
                    rtl=rtl,
                    llm_error=str(exc),
                )
            )
            break
        sim = _verify(design, rtl, config, workdir, sample, round_index)
        rounds.append(AttemptRecord(round=round_index, role=REPAIR_ROLE, sim=sim, rtl=rtl))
        _log(log, f"{design.name}[{sample}] round {round_index}: syntax={sim.syntax_pass} func={sim.func_pass}")

    return _finish_sample(design, sample, rounds, contract, rtl, config, plan_error)


def _finish_sample(
    design: RtllmDesign,
    sample: int,
    rounds: list[AttemptRecord],
    contract: str | None,
    final_rtl: str,
    config: RtllmAgentConfig,
    plan_error: str | None,
) -> SampleResult:
    """Best-of-rounds outcome plus the FINAL candidate (which round won is in to_dict)."""

    return SampleResult(
        design=design.name,
        sample=sample,
        syntax_pass=any(r.sim.syntax_pass for r in rounds),
        func_pass=any(r.sim.func_pass for r in rounds),
        func_pass_strict=any(r.sim.func_pass_strict for r in rounds),
        rounds=rounds,
        contract=contract,
        final_rtl=final_rtl,
        evidence_policy=(config.evidence_policy or "logs"),
        plan_error=plan_error,
    )


def run_design(
    design: RtllmDesign,
    client: LLMClient | None,
    config: RtllmAgentConfig,
    workdir: Path,
    *,
    log: Callable[[str], None] | None = None,
    stop: "StopSignal | None" = None,
) -> DesignResult:
    """Run the full loop for one design: plan -> generate -> verify -> (analyse -> repair)*.

    Repeated ``config.samples`` times, independently, so the driver can compute pass@k the
    way RTLLM's ``auto_run.py`` does: a sample counts once, and it counts if ANY round in it
    reached the outcome.

    ``stop`` (anything with ``is_set()``, typically a :class:`threading.Event`) is checked
    between samples and between repair rounds. Without it an interrupt cannot shorten an
    in-flight design, which at the default timeouts is hours of model calls: 900 s CLI
    timeout x 3 attempts x 4 roles. Samples already finished are still returned, so the
    partial result is honest about how many samples it represents.
    """

    samples: list[SampleResult] = []
    for index in range(max(1, int(config.samples))):
        if index and _stopped(stop):
            _log(log, f"{design.name}: stop requested; ran {index} of {config.samples} samples")
            break
        samples.append(_run_sample(design, client, config, Path(workdir), index, log=log, stop=stop))
    return DesignResult(
        design=design.name,
        category=design.category,
        samples=samples,
        syntax_success=sum(1 for s in samples if s.syntax_pass),
        func_success=sum(1 for s in samples if s.func_pass),
    )
