#!/usr/bin/env python3
"""Run the Rosetta HLS benchmark suite and report what is verifiable.

Rosetta ships six FPGA applications built for Xilinx SDAccel/SDSoC. The Xilinx-only
headers sit behind ``#ifdef OCL`` / ``#ifdef SDSOC`` guards, so the ``src/sw`` kernel plus
``src/host`` builds and runs with a plain ``g++ -DSW``.

Two modes, and you should run both:

``--sw-baseline`` (default)
    Build and run each app's own software path and judge it against the shipped golden
    output. This is the calibration rung -- the Rosetta counterpart of CHStone's
    ``--native-baseline``. If an app cannot be judged here, nothing downstream means
    anything for it.

``--agent``
    Drive the app's ``src/sw`` kernel through this repo's C -> HLS-C conversion and run
    **host software equivalence** (original kernel vs generated HLS-C, same stimuli). That
    is rung 1 of the repo's four-rung ladder. ``--use-llm`` swaps the deterministic
    generator for the repo's LLM generator; ``--auto-repair`` runs the repair loop and is
    independent of ``--use-llm`` (``hlsc_repair_agent`` applies mechanical repairs with no
    model at all and escalates to an LLM patch only when one is configured).

**Nothing here synthesizes anything.** SDAccel/SDSoC/Vitis are not required and are not
faked; every row in either mode carries ``xilinx_available`` and ``rungs_not_attempted``.

The oracle is the honest part of the software mode. Three apps ship ``outputs_golden.txt``
and their host code writes ``outputs.txt``, so the run can be compared against a golden
result. The rest have no shipped golden output, and this harness reports them as
``no_trustworthy_oracle`` rather than scoring an exit code as a pass -- an exit code proves
the program did not crash, not that it computed anything correct.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROSETTA_URL = "https://github.com/cornell-zhang/rosetta.git"
DEFAULT_TIMEOUT = 3600
EVIDENCE_LIMIT = 4000

#: Rungs needing Xilinx tooling; never attempted by this harness.
XILINX_RUNGS = ("hls_synthesis", "sdaccel", "sdsoc")

#: Ordered rungs the agent mode can reach without any Xilinx tooling.
AGENT_RUNG_ORDER = ("discovered", "analyzed", "generated", "host_equivalence")

#: Rosetta predates the stricter libstdc++ header hygiene of modern GCC and relies on
#: transitive includes that no longer happen. Forcing these in is a pure portability fix --
#: it adds no symbol the sources do not already use. Both modes need it.
PORTABILITY_INCLUDES = ("cstdio", "iostream", "cstring", "cstdlib")

_MAKE_VAR_RE = r"^{name}\s*=\s*(.*?)(?<!\\)$"
#: "\t 1878 / 2000 correct!" -- the accuracy line both outputs.txt and the golden file carry.
_CORRECT_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*correct", re.I)

_print_lock = threading.Lock()
_jsonl_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


@dataclass
class RosettaApp:
    name: str
    directory: Path
    host_sources: tuple[str, ...]
    sw_kernel: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "directory": str(self.directory),
            "host_sources": list(self.host_sources),
            "sw_kernel": self.sw_kernel,
        }


@dataclass
class AppResult:
    app: str
    mode: str
    built: bool = False
    ran: bool = False
    oracle: str = "none"          # golden_file | no_trustworthy_oracle | none
    ok: bool | None = None        # None when no oracle could judge it
    xilinx_available: bool = False
    rungs_not_attempted: tuple[str, ...] = XILINX_RUNGS
    failure_family: str | None = None
    measured: str = ""
    expected: str = ""
    evidence: str = ""
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        payload = dict(self.__dict__)
        payload["rungs_not_attempted"] = list(self.rungs_not_attempted)
        return payload


def _make_var(text: str, name: str) -> list[str]:
    """Read a (possibly line-continued) variable out of a Rosetta Makefile."""

    match = re.search(_MAKE_VAR_RE.format(name=re.escape(name)), text, re.M | re.S)
    if not match:
        return []
    value = match.group(1)
    # follow backslash continuations
    start = match.end()
    while value.rstrip().endswith("\\"):
        nl = text.find("\n", start)
        if nl == -1:
            break
        line_end = text.find("\n", nl + 1)
        chunk = text[nl + 1: line_end if line_end != -1 else len(text)]
        value = value.rstrip().rstrip("\\") + " " + chunk
        start = line_end if line_end != -1 else len(text)
    return [tok for tok in value.replace("\\", " ").split() if tok.endswith((".cpp", ".c"))]


def discover_apps(root: Path, only: tuple[str, ...] = ()) -> list[RosettaApp]:
    apps: list[RosettaApp] = []
    for makefile in sorted(root.glob("*/Makefile")):
        directory = makefile.parent
        if only and directory.name not in only:
            continue
        text = makefile.read_text(encoding="utf-8", errors="replace")
        host = _make_var(text, "HOST_SRC_CPP")
        kernel = _make_var(text, "SW_KERNEL_SRC")
        if not host or not kernel:
            continue
        apps.append(RosettaApp(directory.name, directory, tuple(host), kernel[0]))
    return apps


def _accuracy(text: str) -> str:
    match = _CORRECT_RE.search(text)
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def run_sw(app: RosettaApp, out_dir: Path, timeout: int) -> AppResult:
    """Build and run the app's software path, then judge it against the shipped golden output."""

    started = time.time()
    work = out_dir / "apps" / app.name
    work.mkdir(parents=True, exist_ok=True)
    result = AppResult(app=app.name, mode="sw")
    binary = work / f"{app.name}.bin"

    sources = [str((app.directory / s).resolve()) for s in (*app.host_sources, app.sw_kernel)]
    missing = [s for s in sources if not Path(s).exists()]
    if missing:
        result.failure_family = "missing_source"
        result.evidence = "missing: " + ", ".join(Path(m).name for m in missing)
        result.duration_s = round(time.time() - started, 2)
        return result

    cmd = ["g++", "-w", "-O2", "-std=c++11", "-DSW", "-o", str(binary), *sources,
           # Rosetta predates the stricter libstdc++ header hygiene of modern GCC and relies on
           # transitive includes that no longer happen. Forcing these in is a pure portability
           # fix -- it adds no symbols the sources do not already use.
           "-include", "cstdio", "-include", "iostream", "-include", "cstring", "-include", "cstdlib",
           f"-I{app.directory / 'src' / 'host'}", f"-I{app.directory / 'src' / 'sw'}"]
    imagelib = app.directory / "imageLib"
    if imagelib.exists():
        cmd.append(f"-I{imagelib}")
    try:
        build = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        result.failure_family = "build_error"
        result.evidence = f"g++ failed: {exc}"
        result.duration_s = round(time.time() - started, 2)
        return result
    if build.returncode != 0:
        result.failure_family = "build_error"
        result.evidence = (build.stderr or "")[-EVIDENCE_LIMIT:]
        result.duration_s = round(time.time() - started, 2)
        return result
    result.built = True

    # Run inside a copy of the app directory so data files resolve and outputs.txt is ours.
    # outputs.txt is excluded from the copy on purpose: a checkout that was ever built in
    # place carries one, and judging *that* file would report a PASS for a binary that
    # crashed before writing anything.
    sandbox = work / "run"
    shutil.rmtree(sandbox, ignore_errors=True)
    shutil.copytree(app.directory, sandbox,
                    ignore=shutil.ignore_patterns("src", ".git", "outputs.txt"))
    try:
        run = subprocess.run([str(binary)], capture_output=True, text=True,
                             timeout=timeout, cwd=str(sandbox))
        result.ran = True
        result.evidence = ((run.stdout or "") + (run.stderr or ""))[-EVIDENCE_LIMIT:]
    except subprocess.TimeoutExpired:
        result.failure_family = "run_timeout"
        result.evidence = f"timed out after {timeout}s"
        result.duration_s = round(time.time() - started, 2)
        return result
    except OSError as exc:
        result.failure_family = "run_error"
        result.evidence = str(exc)
        result.duration_s = round(time.time() - started, 2)
        return result

    golden_path = app.directory / "outputs_golden.txt"
    produced_path = sandbox / "outputs.txt"
    if not golden_path.exists():
        result.oracle = "no_trustworthy_oracle"
        result.failure_family = "no_golden_output"
        result.notes.append(
            "app ships no outputs_golden.txt; an exit code alone is not evidence of correctness")
        result.duration_s = round(time.time() - started, 2)
        return result

    golden_text = golden_path.read_text(encoding="utf-8", errors="replace")
    result.expected = _accuracy(golden_text)
    if not produced_path.exists():
        result.oracle = "golden_file"
        result.ok = False
        result.failure_family = "no_output_file"
        result.notes.append("host code did not write outputs.txt, so the golden file cannot judge the run")
        result.duration_s = round(time.time() - started, 2)
        return result

    produced_text = produced_path.read_text(encoding="utf-8", errors="replace")
    result.measured = _accuracy(produced_text)
    result.oracle = "golden_file"
    if result.expected and result.measured:
        result.ok = result.measured == result.expected
        if not result.ok:
            result.failure_family = "accuracy_mismatch"
    else:
        result.ok = produced_text.strip() == golden_text.strip()
        if not result.ok:
            result.failure_family = "golden_diff"
        result.notes.append("no 'N / M correct' line; compared full file contents instead")
    result.duration_s = round(time.time() - started, 2)
    return result


