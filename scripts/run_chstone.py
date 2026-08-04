#!/usr/bin/env python3
"""Run the CHStone C-based HLS benchmark suite through this repo's conversion agent.

CHStone ships 12 self-checking C programs. Each benchmark's own ``hls.tcl`` compiles one
top file with ``-Dmain=chstone_main`` and sets ``chstone_main`` as the HLS top, so the
"kernel" is the whole benchmark's ``main``: zero arguments, returns 0 on success.

Two modes, and you should run both:

``--native-baseline``
    Compile and run the benchmark's own C with gcc. ``rc == 0`` means the suite's own
    self-check passed. This is the calibration rung -- the CHStone equivalent of the
    RTLLM ``--reference`` run. If a benchmark fails here, nothing downstream means
    anything for it.

agent mode (default)
    Drive this repo's C -> HLS-C conversion on the benchmark and run **host software
    equivalence** (original C vs generated HLS-C, same stimuli). That is rung 1 of the
    repo's four-rung ladder.

**The Vitis rungs are not attempted.** CSim, CSynth and C/RTL CoSim need ``vitis_hls``,
which this harness does not require and does not fake. Every result row carries
``vitis_available`` and ``rungs_not_attempted`` so a host-equivalence pass can never be
misread as a synthesized or cosimulated design. Each row also carries the exact
``--vitis-ssh`` command that finishes the ladder on a machine that has Vitis.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

CHSTONE_URL = "https://github.com/ferrandi/CHStone.git"
CHSTONE_TOP_FUNCTION = "chstone_main"
DEFAULT_TIMEOUT = 1800
DEFAULT_NATIVE_TIMEOUT = 300
EVIDENCE_LIMIT = 4000

#: ``hls.tcl`` line that names the file holding ``main``.
_ADD_FILES_RE = re.compile(r"^\s*add_files\s+(?!-tb)(\S+\.c)\b", re.M)
#: CHStone declares every top as K&R ``int\nmain ()``; rename the identifier only, so the
#: return type on the preceding line is left alone (prepending one yields ``int int``).
_MAIN_DEF_RE = re.compile(r"(?m)^(\s*)main\s*\(")

#: Rungs of the repo's verifier ladder that need Vitis and are therefore never attempted here.
VITIS_RUNGS = ("csim", "csynth", "cosim")

#: Ordered rungs this harness can reach.
RUNG_ORDER = ("discovered", "native_pass", "analyzed", "generated", "host_equivalence")


@dataclass
class ChstoneBenchmark:
    name: str
    directory: Path
    top_file: Path

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "directory": str(self.directory), "top_file": self.top_file.name}


@dataclass
class BenchmarkResult:
    benchmark: str
    mode: str
    rung: str
    ok: bool
    vitis_available: bool = False
    rungs_not_attempted: tuple[str, ...] = VITIS_RUNGS
    failure_family: str | None = None
    diagnostics: list[str] = field(default_factory=list)
    evidence: str = ""
    returncode: int | None = None
    stdout_head: str = ""
    duration_s: float = 0.0
    project_dir: str | None = None
    vitis_followup_cmd: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = dict(self.__dict__)
        payload["rungs_not_attempted"] = list(self.rungs_not_attempted)
        return payload


_print_lock = threading.Lock()
_jsonl_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def discover_benchmarks(root: Path, only: tuple[str, ...] = ()) -> list[ChstoneBenchmark]:
    """Find CHStone benchmarks, taking each top file from the benchmark's own hls.tcl.

    The top file is *not* always ``<dir>/<dir>.c`` -- jpeg uses ``main.c``, motion uses
    ``mpeg2.c`` and sha uses ``sha_driver.c``. Reading hls.tcl keeps this harness honest
    about which source the suite itself considers the kernel.
    """

    found: list[ChstoneBenchmark] = []
    for tcl in sorted(root.glob("*/hls.tcl")):
        directory = tcl.parent
        if only and directory.name not in only:
            continue
        match = _ADD_FILES_RE.search(tcl.read_text(encoding="utf-8", errors="replace"))
        if not match:
            continue
        top_file = directory / match.group(1)
        if not top_file.exists():
            continue
        found.append(ChstoneBenchmark(directory.name, directory, top_file))
    return found


def rename_main(source: str) -> tuple[str, int]:
    """Rename the K&R ``main ()`` definition to ``chstone_main``, matching hls.tcl's -Dmain."""

    return _MAIN_DEF_RE.subn(r"\1" + CHSTONE_TOP_FUNCTION + " (", source)


