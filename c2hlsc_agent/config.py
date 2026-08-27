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
    max_wall_seconds: int = 14_400
    max_llm_calls: int = 8
    max_vitis_runs: int = 8
    run_id: str | None = None
    auto_repair: bool = False
    keep_going: bool = False
    run_vitis: bool = False
    use_llm: bool = False
    llm_backend: str = "auto"
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_cli_cmd: str = "claude"
    llm_candidates: int = 1
    nl_spec: str | None = None
    use_failure_analyst: bool = True
    use_repair_memory: bool = True
    memory_dir: str | None = None
    propose_contract: bool = False
    tb_augment: bool = False
    augmented_vectors: list = field(default_factory=list)
    vitis_ssh_host: str | None = None
    vitis_remote_dir: str = "~/c2hlsc_runs"
    vitis_setup: str | None = None
    vitis_bin: str = "vitis_hls"


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return {}
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
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


def _argument_config(data: Any) -> ArgumentConfig:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("argument metadata must be a mapping")
    range_value = data.get("range")
    parsed_range = None
    if isinstance(range_value, (list, tuple)) and len(range_value) == 2:
        parsed_range = (int(range_value[0]), int(range_value[1]))
    return ArgumentConfig(
        direction=data.get("direction"),
        length=int(data["length"]) if data.get("length") is not None else None,
        range=parsed_range,
        interface=data.get("interface"),
    )


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
        max_wall_seconds=int(data.get('max_wall_seconds', 14_400)),
        max_llm_calls=int(data.get('max_llm_calls', 8)),
        max_vitis_runs=int(data.get('max_vitis_runs', 8)),
        run_id=str(data['run_id']) if data.get('run_id') else None,
        num_tests=int(data.get("num_tests", data.get("random_test_count", 100))),
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
        llm_backend=str(data.get("llm_backend", "auto")),
        llm_model=(str(data["llm_model"]) if data.get("llm_model") is not None else None),
        llm_base_url=(str(data["llm_base_url"]) if data.get("llm_base_url") is not None else None),
        llm_cli_cmd=str(data.get("llm_cli_cmd", "claude")),
        llm_candidates=int(data.get("llm_candidates", 1)),
        nl_spec=(str(data["nl_spec"]) if data.get("nl_spec") else None),
        vitis_ssh_host=(str(data["vitis_ssh_host"]) if data.get("vitis_ssh_host") else None),
        vitis_remote_dir=str(data.get("vitis_remote_dir", "~/c2hlsc_runs")),
        vitis_setup=(str(data["vitis_setup"]) if data.get("vitis_setup") else None),
        vitis_bin=str(data.get("vitis_bin", "vitis_hls")),
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
        config.num_tests = int(args.num_tests)
    if getattr(args, "cosim_tool", None):
        config.cosim_tool = args.cosim_tool
    if getattr(args, "rtl", None):
        config.rtl = args.rtl
    if getattr(args, "seed", None) is not None:
        config.seed = int(args.seed)
    if getattr(args, "max_iterations", None) is not None:
        config.max_iterations = int(args.max_iterations)
    if getattr(args, 'max_wall_seconds', None) is not None:
        config.max_wall_seconds = int(args.max_wall_seconds)
    if getattr(args, 'max_llm_calls', None) is not None:
        config.max_llm_calls = int(args.max_llm_calls)
    if getattr(args, 'max_vitis_runs', None) is not None:
        config.max_vitis_runs = int(args.max_vitis_runs)
    if getattr(args, 'run_id', None):
        config.run_id = str(args.run_id)
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
    if getattr(args, "no_failure_analyst", False):
        config.use_failure_analyst = False
    if getattr(args, "no_repair_memory", False):
        config.use_repair_memory = False
    if getattr(args, "memory_dir", None):
        config.memory_dir = args.memory_dir
    if getattr(args, "propose_contract", False):
        config.propose_contract = True
    if getattr(args, "tb_augment", False):
        config.tb_augment = True
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
    # Fold in the env var here (not only in RemoteVitis.from_config) so the
    # "remote host implies --run-vitis" rule below applies to it too.
    config.vitis_ssh_host = config.vitis_ssh_host or os.environ.get("C2HLSC_VITIS_SSH")
    if config.vitis_ssh_host and not getattr(args, "no_run_vitis", False):
        config.run_vitis = True
    return config