def row_from_dict(payload: dict) -> AppResult | None:
    """Rebuild a row recorded by an earlier run, or None if it is not a usable row.

    ``--resume`` has to fold the rows already on disk back into the report; without them the
    final report covers only the apps this invocation happened to run.
    """

    if not isinstance(payload, dict):
        return None
    names = {f.name for f in dataclasses.fields(AppResult)}
    kwargs = {key: value for key, value in payload.items() if key in names}
    kwargs["rungs_not_attempted"] = tuple(kwargs.get("rungs_not_attempted") or ())
    try:
        return AppResult(**kwargs)
    except TypeError:
        return None


def _summarise(rows: list[AppResult], apps: int, elapsed: float, resumed: int = 0) -> dict:
    judged = [r for r in rows if r.ok is not None]
    families: dict[str, int] = {}
    for row in rows:
        if row.failure_family:
            families[row.failure_family] = families.get(row.failure_family, 0) + 1
    return {
        "mode": "sw",
        "apps": apps,
        "completed": len(rows),
        "resumed": resumed,
        "built": sum(1 for r in rows if r.built),
        "ran": sum(1 for r in rows if r.ran),
        "judged": len(judged),
        "passed": sum(1 for r in judged if r.ok),
        "no_trustworthy_oracle": [r.app for r in rows if r.oracle == "no_trustworthy_oracle"],
        "failure_families": families,
        "xilinx_available": False,
        "rungs_not_attempted": list(XILINX_RUNGS),
        "ladder_note": (
            "Software path only. No HLS synthesis, SDAccel or SDSoC step was attempted and no "
            "claim is made about them. 'passed' is out of the apps with a shipped golden output, "
            "not out of all apps."
        ),
        "elapsed_s": round(elapsed, 1),
    }


