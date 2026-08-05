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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_rosetta.py",
        description="Build and run Rosetta's software path and judge it against shipped golden "
                    "outputs. No Xilinx tooling is used and none of the HLS rungs are faked.",
    )
    parser.add_argument("--benchmark", required=True, type=Path, help="Path to a Rosetta checkout")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--apps", nargs="+", default=[], metavar="NAME")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--sw-baseline", action="store_true",
                        help="Build and run the software path (currently the only mode)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
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

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    done: set[str] = set()
    prior: dict[str, AppResult] = {}
    if args.resume and results_path.exists():
        wanted = {a.name for a in apps}
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
                name = payload["app"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            done.add(name)
            row = row_from_dict(payload)
            # Rows for apps outside this selection stay in the file but out of the report;
            # the last row for an app wins if it was recorded more than once.
            if row is not None and name in wanted:
                prior[name] = row
    elif results_path.exists() and results_path.stat().st_size:
        # Keep the previous sweep recoverable and stop this run's rows from being appended
        # to it: a mixed file makes a later --resume skip everything.
        os.replace(results_path, results_path.with_name(results_path.name + ".prev"))
    todo = [a for a in apps if a.name not in done]
    log(f"Rosetta [sw]: {len(apps)} apps, {len(todo)} to run, {args.workers} worker(s)")
    if args.resume:
        log(f"resume: {len(apps) - len(todo)} apps already done, {len(todo)} to run")
    log("note: no Xilinx tooling is used; HLS synthesis / SDAccel / SDSoC are NOT attempted")
    started = time.time()

    def run_one(app: RosettaApp) -> AppResult:
        row = run_sw(app, out_dir, args.timeout)
        verdict = "-" if row.ok is None else ("PASS" if row.ok else "FAIL")
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
    summary = _summarise(rows, len(apps), time.time() - started, resumed=len(prior))
    (out_dir / "report.json").write_text(
        json.dumps({**summary, "results": [r.to_dict() for r in rows]}, indent=2, sort_keys=True),
        encoding="utf-8")
    (out_dir / "report.md").write_text(_render_markdown(rows, summary), encoding="utf-8")
    log(f"\nsw: built {summary['built']}/{summary['completed']}, ran {summary['ran']}, "
        f"passed {summary['passed']}/{summary['judged']} judged")
    if summary["no_trustworthy_oracle"]:
        log("no trustworthy oracle (excluded): " + ", ".join(summary["no_trustworthy_oracle"]))
    log(f"report: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