# --------------------------------------------------------------------------- #
# Native baseline
# --------------------------------------------------------------------------- #


def run_native(bench: ChstoneBenchmark, workdir: Path, timeout: int) -> BenchmarkResult:
    """Compile and run the benchmark's own C. rc == 0 means its self-check passed."""

    started = time.time()
    workdir.mkdir(parents=True, exist_ok=True)
    binary = workdir / f"{bench.name}.bin"
    result = BenchmarkResult(benchmark=bench.name, mode="native", rung="discovered", ok=False)
    try:
        build = subprocess.run(
            ["gcc", "-w", "-O1", f"-I{bench.directory}", "-o", str(binary), str(bench.top_file), "-lm"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        result.failure_family = "native_build_error"
        result.evidence = f"gcc failed: {exc}"
        result.duration_s = round(time.time() - started, 2)
        return result
    if build.returncode != 0:
        result.failure_family = "native_build_error"
        result.evidence = (build.stderr or "")[-EVIDENCE_LIMIT:]
        result.duration_s = round(time.time() - started, 2)
        return result
    try:
        run = subprocess.run([str(binary)], capture_output=True, text=True,
                             timeout=timeout, cwd=str(bench.directory))
        result.returncode = run.returncode
        result.stdout_head = (run.stdout or "")[:400]
        result.ok = run.returncode == 0
        result.rung = "native_pass" if result.ok else "discovered"
        if not result.ok:
            result.failure_family = "native_selfcheck_failed"
            result.evidence = ((run.stdout or "") + (run.stderr or ""))[-EVIDENCE_LIMIT:]
    except subprocess.TimeoutExpired:
        result.failure_family = "native_timeout"
        result.evidence = f"timed out after {timeout}s"
    except OSError as exc:
        result.failure_family = "native_run_error"
        result.evidence = str(exc)
    result.duration_s = round(time.time() - started, 2)
    return result


# --------------------------------------------------------------------------- #
# Agent rung
# --------------------------------------------------------------------------- #


def _classify_conversion(report: dict, log_text: str) -> tuple[str, str | None]:
    """Map a conversion_report.json onto (rung reached, failure family)."""

    phases = report.get("phases") or {}
    software = str(phases.get("software_equivalence", "")).lower()
    if software == "pass":
        return "host_equivalence", None
    assessment = report.get("multi_agent") or report.get("assessment") or {}
    family = assessment.get("failure_family") if isinstance(assessment, dict) else None
    diagnostics = report.get("diagnostics") or []
    if any(str(d.get("severity", "")).lower() == "error" for d in diagnostics if isinstance(d, dict)):
        if software in {"", "skipped"}:
            return "analyzed", family or "static_source_rejected"
    if software in {"", "skipped"}:
        return "analyzed", family or "static_source_rejected"
    lowered = log_text.lower()
    if "was not declared in this scope" in lowered or "error:" in lowered:
        return "generated", family or "generated_hlsc_does_not_compile"
    return "generated", family or "host_behavior_mismatch"


def run_agent(bench: ChstoneBenchmark, out_dir: Path, args: argparse.Namespace) -> BenchmarkResult:
    """Convert the benchmark to HLS-C and run host software equivalence (ladder rung 1)."""

    started = time.time()
    work = out_dir / "benchmarks" / bench.name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # Copy the whole benchmark directory so the top file's #includes still resolve.
    staged = work / "src_copy"
    shutil.copytree(bench.directory, staged)
    top_copy = staged / bench.top_file.name
    renamed, count = rename_main(top_copy.read_text(encoding="utf-8", errors="replace"))
    result = BenchmarkResult(benchmark=bench.name, mode="agent", rung="discovered", ok=False)
    if count != 1:
        result.failure_family = "main_rename_failed"
        result.evidence = f"expected exactly one main definition, renamed {count}"
        result.duration_s = round(time.time() - started, 2)
        return result
    top_copy.write_text(renamed, encoding="utf-8")

    project = work / "project"
    cmd = [
        sys.executable, "-m", "c2hlsc_agent.cli", "convert",
        "--input", str(top_copy),
        "--top", CHSTONE_TOP_FUNCTION,
        "--out", str(project),
        "--no-run-vitis",
    ]
    cmd.append("--keep-going" if args.keep_going else "--no-llm")
    if args.keep_going and not args.use_llm:
        cmd.append("--no-llm")
    if args.use_llm:
        cmd.append("--use-llm")
        if args.llm_backend:
            cmd += ["--llm-backend", args.llm_backend]
        if args.llm_model:
            cmd += ["--llm-model", args.llm_model]
        if args.llm_cli_cmd:
            cmd += ["--llm-cli-cmd", args.llm_cli_cmd]
        if args.auto_repair:
            cmd += ["--auto-repair", "--max-iterations", str(args.max_iterations)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout,
                              cwd=str(Path(__file__).resolve().parent.parent))
        combined = ((proc.stdout or "") + (proc.stderr or ""))
    except subprocess.TimeoutExpired as exc:
        result.failure_family = "conversion_timeout"
        result.evidence = f"convert timed out after {args.timeout}s"
        result.duration_s = round(time.time() - started, 2)
        result.project_dir = str(project)
        del exc
        return result
    except OSError as exc:
        result.failure_family = "conversion_error"
        result.evidence = str(exc)
        result.duration_s = round(time.time() - started, 2)
        return result

    report_path = project / "conversion_report.json"
    equivalence_log = project / "software_equivalence.log"
    log_text = equivalence_log.read_text(encoding="utf-8", errors="replace") if equivalence_log.exists() else combined
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}
    else:
        report = {}

    rung, family = _classify_conversion(report, log_text)
    result.rung = rung
    result.ok = rung == "host_equivalence"
    result.failure_family = None if result.ok else family
    result.diagnostics = [
        f"[{d.get('severity')}] {d.get('code')}: {d.get('message')}"
        for d in (report.get("diagnostics") or []) if isinstance(d, dict)
    ]
    result.evidence = log_text[-EVIDENCE_LIMIT:]
    result.project_dir = str(project)
    result.vitis_followup_cmd = (
        f"python3 -m c2hlsc_agent.cli convert --input {top_copy} --top {CHSTONE_TOP_FUNCTION} "
        f"--out {project} --vitis-ssh USER@VITIS_HOST --keep-going"
    )
    result.duration_s = round(time.time() - started, 2)
    return result


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def _summarise(rows: list[BenchmarkResult], mode: str, elapsed: float, benchmarks: int) -> dict:
    reached: dict[str, int] = {}
    families: dict[str, int] = {}
    for row in rows:
        reached[row.rung] = reached.get(row.rung, 0) + 1
        if row.failure_family:
            families[row.failure_family] = families.get(row.failure_family, 0) + 1
    return {
        "mode": mode,
        "benchmarks": benchmarks,
        "completed": len(rows),
        "passed": sum(1 for r in rows if r.ok),
        "rung_reached": reached,
        "failure_families": families,
        "vitis_available": False,
        "rungs_not_attempted": list(VITIS_RUNGS),
        "ladder_note": (
            "Only host software equivalence (rung 1 of 4) can run without vitis_hls. "
            "CSim, CSynth and C/RTL CoSim were NOT attempted and no claim is made about them."
        ),
        "elapsed_s": round(elapsed, 1),
    }


def _render_markdown(rows: list[BenchmarkResult], summary: dict) -> str:
    lines = ["# CHStone run — c2hlsc-agent", ""]
    lines.append(f"Mode: `{summary['mode']}` · benchmarks: {summary['benchmarks']} · "
                 f"passed: **{summary['passed']}/{summary['completed']}** · {summary['elapsed_s']}s")
    lines.append("")
    lines.append("> **Ladder coverage.** " + summary["ladder_note"])
    lines.append("")
    lines.append("| benchmark | rung reached | ok | failure family | seconds |")
    lines.append("| --- | --- | :-: | --- | --: |")
    for row in sorted(rows, key=lambda r: r.benchmark):
        lines.append(f"| `{row.benchmark}` | {row.rung} | {'PASS' if row.ok else 'FAIL'} | "
                     f"{row.failure_family or '-'} | {row.duration_s} |")
    if summary["failure_families"]:
        lines += ["", "## Failure families", "", "| family | n |", "| --- | :-: |"]
        for family, count in sorted(summary["failure_families"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{family}` | {count} |")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_chstone.py",
        description="Run the CHStone HLS benchmark suite through the c2hlsc conversion agent. "
                    "Only host software equivalence runs here; the Vitis rungs are never faked.",
    )
    parser.add_argument("--benchmark", required=True, type=Path, help="Path to a CHStone checkout")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--benchmarks", nargs="+", default=[], metavar="NAME")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--native-baseline", action="store_true",
                        help="Compile and run CHStone's own C (the calibration rung); no conversion")
    parser.add_argument("--keep-going", action="store_true", default=True,
                        help="Emit the project even when static diagnostics contain errors (default: on, "
                             "matching CHStone's own Vitis flow which tolerates printf in the top)")
    parser.add_argument("--strict-diagnostics", dest="keep_going", action="store_false",
                        help="Stop at static analysis when the top has non-synthesizable constructs")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--llm-backend")
    parser.add_argument("--llm-model")
    parser.add_argument("--llm-cli-cmd")
    parser.add_argument("--auto-repair", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--native-timeout", type=int, default=DEFAULT_NATIVE_TIMEOUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.benchmark
    if not root.exists():
        raise SystemExit(f"CHStone checkout not found: {root} (clone {CHSTONE_URL})")
    benchmarks = discover_benchmarks(root, tuple(args.benchmarks))
    if not benchmarks:
        raise SystemExit(f"no CHStone benchmarks found under {root}")

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    done: set[str] = set()
    if args.resume and results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["benchmark"])
            except (json.JSONDecodeError, KeyError):
                continue
    todo = [b for b in benchmarks if b.name not in done]
    mode = "native" if args.native_baseline else "agent"
    log(f"CHStone [{mode}]: {len(benchmarks)} benchmarks, {len(todo)} to run, {args.workers} worker(s)")
    if mode == "agent":
        log("note: vitis_hls is not used by this harness; CSim/CSynth/CoSim are NOT attempted")

    started = time.time()

    def run_one(bench: ChstoneBenchmark) -> BenchmarkResult:
        if args.native_baseline:
            row = run_native(bench, out_dir / "native" / bench.name, args.native_timeout)
        else:
            row = run_agent(bench, out_dir, args)
        log(f"[{bench.name:<9}] rung={row.rung:<17} {'PASS' if row.ok else 'FAIL':<4} "
            f"{row.failure_family or '':<34} {row.duration_s}s")
        if args.verbose and row.evidence and not row.ok:
            log("    " + row.evidence.strip().splitlines()[-1][:160] if row.evidence.strip() else "")
        with _jsonl_lock, results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row.to_dict(), sort_keys=True) + "\n")
        return row

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rows = list(pool.map(run_one, todo))

    summary = _summarise(rows, mode, time.time() - started, len(benchmarks))
    (out_dir / "report.json").write_text(
        json.dumps({**summary, "results": [r.to_dict() for r in rows]}, indent=2, sort_keys=True),
        encoding="utf-8")
    (out_dir / "report.md").write_text(_render_markdown(rows, summary), encoding="utf-8")
    log(f"\n{mode}: {summary['passed']}/{summary['completed']} passed; "
        f"rungs {summary['rung_reached']}")
    log(f"report: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