def _render_markdown(rows: list[AppResult], summary: dict) -> str:
    lines = ["# Rosetta software-path run — c2hlsc-agent", ""]
    lines.append(f"apps: {summary['apps']} · built: {summary['built']} · ran: {summary['ran']} · "
                 f"judged: {summary['judged']} · **passed: {summary['passed']}/{summary['judged']}** · "
                 f"{summary['elapsed_s']}s")
    lines += ["", "> **Coverage.** " + summary["ladder_note"], ""]
    lines += ["| app | built | ran | oracle | verdict | measured | expected | seconds |",
              "| --- | :-: | :-: | --- | :-: | :-: | :-: | --: |"]
    for row in sorted(rows, key=lambda r: r.app):
        verdict = "-" if row.ok is None else ("PASS" if row.ok else "FAIL")
        lines.append(f"| `{row.app}` | {'Y' if row.built else 'N'} | {'Y' if row.ran else 'N'} | "
                     f"{row.oracle} | {verdict} | {row.measured or '-'} | {row.expected or '-'} | "
                     f"{row.duration_s} |")
    if summary["no_trustworthy_oracle"]:
        lines += ["", "## No trustworthy oracle", "",
                  "These apps ship no `outputs_golden.txt`. They are excluded from the denominator; "
                  "an exit code proves only that the program did not crash:", ""]
        lines += [f"- `{name}`" for name in summary["no_trustworthy_oracle"]]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Agent rung: src/sw kernel -> HLS-C -> host software equivalence
# --------------------------------------------------------------------------- #


#: Every Rosetta Makefile declares the kernel it builds.
_KERNEL_NAME_RE = re.compile(r"^\s*KERNEL_NAME\s*=\s*(\S+)", re.M)
#: The generated equivalence testbench prints this on success.
_EQUIV_PASS_RE = re.compile(r"all\s+\d+\s+tests passed", re.I)


@dataclass
class AgentResult:
    """One app's agent-rung row.

    Field names overlap :class:`AppResult` deliberately (``app``/``ok``/``built``/``ran``/
    ``failure_family``/``duration_s``/``evidence``) so the driver logs and streams both
    modes through one code path, while the agent-only columns (``rung``, ``top``,
    ``walls``) mirror the CHStone harness's schema.
    """

    app: str
    mode: str = "agent"
    top: str = ""
    generator: str = "deterministic"   # deterministic | llm
    rung: str = "discovered"
    ok: bool = False
    built: bool = False               # generated HLS-C + golden testbench compiled
    ran: bool = False                 # the equivalence testbench executed
    oracle: str = "original_sw_kernel"
    xilinx_available: bool = False
    #: Per-phase Vitis status when --run-vitis ran; None means the rung was not attempted.
    csim: str | None = None
    csynth: str | None = None
    cosim: str | None = None
    vitis_ok: bool | None = None
    rungs_not_attempted: tuple[str, ...] = XILINX_RUNGS
    failure_family: str | None = None
    walls: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    measured: str = ""
    expected: str = ""
    evidence: str = ""
    duration_s: float = 0.0
    project_dir: str | None = None
    xilinx_followup_cmd: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        payload = dict(self.__dict__)
        payload["rungs_not_attempted"] = list(self.rungs_not_attempted)
        return payload


