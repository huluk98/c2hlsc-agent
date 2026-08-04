from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ArgumentConfig:
    direction: str | None = None
    length: int | None = None
    range: tuple[int, int] | None = None
    interface: str | None = None


@dataclass
class AgentConfig:
    input_files: list[Path] = field(default_factory=list)
    include_dirs: list[Path] = field(default_factory=list)
    compiler_flags: list[str] = field(default_factory=list)
    top: str | None = None
    arguments: dict[str, ArgumentConfig] = field(default_factory=dict)
    num_tests: int = 100
    directed_tests: list[str] = field(default_factory=lambda: ["zeros", "ones", "minmax", "alternating"])
    part: str = "xczu7ev-ffvc1156-2-e"
    clock: float = 10.0
    interface_mode: str = "default"
    allow_pragmas: bool = True
    cosim_tool: str | None = None
    rtl: str = "verilog"
    seed: int = 1
    max_iterations: int = 1
    auto_repair: bool = False
    keep_going: bool = False
    run_vitis: bool = False
    use_llm: bool = False
    # Shift-left verification runs after host equivalence and before any HLS backend:
    # paired traces are required; gcov/KLEE preserve explicit pass/skipped/blocked evidence.
    run_shift_left: bool = True
    # Live contract_planner: an LLM pass after analysis that proposes per-argument
    # direction/length/range where the regex inference is uncertain; user config wins
    # per-field and the verifier ladder still gates everything. Requires use_llm.
    plan_contracts: bool = False
    # Audit-memory knowledge base (opt-in): promote audited repair successes into a
    # JSONL card store and retrieve them into future repair prompts as strategy hints.
    # Store path precedence: audit_memory_path > C2HLSC_AUDIT_MEMORY env > ~/.c2hlsc/.
    audit_memory: bool = False
    audit_memory_path: str | None = None
    llm_backend: str = "auto"
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_cli_cmd: str = "claude"
    llm_candidates: int = 1
    # Best-of-N generation (--candidates) dispatches its independent candidate generations
    # concurrently: each is a pure text-in/text-out call that blocks on the backend, so
    # threads overlap the waiting. De-duplication and scoring stay serial and in attempt
    # order, so the winner is identical to the serial path. Set to 1 to fully serialize.
    # NOTE: this does NOT apply to the qor_optimizer round loop, whose attempts chain
    # through `history` on purpose and must stay sequential.
    llm_candidate_workers: int = 4
    nl_spec: str | None = None
    vitis_ssh_host: str | None = None
    vitis_remote_dir: str = "~/c2hlsc_runs"
    vitis_setup: str | None = None
    vitis_bin: str = "vitis_hls"
    # Which backend runs the csynth/cosim ladder. "auto" picks vitis-ssh if a remote
    # host is configured, else local vitis_hls if on PATH, else the local Bambu backend
    # (local-hls) if available, else skips. Explicit values: vitis | vitis-ssh | local-hls | none.
    cosim_backend: str = "auto"
    # PPA workflow criteria (the `ppa:` config block). The node is a first-class
    # criterion: every slack/area/power gate is measured against it. min_slack is in
    # the node's liberty time unit (ns for nangate45/sky130hd, ps for asap7).
    node: str = "nangate45"
    run_local_ppa: bool = False
    min_slack: float | None = None
    max_area_um2: float | None = None
    max_power_w: float | None = None
    max_latency_cycles: int | None = None
    liberty: str | None = None  # explicit single-liberty override (legacy escape hatch)


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return {}
    # Match PyYAML's boolean set so the dependency-free fallback parser agrees with the
    # PyYAML path (e.g. `run_local_ppa: no` -> False either way, not the truthy str "no").
    if value.lower() in {"true", "yes", "on"}:
        return True
    if value.lower() in {"false", "no", "off"}:
        return False
    if value.lower() in {"null", "none"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        return ast.literal_eval(value)
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _strip_inline_comment(line: str) -> str:
    """Drop a ``#`` comment, honouring quotes and requiring whitespace before ``#``.

    A ``#`` inside a quoted scalar (e.g. ``nl_spec: "count the # of set bits"``) is part
    of the value, and a ``#`` not preceded by whitespace is too; only an unquoted ``#`` at
    line-start or after whitespace begins a comment. The naive ``split('#')`` corrupted
    both cases — a common trap for free-text values like ``nl_spec``.
    """

    quote: str | None = None
    for idx, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (idx == 0 or line[idx - 1] in " \t"):
            return line[:idx]
    return line


def _minimal_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    pending_list_key: tuple[int, dict[str, Any], str] | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        line = _strip_inline_comment(raw_line).rstrip()
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if content.startswith("- "):
            item = _parse_scalar(content[2:])
            if not isinstance(parent, list):
                if pending_list_key is None:
                    raise ValueError(f"YAML list item without list parent: {raw_line}")
                _, dict_parent, key = pending_list_key
                dict_parent[key] = []
                parent = dict_parent[key]
                stack.append((indent - 1, parent))
            parent.append(item)
            continue

        if ":" not in content:
            raise ValueError(f"Unsupported YAML line: {raw_line}")
        key, value = content.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not isinstance(parent, dict):
            raise ValueError(f"YAML mapping under non-mapping parent: {raw_line}")

        if value == "":
            new_map: dict[str, Any] = {}
            parent[key] = new_map
            pending_list_key = (indent, parent, key)
            stack.append((indent, new_map))
        else:
            parent[key] = _parse_scalar(value)
            pending_list_key = None

    return root


def _load_data(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        return json.loads(text)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("YAML root must be a mapping")
        return data
    except ModuleNotFoundError:
        return _minimal_yaml(text)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


_ARG_DIRECTIONS = {"input", "output", "inout"}


def _argument_config(data: Any) -> ArgumentConfig:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("argument metadata must be a mapping")
    range_value = data.get("range")
    parsed_range = None
    if isinstance(range_value, (list, tuple)) and len(range_value) == 2:
        parsed_range = (int(range_value[0]), int(range_value[1]))
    # Validate `direction` rather than passing any string through: every consumer tests
    # membership in {"output", "inout"}, so a typo like `out` silently means "not
    # compared" and the testbench proves nothing. Fail at config-load time instead.
    direction = data.get("direction")
    if direction is not None:
        if not isinstance(direction, str) or direction not in _ARG_DIRECTIONS:
            raise ValueError(
                f"argument direction must be one of {sorted(_ARG_DIRECTIONS)}; got {direction!r}"
            )
    return ArgumentConfig(
        direction=direction,
        length=int(data["length"]) if data.get("length") is not None else None,
        range=parsed_range,
        interface=data.get("interface"),
    )


def _ppa_block(data: dict[str, Any]) -> dict[str, Any]:
    """Parse the `ppa:` criteria block. Presence of the block enables the local PPA
    step by default (that is what "hardwired into the workflow" means) — it can still
    be disabled explicitly with `run_local_ppa: false`."""

    block = data.get("ppa")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ValueError("config `ppa` must be a mapping (node/min_slack/... keys)")
    known = {"node", "run_local_ppa", "min_slack", "max_area_um2", "max_power_w", "max_latency_cycles", "liberty"}
    unknown = set(block) - known
    if unknown:
        raise ValueError(f"unknown ppa config keys: {sorted(unknown)} (expected {sorted(known)})")
    node = str(block.get("node", "nangate45"))
    from .nodes import NODES  # local import keeps config importable without heavy deps

    if node not in NODES:
        raise ValueError(f"unknown ppa node {node!r}; expected one of {', '.join(sorted(NODES))}")
    criteria = {
        "min_slack": (float(block["min_slack"]) if block.get("min_slack") is not None else None),
        "max_area_um2": (float(block["max_area_um2"]) if block.get("max_area_um2") is not None else None),
        "max_power_w": (float(block["max_power_w"]) if block.get("max_power_w") is not None else None),
        "max_latency_cycles": (int(block["max_latency_cycles"]) if block.get("max_latency_cycles") is not None else None),
    }
    any_criterion = any(v is not None for v in criteria.values())
    # run_local_ppa is the single switch that runs the step. A declared hard criterion
    # must be enforced, so it forces the switch on; an explicit `run_local_ppa: false`
    # alongside a criterion is a contradiction we surface loudly rather than silently
    # dropping the criterion (finding: asymmetric gate-enable).
    explicit_off = block.get("run_local_ppa") is False
    if any_criterion and explicit_off:
        declared = [k for k, v in criteria.items() if v is not None]
        raise ValueError(
            f"ppa.run_local_ppa is false but these criteria were declared and would be "
            f"silently ignored: {declared}. Remove the criteria or set run_local_ppa: true."
        )
    return {
        "node": node,
        "run_local_ppa": bool(block.get("run_local_ppa", True)) or any_criterion,
        "liberty": (str(block["liberty"]) if block.get("liberty") else None),
        **criteria,
    }


def load_config(path: Path | None) -> AgentConfig:
    if path is None:
        return AgentConfig()
    data = _load_data(path)
    base = path.parent
    inputs = data.get("input_files", data.get("input", []))
    arguments = {
        name: _argument_config(value)
        for name, value in (data.get("arguments") or {}).items()
    }
    return AgentConfig(
        input_files=[(base / str(item)).resolve() for item in _as_list(inputs)],
        include_dirs=[(base / str(item)).resolve() for item in _as_list(data.get("include_dirs"))],
        compiler_flags=[str(item) for item in _as_list(data.get("compiler_flags"))],
        top=data.get("top"),
        arguments=arguments,
        # Clamp to >= 1: num_tests <= 0 makes the testbench loop body (including the
        # directed boundary cases) never execute, so nothing is compared.
        num_tests=max(1, int(data.get("num_tests", data.get("random_test_count", 100)))),
        directed_tests=[str(item) for item in _as_list(data.get("directed_tests"))] or ["zeros", "ones", "minmax", "alternating"],
        part=str(data.get("part", "xczu7ev-ffvc1156-2-e")),
        clock=float(data.get("clock", data.get("clock_period", 10.0))),
        interface_mode=str(data.get("interface_mode", "default")),
        allow_pragmas=bool(data.get("allow_pragmas", True)),
        max_iterations=int(data.get("max_iterations", 1)),
        auto_repair=bool(data.get("auto_repair", False)),
        keep_going=bool(data.get("keep_going", False)),
        run_vitis=bool(data.get("run_vitis", False)),
        seed=int(data.get("seed", 1)),
        use_llm=bool(data.get("use_llm", False)),
        run_shift_left=bool(data.get("run_shift_left", True)),
        plan_contracts=bool(data.get("plan_contracts", False)),
        audit_memory=bool(data.get("audit_memory", False)),
        audit_memory_path=(str(data["audit_memory_path"]) if data.get("audit_memory_path") else None),
        llm_backend=str(data.get("llm_backend", "auto")),
        llm_model=(str(data["llm_model"]) if data.get("llm_model") is not None else None),
        llm_base_url=(str(data["llm_base_url"]) if data.get("llm_base_url") is not None else None),
        llm_cli_cmd=str(data.get("llm_cli_cmd", "claude")),
        llm_candidates=int(data.get("llm_candidates", 1)),
        llm_candidate_workers=max(1, int(data.get("llm_candidate_workers", 4))),
        nl_spec=(str(data["nl_spec"]) if data.get("nl_spec") else None),
        vitis_ssh_host=(str(data["vitis_ssh_host"]) if data.get("vitis_ssh_host") else None),
        vitis_remote_dir=str(data.get("vitis_remote_dir", "~/c2hlsc_runs")),
        vitis_setup=(str(data["vitis_setup"]) if data.get("vitis_setup") else None),
        vitis_bin=str(data.get("vitis_bin", "vitis_hls")),
        cosim_backend=str(data.get("cosim_backend", "auto")),
        **_ppa_block(data),
    )


def merge_cli_config(config: AgentConfig, args: Any) -> AgentConfig:
    if getattr(args, "input", None):
        config.input_files = [Path(args.input).resolve()]
    if getattr(args, "top", None):
        config.top = args.top
    if getattr(args, "part", None):
        config.part = args.part
    if getattr(args, "clock", None) is not None:
        config.clock = float(args.clock)
    if getattr(args, "num_tests", None) is not None:
        requested = int(args.num_tests)
        if requested < 1:
            raise ValueError("--num-tests must be >= 1 (0 compares nothing)")
        config.num_tests = requested
    if getattr(args, "cosim_tool", None):
        config.cosim_tool = args.cosim_tool
    if getattr(args, "rtl", None):
        config.rtl = args.rtl
    if getattr(args, "seed", None) is not None:
        config.seed = int(args.seed)
    if getattr(args, "max_iterations", None) is not None:
        config.max_iterations = int(args.max_iterations)
    if getattr(args, "auto_repair", False):
        config.auto_repair = True
    if getattr(args, "keep_going", False):
        config.keep_going = True
    if getattr(args, "run_vitis", False):
        config.run_vitis = True
    if getattr(args, "no_run_vitis", False):
        config.run_vitis = False
    if getattr(args, "use_llm", False):
        config.use_llm = True
    elif getattr(args, "no_llm", False):
        config.use_llm = False
    if getattr(args, "shift_left", False):
        config.run_shift_left = True
    elif getattr(args, "no_shift_left", False):
        config.run_shift_left = False
    if getattr(args, "plan_contracts", False):
        config.plan_contracts = True
    elif getattr(args, "no_plan_contracts", False):
        config.plan_contracts = False
    if getattr(args, "audit_memory", False):
        config.audit_memory = True
    elif getattr(args, "no_audit_memory", False):
        config.audit_memory = False
    if getattr(args, "audit_memory_path", None):
        config.audit_memory_path = args.audit_memory_path
        config.audit_memory = True
    if getattr(args, "llm_backend", None):
        config.llm_backend = args.llm_backend
    if getattr(args, "llm_model", None):
        config.llm_model = args.llm_model
    if getattr(args, "llm_base_url", None):
        config.llm_base_url = args.llm_base_url
    if getattr(args, "llm_cli_cmd", None):
        config.llm_cli_cmd = args.llm_cli_cmd
    if getattr(args, "candidates", None) is not None:
        config.llm_candidates = max(1, int(args.candidates))
    if getattr(args, "llm_candidate_workers", None) is not None:
        config.llm_candidate_workers = max(1, int(args.llm_candidate_workers))
    spec_inline = getattr(args, "spec", None)
    spec_file = getattr(args, "spec_file", None)
    if spec_file:
        config.nl_spec = Path(spec_file).expanduser().read_text(encoding="utf-8").strip()
    elif spec_inline:
        config.nl_spec = spec_inline.strip()
    if getattr(args, "vitis_ssh", None):
        config.vitis_ssh_host = args.vitis_ssh
    if getattr(args, "vitis_remote_dir", None):
        config.vitis_remote_dir = args.vitis_remote_dir
    if getattr(args, "vitis_setup", None):
        config.vitis_setup = args.vitis_setup
    if getattr(args, "vitis_bin", None):
        config.vitis_bin = args.vitis_bin
    if getattr(args, "cosim_backend", None):
        config.cosim_backend = args.cosim_backend
    if getattr(args, "node", None):
        config.node = args.node
    if getattr(args, "min_slack", None) is not None:
        config.min_slack = float(args.min_slack)
        config.run_local_ppa = True
    if getattr(args, "max_area", None) is not None:
        config.max_area_um2 = float(args.max_area)
        config.run_local_ppa = True
    if getattr(args, "max_power", None) is not None:
        config.max_power_w = float(args.max_power)
        config.run_local_ppa = True
    if getattr(args, "max_latency", None) is not None:
        config.max_latency_cycles = int(args.max_latency)
        config.run_local_ppa = True
    if getattr(args, "local_ppa", False):
        config.run_local_ppa = True
    if getattr(args, "liberty", None):
        config.liberty = args.liberty
    # Fold in the env var here (not only in RemoteVitis.from_config) so the
    # "remote host implies --run-vitis" rule below applies to it too.
    config.vitis_ssh_host = config.vitis_ssh_host or os.environ.get("C2HLSC_VITIS_SSH")
    if config.vitis_ssh_host and not getattr(args, "no_run_vitis", False):
        config.run_vitis = True
    return config