def strip_comments(source: str) -> str:
    """Drop block and line comments (the same normalization the analyzer skips)."""

    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//.*", "", source)


def _definition_re(top: str) -> re.Pattern[str]:
    """``c2hlsc_agent.analyze._extract_function``'s own top-function regex, verbatim.

    Kept identical on purpose: it is what the measurements below are measured *against*,
    so it must not drift from the converter it is reporting on.
    """

    return re.compile(
        rf"(?P<ret>[A-Za-z_][\w\s\*\d]*?)\s+{re.escape(top)}\s*\((?P<params>[^;{{}}]*)\)\s*\{{",
        flags=re.S,
    )


def discover_sw_top(app: RosettaApp) -> str | None:
    """The software kernel's top function, taken from the app's own Makefile.

    Every Rosetta Makefile declares ``KERNEL_NAME`` and the software entry point is
    ``<KERNEL_NAME>_sw``, which the ``src/sw`` header also declares. Reading the Makefile
    keeps this harness honest about which function the suite itself calls the kernel --
    the same reason the CHStone harness reads ``hls.tcl`` for the top file instead of
    guessing ``<dir>/<dir>.c``. Falls back to any ``*_sw`` definition in the kernel source.
    """

    makefile = app.directory / "Makefile"
    kernel_path = app.directory / app.sw_kernel
    if not makefile.exists() or not kernel_path.exists():
        return None
    source = strip_comments(kernel_path.read_text(encoding="utf-8", errors="replace"))
    candidates: list[str] = []
    declared = _KERNEL_NAME_RE.search(makefile.read_text(encoding="utf-8", errors="replace"))
    if declared:
        candidates.append(f"{declared.group(1)}_sw")
    candidates += [name for name in re.findall(r"\b(\w+_sw)\s*\(", source) if name not in candidates]
    for name in candidates:
        if _definition_re(name).search(source):
            return name
    return None


def declared_return_type(source: str, top: str) -> str | None:
    """The top's return type read from *comment-stripped* source -- the ground truth.

    ``c2hlsc_agent.analyze`` runs its extraction regex on the raw text, so a ``//`` comment
    sitting directly above the definition is absorbed into the captured return type.
    Re-running the identical regex on stripped text is what that misparse is measured
    against. Nothing here changes the converter; it only names what the converter produced.
    """

    match = _definition_re(top).search(strip_comments(source))
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group("ret")).strip()


def generated_return_type(project: Path, top: str) -> str | None:
    """The return type the converter actually emitted into ``src/hls_top.hpp``."""

    header = project / "src" / "hls_top.hpp"
    if not header.exists():
        return None
    match = re.search(
        rf"^(?P<ret>[^#\n]*?)\s*\b{re.escape(top)}\s*\(",
        header.read_text(encoding="utf-8", errors="replace"),
        re.M,
    )
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group("ret")).strip()


def _quote(log_text: str, pattern: re.Pattern[str]) -> str:
    """First log line matching ``pattern``, so a family is always backed by real evidence."""

    for line in log_text.splitlines():
        if pattern.search(line):
            return line.strip()
    return ""


_MULTIDIM_RE = re.compile(r"cannot convert .+ to .+\(\*\)\[")
_STRUCT_STIMULUS_RE = re.compile(r"no matching function for call to .(?P<t>\w+)::(?P=t)\(")
_HEADER_TYPE_RE = re.compile(r"hls_top\.hpp:\d+:\d+: error: .*(was not declared in this scope|declared void)")
_ANY_ERROR_RE = re.compile(r"\berror:")


def detect_walls(log_text: str, project: Path, top: str, kernel_source: str) -> list[tuple[str, str]]:
    """Every distinct wall the run hit, in the order they block the build.

    Reporting all of them, not only the first, is the point: an app can stop on several
    independent limitations at once, and a report naming only the first would suggest that
    fixing it is enough to move the app. The counts are still a floor -- an error in
    ``src/hls_top.hpp`` aborts that translation unit and can hide later walls.
    """

    walls: list[tuple[str, str]] = []
    emitted = generated_return_type(project, top)
    declared = declared_return_type(kernel_source, top)
    if emitted is not None and declared is not None and emitted != declared:
        stray = _quote(log_text, re.compile(r"does not name a type"))
        walls.append((
            "top_signature_misparsed",
            f"converter emitted return type {emitted!r} for a top declared {declared!r}"
            + (f"; {stray}" if stray else ""),
        ))
    for family, pattern in (
        ("multidim_array_arg_unsupported", _MULTIDIM_RE),
        ("struct_arg_stimulus_unsupported", _STRUCT_STIMULUS_RE),
        ("generated_header_missing_app_types", _HEADER_TYPE_RE),
    ):
        quoted = _quote(log_text, pattern)
        if quoted:
            walls.append((family, quoted))
    if not walls and _ANY_ERROR_RE.search(log_text):
        walls.append(("generated_hlsc_does_not_compile", _quote(log_text, _ANY_ERROR_RE)))
    return walls


def classify_agent(report: dict, log_text: str, project: Path, top: str, kernel_source: str
                   ) -> tuple[str, str | None, list[tuple[str, str]]]:
    """Map the host-equivalence log (authoritative) onto (rung, failure family, walls).

    The log wins over ``conversion_report.json``'s phase field, matching the CHStone
    harness: the report records the phase as the converter saw it and can be stale after a
    repair iteration.
    """

    if _EQUIV_PASS_RE.search(log_text):
        return "host_equivalence", None, []
    phases = report.get("phases") or {}
    software = str(phases.get("software_equivalence", {}).get("status", "")
                   if isinstance(phases.get("software_equivalence"), dict)
                   else phases.get("software_equivalence", "")).lower()
    if software == "pass" and "error:" not in log_text.lower():
        return "host_equivalence", None, []

    walls = detect_walls(log_text, project, top, kernel_source)
    # The equivalence log is written only once `make test` actually ran. Without it the run
    # never left static analysis, so it must not be scored as "generated and then failed" --
    # under --strict-diagnostics the project files exist but nothing was ever compiled.
    if not (project / "software_equivalence.log").exists():
        diagnostics = report.get("diagnostics") or []
        blocked = any(str(d.get("severity", "")).lower() == "error"
                      for d in diagnostics if isinstance(d, dict))
        if not (project / "src" / "hls_top.cpp").exists():
            return "discovered", "conversion_produced_no_project", walls
        return "analyzed", "static_source_rejected" if blocked else "host_equivalence_not_run", walls
    if not walls:
        # It compiled and ran, but the two implementations disagreed.
        return "generated", "host_behavior_mismatch", []
    return "generated", walls[0][0], walls


def render_agent_config(app: RosettaApp) -> str:
    """The ``--config`` the conversion runs under.

    ``-I src/sw -I src/host`` is what lets the copied ``input.c`` resolve its own quoted
    includes (``"sgd_sw.h"`` and, through it, ``"../host/typedefs.h"``) from the project
    directory, so the harness never has to stage or rewrite Rosetta's sources. ``-DSW``
    selects the plain-C++ typedefs over the ``ap_int``/``ap_fixed`` ones, which is the
    whole reason the software kernel is buildable off a Xilinx machine.
    """

    flags = [
        "-DSW",
        "-w",
        f"-I{app.directory / 'src' / 'sw'}",
        f"-I{app.directory / 'src' / 'host'}",
    ]
    imagelib = app.directory / "imageLib"
    if imagelib.exists():
        flags.append(f"-I{imagelib}")
    for header in PORTABILITY_INCLUDES:
        flags += ["-include", header]
    lines = [
        "# Generated by scripts/run_rosetta.py for the Rosetta agent rung.",
        "compiler_flags:",
        *[f"  - {json.dumps(flag)}" for flag in flags],
    ]
    return "\n".join(lines) + "\n"


def run_agent(app: RosettaApp, out_dir: Path, args: argparse.Namespace) -> AgentResult:
    """Convert the app's src/sw kernel to HLS-C and run host software equivalence (rung 1)."""

    started = time.time()
    result = AgentResult(app=app.name, generator="llm" if args.use_llm else "deterministic")
    # Absolute: the conversion subprocess runs from the repo root, so a relative --out-dir
    # would otherwise land the project somewhere other than where the caller asked.
    work = (out_dir / "apps" / app.name).resolve()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    kernel = (app.directory / app.sw_kernel).resolve()
    if not kernel.exists():
        result.failure_family = "missing_source"
        result.evidence = f"sw kernel not found: {kernel}"
        result.duration_s = round(time.time() - started, 2)
        return result
    top = discover_sw_top(app)
    if not top:
        result.failure_family = "sw_top_not_found"
        result.evidence = (f"no <KERNEL_NAME>_sw definition found in {app.sw_kernel}; "
                           "the Makefile's KERNEL_NAME did not resolve to a kernel entry point")
        result.duration_s = round(time.time() - started, 2)
        return result
    result.top = top
    kernel_source = kernel.read_text(encoding="utf-8", errors="replace")

    config_path = work / "config.yaml"
    config_path.write_text(render_agent_config(app), encoding="utf-8")
    project = work / "project"
    result.project_dir = str(project)

    cmd = [
        sys.executable, "-m", "c2hlsc_agent.cli", "convert",
        "--input", str(kernel),
        "--top", top,
        "--out", str(project),
        "--config", str(config_path),
        "--run-vitis" if getattr(args, "run_vitis", False) else "--no-run-vitis",
    ]
    if args.keep_going:
        cmd.append("--keep-going")
    if args.use_llm:
        cmd.append("--use-llm")
        for flag, value in (("--llm-backend", args.llm_backend),
                            ("--llm-model", args.llm_model),
                            ("--llm-cli-cmd", args.llm_cli_cmd)):
            if value:
                cmd += [flag, value]
    else:
        cmd.append("--no-llm")
    # --auto-repair is independent of --use-llm: hlsc_repair_agent applies deterministic
    # mechanical repairs (missing includes, helper-source inclusion, restrict compatibility)
    # with no model at all, and escalates to an LLM patch only when one is configured.
    if args.auto_repair:
        cmd += ["--auto-repair", "--max-iterations", str(args.max_iterations)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout,
                              cwd=str(Path(__file__).resolve().parent.parent))
        combined = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        result.failure_family = "conversion_timeout"
        result.evidence = f"convert timed out after {args.timeout}s"
        result.duration_s = round(time.time() - started, 2)
        return result
    except OSError as exc:
        result.failure_family = "conversion_error"
        result.evidence = str(exc)
        result.duration_s = round(time.time() - started, 2)
        return result

    equivalence_log = project / "software_equivalence.log"
    log_text = (equivalence_log.read_text(encoding="utf-8", errors="replace")
                if equivalence_log.exists() else combined)
    report: dict = {}
    report_path = project / "conversion_report.json"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}

    if getattr(args, "run_vitis", False):
        for phase in ("csim", "csynth", "cosim"):
            status = report.get(phase)
            setattr(result, phase, str(status) if status is not None else None)
        result.xilinx_available = result.csim is not None
        result.vitis_ok = all(
            getattr(result, phase) == "pass" for phase in ("csim", "csynth", "cosim")
        )

    rung, family, walls = classify_agent(report, log_text, project, top, kernel_source)
    result.rung = rung
    result.ok = rung == "host_equivalence"
    result.failure_family = None if result.ok else family
    result.walls = [name for name, _quoted in walls]
    result.built = rung == "host_equivalence" or (rung == "generated" and not walls)
    result.ran = result.built
    result.diagnostics = [
        f"[{d.get('severity')}] {d.get('code')}: {d.get('message')}"
        for d in (report.get("diagnostics") or []) if isinstance(d, dict)
    ]
    result.notes += [f"{name}: {quoted}" for name, quoted in walls]
    if result.ok:
        match = _EQUIV_PASS_RE.search(log_text)
        result.measured = match.group(0) if match else "host equivalence passed"
        # A pass here is host equivalence only, and only over the stimulus the generated
        # testbench could build; it is not a synthesized, cosimulated design.
        result.notes.append(
            "host software equivalence only (rung 1 of 4); no HLS synthesis was attempted")
    result.evidence = log_text[-EVIDENCE_LIMIT:]
    result.xilinx_followup_cmd = (
        f"python3 -m c2hlsc_agent.cli convert --input {kernel} --top {top} "
        f"--out {project} --config {config_path} --vitis-ssh USER@VITIS_HOST --keep-going"
    )
    result.duration_s = round(time.time() - started, 2)
    return result


def agent_row_from_dict(payload: dict) -> AgentResult | None:
    """Rebuild an agent row recorded by an earlier run (the ``--resume`` counterpart)."""

    if not isinstance(payload, dict):
        return None
    names = {f.name for f in dataclasses.fields(AgentResult)}
    kwargs = {key: value for key, value in payload.items() if key in names}
    kwargs["rungs_not_attempted"] = tuple(kwargs.get("rungs_not_attempted") or ())
    try:
        return AgentResult(**kwargs)
    except TypeError:
        return None


def _summarise_agent(rows: list[AgentResult], apps: int, elapsed: float, resumed: int = 0) -> dict:
    reached: dict[str, int] = {}
    families: dict[str, int] = {}
    walls: dict[str, int] = {}
    for row in rows:
        reached[row.rung] = reached.get(row.rung, 0) + 1
        if row.failure_family:
            families[row.failure_family] = families.get(row.failure_family, 0) + 1
        for wall in row.walls:
            walls[wall] = walls.get(wall, 0) + 1
    generators = sorted({row.generator for row in rows}) or ["deterministic"]
    return {
        "mode": "agent",
        "generator": "+".join(generators),
        "apps": apps,
        "completed": len(rows),
        "resumed": resumed,
        "passed": sum(1 for r in rows if r.ok),
        "rung_reached": reached,
        "failure_families": families,
        "walls": walls,
        "xilinx_available": False,
        "rungs_not_attempted": list(XILINX_RUNGS),
        "ladder_note": (
            "Only host software equivalence (rung 1 of 4) can run without Xilinx tooling. "
            "HLS synthesis, SDAccel and SDSoC were NOT attempted and no claim is made about "
            "them. 'passed' means the generated HLS-C matched the original src/sw kernel on "
            "the generated stimulus -- not that anything was synthesized."
        ),
        "elapsed_s": round(elapsed, 1),
    }


def _render_agent_markdown(rows: list[AgentResult], summary: dict) -> str:
    lines = ["# Rosetta agent-rung run — c2hlsc-agent", ""]
    lines.append(f"Mode: `agent` · generator: `{summary['generator']}` · apps: {summary['apps']} · "
                 f"passed: **{summary['passed']}/{summary['completed']}** · {summary['elapsed_s']}s")
    lines += ["", "> **Ladder coverage.** " + summary["ladder_note"], ""]
    lines += ["| app | top | rung reached | ok | failure family | seconds |",
              "| --- | --- | --- | :-: | --- | --: |"]
    for row in sorted(rows, key=lambda r: r.app):
        lines.append(f"| `{row.app}` | `{row.top or '-'}` | {row.rung} | "
                     f"{'PASS' if row.ok else 'FAIL'} | {row.failure_family or '-'} | {row.duration_s} |")
    if summary["failure_families"]:
        lines += ["", "## Failure families (first blocking wall per app)", "",
                  "| family | n |", "| --- | :-: |"]
        for family, count in sorted(summary["failure_families"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{family}` | {count} |")
    if summary["walls"]:
        lines += ["", "## Every wall the compiler reported", "",
                  "Apps commonly stop on more than one independent limitation, so fixing only the "
                  "first would not move them. Read these counts as a floor: an error in "
                  "`src/hls_top.hpp` aborts that translation unit, which can mask further walls "
                  "in the same app.", "", "| wall | n |", "| --- | :-: |"]
        for wall, count in sorted(summary["walls"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{wall}` | {count} |")
    quoted = [(row.app, note) for row in sorted(rows, key=lambda r: r.app) for note in row.notes
              if ": " in note and not note.startswith("host software equivalence")]
    if quoted:
        lines += ["", "## Diagnostics, quoted", ""]
        for app, note in quoted:
            lines.append(f"- `{app}` — {note}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_rosetta.py",
        description="Run Rosetta's software path, or drive its src/sw kernels through the "
                    "c2hlsc conversion agent and check host equivalence. No Xilinx tooling is "
                    "used and none of the HLS rungs are faked.",
    )
    parser.add_argument("--benchmark", required=True, type=Path, help="Path to a Rosetta checkout")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--apps", nargs="+", default=[], metavar="NAME")
    parser.add_argument("--workers", type=int, default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sw-baseline", action="store_true",
                      help="Build and run the software path and judge it against the shipped "
                           "golden output (the calibration rung; the default)")
    mode.add_argument("--agent", action="store_true",
                      help="Convert each app's src/sw kernel to HLS-C and run host software "
                           "equivalence (ladder rung 1). Implied by --use-llm/--auto-repair.")
    parser.add_argument("--keep-going", action="store_true", default=True,
                        help="Emit the project even when static diagnostics contain errors "
                             "(default: on; Rosetta trips the analyzer's variable-length-array "
                             "check on named compile-time constants such as K_CONST)")
    parser.add_argument("--strict-diagnostics", dest="keep_going", action="store_false",
                        help="Stop at static analysis instead of pushing past those diagnostics")
    parser.add_argument("--use-llm", action="store_true",
                        help="use the repo's LLM HLS-C generator instead of the deterministic one")
    parser.add_argument("--llm-backend")
    parser.add_argument("--llm-model")
    parser.add_argument("--llm-cli-cmd")
    parser.add_argument("--auto-repair", action="store_true",
                        help="run hlsc_repair_agent between verification attempts (works without "
                             "--use-llm: the mechanical repairs need no model)")
    parser.add_argument("--max-iterations", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--run-vitis",
        action="store_true",
        help="continue past host equivalence into Vitis CSim/CSynth/CoSim. The convert "
             "command repairs against the failing phase. Requires vitis_hls on PATH or "
             "C2HLSC_VITIS_BIN.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.benchmark
    if not root.exists():
        raise SystemExit(f"Rosetta checkout not found: {root} (clone {ROSETTA_URL})")
    apps = discover_apps(root, tuple(args.apps))
    if not apps:
        raise SystemExit(f"no Rosetta apps found under {root}")

    # Agent-only flags imply the agent rung, so they can never be silently ignored;
    # everything else keeps the software baseline as the default mode.
    agent_mode = bool(args.agent or ((args.use_llm or args.auto_repair) and not args.sw_baseline))
    mode = "agent" if agent_mode else "sw"

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    rebuild_row = agent_row_from_dict if agent_mode else row_from_dict
    done: set[str] = set()
    prior: dict[str, AppResult | AgentResult] = {}
    if args.resume and results_path.exists():
        wanted = {a.name for a in apps}
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
                name = payload["app"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            done.add(name)
            row = rebuild_row(payload)
            # Rows for apps outside this selection stay in the file but out of the report;
            # the last row for an app wins if it was recorded more than once.
            if row is not None and name in wanted:
                prior[name] = row
    elif results_path.exists() and results_path.stat().st_size:
        # Keep the previous sweep recoverable and stop this run's rows from being appended
        # to it: a mixed file makes a later --resume skip everything.
        os.replace(results_path, results_path.with_name(results_path.name + ".prev"))
    todo = [a for a in apps if a.name not in done]
    log(f"Rosetta [{mode}]: {len(apps)} apps, {len(todo)} to run, {args.workers} worker(s)")
    if agent_mode:
        log(f"generator: {'llm' if args.use_llm else 'deterministic'}"
            f"{', repair on' if args.auto_repair else ', no repair'}")
    if args.resume:
        log(f"resume: {len(apps) - len(todo)} apps already done, {len(todo)} to run")
    log("note: no Xilinx tooling is used; HLS synthesis / SDAccel / SDSoC are NOT attempted")
    started = time.time()

    def run_one(app: RosettaApp) -> AppResult | AgentResult:
        row = run_agent(app, out_dir, args) if agent_mode else run_sw(app, out_dir, args.timeout)
        verdict = "-" if row.ok is None else ("PASS" if row.ok else "FAIL")
        if agent_mode:
            log(f"[{app.name:<18}] rung={row.rung:<17} {verdict:<5} "
                f"{row.failure_family or '':<36} {row.duration_s}s")
        else:
            log(f"[{app.name:<18}] built={'Y' if row.built else 'N'} ran={'Y' if row.ran else 'N'} "
                f"oracle={row.oracle:<22} {verdict:<5} {row.measured or ''} "
                f"{row.failure_family or ''} {row.duration_s}s")
        if args.verbose and row.evidence and row.ok is not True:
            tail = [ln for ln in row.evidence.strip().splitlines() if ln.strip()][-2:]
            for line in tail:
                log("    " + line[:170])
        with _jsonl_lock, results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row.to_dict(), sort_keys=True) + "\n")
        return row

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        fresh = list(pool.map(run_one, todo))

    # The report covers the whole suite, not just what this invocation ran.
    rows = sorted([*prior.values(), *fresh], key=lambda r: r.app)
    elapsed = time.time() - started
    if agent_mode:
        summary = _summarise_agent(rows, len(apps), elapsed, resumed=len(prior))
        markdown = _render_agent_markdown(rows, summary)
    else:
        summary = _summarise(rows, len(apps), elapsed, resumed=len(prior))
        markdown = _render_markdown(rows, summary)
    (out_dir / "report.json").write_text(
        json.dumps({**summary, "results": [r.to_dict() for r in rows]}, indent=2, sort_keys=True),
        encoding="utf-8")
    (out_dir / "report.md").write_text(markdown, encoding="utf-8")
    if agent_mode:
        log(f"\nagent: {summary['passed']}/{summary['completed']} reached host equivalence; "
            f"rungs {summary['rung_reached']}")
        if summary["walls"]:
            log("walls: " + ", ".join(f"{k}={v}" for k, v in sorted(summary["walls"].items())))
    else:
        log(f"\nsw: built {summary['built']}/{summary['completed']}, ran {summary['ran']}, "
            f"passed {summary['passed']}/{summary['judged']} judged")
        if summary["no_trustworthy_oracle"]:
            log("no trustworthy oracle (excluded): " + ", ".join(summary["no_trustworthy_oracle"]))
    log(f"report: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
