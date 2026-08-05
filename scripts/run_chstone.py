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

Staging: why the agent rung has two flows
-----------------------------------------

The equivalence testbench is C++ and used to ``#include`` the golden reference ``input.c``
into itself. CHStone is C89, so for five of the twelve benchmarks that build failed *before
the candidate was ever exercised* -- ``adpcm``/``jpeg`` on narrowed initialisers,
``blowfish``/``motion`` on K&R parameter definitions, and (once repair pulled the original
sources into the candidate too) ``aes``/``dfadd``/``dfdiv``/``dfmul``/``dfsin``/``mips`` on
``multiple definition of``. Those rows were zeros for a defect in the harness.

The default flow (:func:`run_agent_staged`) fixes that in
:mod:`c2hlsc_agent.chstone_staging`: the golden reference is compiled **as C, by a C
compiler, in its own translation unit**, reduced with ``objcopy`` to a single exported
symbol, and linked in. It also splits generation from repair, so each repair round -- and
each LLM repair prompt -- sees only the candidate's own failure.

``--legacy-staging`` keeps the old single-``convert`` flow bit for bit, because that is the
configuration every previously published CHStone number was measured under. The staging is
therefore an ablation arm, not an unfalsifiable improvement.

Every pass is re-run against a deliberately wrong candidate (``--mutation-check``, on by
default): the equivalence test must go red, or the pass is reported as
``vacuous_equivalence_test`` rather than counted.

Ablation knobs
--------------

``--repair-rounds 0|1|2|3``   repair rounds (``--max-iterations`` is the same knob spelled
                             as verification attempts, N-1 repairs)
``--use-llm``                 model generator vs the deterministic converter
``--legacy-staging``          the pre-fix staging
``--strict-narrowing``        drop ``-Wno-narrowing`` from the candidate's C++ compile
``--label NAME``              names the arm in ``report.json``/``report.md``

Reports separate ``passed`` from ``reachable``: a benchmark the harness blocks is counted
as unreachable, never as a candidate that failed.
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from c2hlsc_agent import chstone_staging  # noqa: E402

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
    notes: list[str] = field(default_factory=list)
    #: "golden_c_tu" (the fixed staging) or "legacy_inline" (golden #included into the C++ TB).
    staging: str = "legacy_inline"
    staging_detail: dict | None = None
    #: repair rounds the arm allowed, and how many actually ran. An ablation arm is only
    #: comparable to another arm with the same *allowed* count.
    repair_rounds_allowed: int = 0
    repair_rounds_run: int = 0
    #: how many stimuli the equivalence testbench reported executing on a pass.
    stimulus_count: int | None = None
    #: "red" / "FALSE_GREEN" / "inconclusive" -- see run_mutation_check.
    mutation_check: str | None = None
    mutation_detail: str | None = None

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
    # Absolute: the benchmark is *run* with cwd set to its own source directory (CHStone
    # tops resolve data paths relatively), so a binary path relative to the repo root would
    # not exist from there -- and the resulting ENOENT reads exactly like a benchmark that
    # failed its own self-check. A relative --out-dir silently zeroed the calibration rung.
    binary = (workdir / f"{bench.name}.bin").resolve()
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


#: The generated testbench prints this on success.
_EQUIV_PASS_RE = re.compile(r"all\s+\d+\s+tests passed", re.I)

#: ...and this when a comparison fails. The mutation check requires this exact line rather
#: than a nonzero exit code, so a mutant that merely fails to compile can never be scored
#: as evidence that the equivalence test has teeth.
_MUTANT_MISMATCH_RE = re.compile(r"Mismatch test=\d+ [^\n]*", re.I)


def _phase_status(report: dict, phase: str) -> str:
    """Status string for one verifier phase, tolerating both conversion_report layouts.

    ``conversion_report.json`` records ``phases[name]`` as a serialized ``PhaseResult``
    (``{"name": ..., "status": "pass", ...}``) and *also* mirrors the status as a flat
    top-level ``report[name]`` string. Reading ``phases[name]`` through ``str()`` compared a
    dict repr against ``"pass"`` / ``"skipped"``, so both of the branches that consume this
    were dead whenever a phase had actually run: an equivalence pass whose log did not carry
    the "all N tests passed" line was scored FAIL, and a benchmark the converter rejected
    before equivalence (status ``skipped``) was reported at the ``generated`` rung instead of
    ``analyzed`` -- i.e. with the wrong earliest-failing stage.
    """

    value = (report.get("phases") or {}).get(phase)
    if isinstance(value, dict):
        value = value.get("status")
    if value is None:
        value = report.get(phase)
    return str(value or "").lower()


def _classify_conversion(report: dict, log_text: str) -> tuple[str, str | None]:
    """Map the host-equivalence log (authoritative) + conversion_report onto (rung, family).

    The log wins over conversion_report.json's phase field: the report records the phase as
    the converter saw it, before sibling sources were staged, so it can be stale.
    """

    if _EQUIV_PASS_RE.search(log_text):
        return "host_equivalence", None
    software = _phase_status(report, "software_equivalence")
    if software == "pass" and "error:" not in log_text.lower():
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
    # After a successful helper-source repair the wall moves to the link step: the golden
    # reference TU and the HLS-C TU both define the original's file-scope globals. That is a
    # defect in the single-binary equivalence harness, not in the candidate, so it gets its
    # own family rather than hiding inside "does not compile".
    if "multiple definition of" in lowered:
        return "generated", "golden_candidate_symbol_collision"
    # CHStone is C compiled by a C++ testbench. Narrowing conversions, K&R declarations and
    # tentative-definition redeclarations are legal C that g++ rejects -- again a property of
    # the flow, not of the generated HLS-C. "tb/../" means the error is in the golden
    # reference the harness inlined, so the candidate was never scored: that is a HARNESS
    # fault and the benchmark is unreachable (see UNREACHABLE_FAMILIES).
    if any(marker in lowered for marker in (
            "narrowing conversion", "redeclared as different kind",
            "redefinition of", "variable or field")) and "tb/../" in log_text:
        return "generated", "original_c_not_valid_cpp"
    # "src/../" means the same C89 constructs, but reached through the *candidate's* own
    # translation unit -- the deterministic repair pulls the original sources into
    # src/hls_top.cpp to supply helpers. That is the converter's strategy failing, not the
    # harness's, so it stays a scored failure with its own name rather than borrowing the
    # unreachable family above.
    if "src/../" in log_text and "error:" in lowered:
        return "generated", "candidate_includes_original_c"
    if "was not declared in this scope" in lowered or "error:" in lowered:
        return "generated", family or "generated_hlsc_does_not_compile"
    return "generated", family or "host_behavior_mismatch"


def _convert_command(top_copy: Path, project: Path, args: argparse.Namespace,
                     *, max_iterations: int, auto_repair: bool) -> list[str]:
    """Build the ``c2hlsc_agent.cli convert`` invocation for one benchmark."""

    cmd = [
        sys.executable, "-m", "c2hlsc_agent.cli", "convert",
        "--input", str(top_copy),
        "--top", CHSTONE_TOP_FUNCTION,
        "--out", str(project),
        "--no-run-vitis",
    ]
    if args.keep_going:
        cmd.append("--keep-going")
    if args.use_llm:
        cmd.append("--use-llm")
        if args.llm_backend:
            cmd += ["--llm-backend", args.llm_backend]
        if args.llm_model:
            cmd += ["--llm-model", args.llm_model]
        if args.llm_cli_cmd:
            cmd += ["--llm-cli-cmd", args.llm_cli_cmd]
    else:
        cmd.append("--no-llm")
    cmd += ["--max-iterations", str(max(1, max_iterations))]
    if auto_repair:
        cmd.append("--auto-repair")
    return cmd


def _repair_command(project: Path, top_copy: Path, log_path: Path, iteration: int,
                    args: argparse.Namespace) -> list[str]:
    """One repair round driven from the *staged* equivalence log.

    Running repair outside ``convert`` is the whole point of the staged flow: the evidence
    the repair agent (and the model) sees is then the candidate's own failure, not a
    C89-in-a-C++-front-end error from the golden reference it does not own.
    """

    cmd = [
        sys.executable, "-m", "c2hlsc_agent.cli", "repair",
        "--project", str(project),
        "--stage", "software_equivalence",
        "--evidence", str(log_path),
        "--input", str(top_copy),
        "--top", CHSTONE_TOP_FUNCTION,
        "--iteration", str(iteration),
    ]
    if args.use_llm:
        cmd.append("--use-llm")
        if args.llm_backend:
            cmd += ["--llm-backend", args.llm_backend]
        if args.llm_model:
            cmd += ["--llm-model", args.llm_model]
        if args.llm_cli_cmd:
            cmd += ["--llm-cli-cmd", args.llm_cli_cmd]
    else:
        cmd.append("--no-llm")
    return cmd


def _make(project: Path, target: str, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(["make", target], capture_output=True, text=True,
                              timeout=timeout, cwd=str(project))
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"make {target} timed out after {timeout}s"
    except OSError as exc:
        return 125, f"make {target} failed: {exc}"


def _stimulus_count(log_text: str) -> int | None:
    match = re.search(r"all\s+(\d+)\s+tests passed", log_text, re.I)
    return int(match.group(1)) if match else None


def run_mutation_check(project: Path, timeout: int) -> tuple[str, str]:
    """Prove the equivalence test is not vacuous: perturb the candidate, expect red.

    Returns ``(verdict, detail)`` where verdict is one of:

    ``red``            the mutant build ran and the test failed -- the check has teeth
    ``FALSE_GREEN``    the mutant build ran and the test still passed -- any pass from this
                       benchmark proves nothing and must not be counted
    ``inconclusive``   the mutant could not be built (the candidate's top could not be
                       wrapped); no claim either way
    """

    mutant = chstone_staging.write_mutant_source(project, CHSTONE_TOP_FUNCTION)
    if mutant is None:
        return "inconclusive", "could not rename the candidate's top to build a mutant"
    code, output = _make(project, chstone_staging.MUTANT_TARGET, timeout)
    if code in {124, 125}:
        return "inconclusive", output.strip()[-400:]
    # A nonzero exit is NOT by itself evidence: the mutant failing to *compile* would also
    # exit nonzero and would prove nothing about the oracle. Only the testbench's own
    # mismatch report proves the binary ran, executed stimuli, and caught the perturbation.
    mismatch = _MUTANT_MISMATCH_RE.search(output)
    if mismatch:
        return "red", mismatch.group(0)
    if _EQUIV_PASS_RE.search(output):
        return "FALSE_GREEN", "equivalence test passed against a candidate returning ret+1"
    return "inconclusive", (
        "mutant did not build or did not report a mismatch: " + output.strip()[-400:]
    )


def run_agent_staged(bench: ChstoneBenchmark, out_dir: Path, args: argparse.Namespace) -> BenchmarkResult:
    """Generate once, stage the golden reference properly, then repair against clean evidence.

    Two phases rather than one ``convert --auto-repair`` call, because the staging has to
    land *between* generation and the first verification. Under the legacy single-call flow
    the first verification compiles CHStone's C with ``g++``, and for five benchmarks that
    error is what every repair round -- and every LLM repair prompt -- is handed, so the
    candidate is never the thing being measured.
    """

    started = time.time()
    work = out_dir / "benchmarks" / bench.name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    staged = work / "src_copy"
    shutil.copytree(bench.directory, staged)
    top_copy = staged / bench.top_file.name
    renamed, count = rename_main(top_copy.read_text(encoding="utf-8", errors="replace"))
    result = BenchmarkResult(benchmark=bench.name, mode="agent", rung="discovered", ok=False)
    result.staging = "golden_c_tu"
    result.repair_rounds_allowed = repair_rounds(args)
    if count != 1:
        result.failure_family = "main_rename_failed"
        result.evidence = f"expected exactly one main definition, renamed {count}"
        result.duration_s = round(time.time() - started, 2)
        return result
    top_copy.write_text(renamed, encoding="utf-8")

    project = work / "project"
    # Phase 1: generation only. Repair is driven below, after staging.
    cmd = _convert_command(top_copy, project, args, max_iterations=1, auto_repair=False)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout,
                              cwd=str(REPO_ROOT))
        combined = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        result.failure_family = "conversion_timeout"
        result.evidence = f"convert timed out after {args.timeout}s"
        result.duration_s = round(time.time() - started, 2)
        result.project_dir = str(project)
        return result
    except OSError as exc:
        result.failure_family = "conversion_error"
        result.evidence = str(exc)
        result.duration_s = round(time.time() - started, 2)
        return result

    staging = chstone_staging.stage_project(
        project, staged, top_copy.name, CHSTONE_TOP_FUNCTION,
        relax_narrowing=args.relax_narrowing,
    )
    result.staging_detail = staging.to_dict()
    if not staging.applied:
        result.rung = "generated" if project.exists() else "analyzed"
        result.failure_family = "staging_not_applied"
        result.evidence = (staging.skipped_reason or "staging did not apply") + "\n" + combined[-2000:]
        result.duration_s = round(time.time() - started, 2)
        result.project_dir = str(project)
        return result

    # Phase 2: verify -> repair -> verify, with the golden reference out of the way.
    log_path = project / "software_equivalence.log"
    rounds = result.repair_rounds_allowed
    log_text = ""
    for attempt in range(rounds + 1):
        code, log_text = _make(project, "test", args.timeout)
        log_path.write_text(log_text, encoding="utf-8")
        result.returncode = code
        if code == 0 and _EQUIV_PASS_RE.search(log_text):
            break
        if attempt >= rounds:
            break
        try:
            repair = subprocess.run(
                _repair_command(project, top_copy, log_path, attempt + 1, args),
                capture_output=True, text=True, timeout=args.timeout, cwd=str(REPO_ROOT))
        except (subprocess.TimeoutExpired, OSError) as exc:
            result.notes.append(f"repair round {attempt + 1} did not complete: {exc}")
            break
        result.repair_rounds_run = attempt + 1
        summary = ((repair.stdout or "").strip().splitlines() or [""])[0][:160]
        result.notes.append(f"repair round {attempt + 1}: {summary}")
        # The repair agent may rewrite tb/testbench.cpp (it adds a `#define restrict` for
        # the macro-included golden). Re-assert the staging so a repair can never quietly
        # put CHStone's C back in front of g++.
        again = chstone_staging.stage_project(
            project, staged, top_copy.name, CHSTONE_TOP_FUNCTION,
            relax_narrowing=args.relax_narrowing,
        )
        if not again.applied:
            result.notes.append(f"staging lost after repair round {attempt + 1}: {again.skipped_reason}")
            break
        if repair.returncode != 0:
            # rc 1 from the repair command means "nothing changed"; another round would
            # re-verify an identical project.
            break

    report_path = project / "conversion_report.json"
    report: dict = {}
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}

    # The STAGED log is the only authority for this flow. conversion_report.json records the
    # phase-1 verification, which ran before staging -- for a benchmark whose golden could
    # not be compiled that field says "fail" harmlessly, but for one that passed unstaged it
    # says "pass", and letting that stand in for a staged link failure would manufacture a
    # green row. So: pass is the testbench's own banner plus a zero exit, nothing else.
    result.ok = result.returncode == 0 and bool(_EQUIV_PASS_RE.search(log_text))
    if result.ok:
        result.rung, result.failure_family = "host_equivalence", None
    else:
        result.rung, result.failure_family = _classify_conversion(
            {"diagnostics": report.get("diagnostics"), "software_equivalence": "fail"}, log_text
        )
    result.stimulus_count = _stimulus_count(log_text) if result.ok else None
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

    if result.ok and args.mutation_check:
        verdict, detail = run_mutation_check(project, args.timeout)
        result.mutation_check = verdict
        result.mutation_detail = detail
        if verdict == "FALSE_GREEN":
            # A pass that survives a deliberately wrong candidate is not a pass.
            result.ok = False
            result.rung = "generated"
            result.failure_family = "vacuous_equivalence_test"

    result.duration_s = round(time.time() - started, 2)
    return result


def run_agent(bench: ChstoneBenchmark, out_dir: Path, args: argparse.Namespace) -> BenchmarkResult:
    """Legacy flow: one ``convert --auto-repair`` call with the golden inlined into the C++ TB.

    Kept, and reachable through ``--legacy-staging``, because it is the configuration every
    previously published CHStone number in ``docs/chstone_rosetta.md`` was measured under.
    Deleting it would make the staging fix unfalsifiable; keeping it makes the staging an
    ablation arm like any other.
    """

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
    result.staging = "legacy_inline"
    result.repair_rounds_allowed = repair_rounds(args)
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
    if args.keep_going:
        cmd.append("--keep-going")
    if args.use_llm:
        cmd.append("--use-llm")
        if args.llm_backend:
            cmd += ["--llm-backend", args.llm_backend]
        if args.llm_model:
            cmd += ["--llm-model", args.llm_model]
        if args.llm_cli_cmd:
            cmd += ["--llm-cli-cmd", args.llm_cli_cmd]
    else:
        cmd.append("--no-llm")
    # --auto-repair is independent of --use-llm: hlsc_repair_agent applies deterministic
    # mechanical repairs (missing includes, helper-source inclusion, restrict compatibility,
    # interface-pragma stripping) with no model at all, and only escalates to an LLM patch
    # when one is configured. Gating it on --use-llm measured single-shot generation and
    # called it the agent.
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

    # CHStone tops #include their sibling sources ("softfloat.c", "softfloat-macros", ...).
    # The converter copies only --input into the project as input.c, so the GOLDEN reference
    # cannot compile until those siblings sit next to it. Stage them and re-run host
    # equivalence, otherwise a missing-include error in the reference is misread as a defect
    # in the generated HLS-C.
    if project.exists():
        for sibling in sorted(staged.iterdir()):
            if not sibling.is_file() or sibling.name in {top_copy.name, "hls.tcl"}:
                continue
            target = project / sibling.name
            if not target.exists():
                shutil.copy(sibling, target)
        try:
            retest = subprocess.run(["make", "test"], capture_output=True, text=True,
                                    timeout=args.timeout, cwd=str(project))
            combined = ((retest.stdout or "") + (retest.stderr or ""))
            (project / "software_equivalence.log").write_text(combined, encoding="utf-8")
            result.notes.append("host equivalence re-run after staging sibling sources")
        except (subprocess.TimeoutExpired, OSError) as exc:
            combined += f"\nmake test after staging siblings failed: {exc}"

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


def repair_rounds(args: argparse.Namespace) -> int:
    """Repair rounds this arm allows, from either spelling of the knob.

    ``--repair-rounds N`` is authoritative when given. Otherwise the CLI's own
    ``--max-iterations`` semantics apply: N verification attempts, so N-1 repairs. Either
    way repair only happens when ``--auto-repair`` is on, so an arm can never acquire
    repair rounds it did not ask for.
    """

    if getattr(args, "repair_rounds", None) is not None:
        rounds = max(0, int(args.repair_rounds))
    else:
        rounds = max(0, int(args.max_iterations) - 1)
    return rounds if getattr(args, "auto_repair", False) else 0


def arm_descriptor(args: argparse.Namespace) -> dict[str, object]:
    """The full configuration of this run, so no table from it is quotable without it."""

    return {
        "label": args.label or ("native" if args.native_baseline else
                                ("llm" if args.use_llm else "deterministic")),
        "generator": "llm" if args.use_llm else "deterministic",
        "llm_backend": args.llm_backend,
        "llm_model": args.llm_model,
        "staging": "golden_c_tu" if args.staged else "legacy_inline",
        "repair_rounds_allowed": repair_rounds(args),
        "max_iterations": args.max_iterations,
        "auto_repair": bool(args.auto_repair),
        "relax_narrowing": bool(args.relax_narrowing) and args.staged,
        "mutation_check": bool(args.mutation_check) and args.staged,
        "keep_going": bool(args.keep_going),
    }


def row_from_dict(payload: dict) -> BenchmarkResult | None:
    """Rebuild a row recorded by an earlier run, or None if it is not a usable row.

    ``--resume`` has to fold the rows already on disk back into the report; without them the
    final report.md of a resumed sweep covers only the benchmarks this invocation happened to
    run ("passed: 1/2" for a 12-benchmark suite).
    """

    if not isinstance(payload, dict):
        return None
    names = {f.name for f in dataclasses.fields(BenchmarkResult)}
    kwargs = {key: value for key, value in payload.items() if key in names}
    kwargs["rungs_not_attempted"] = tuple(kwargs.get("rungs_not_attempted") or ())
    try:
        return BenchmarkResult(**kwargs)
    except TypeError:
        return None


#: Failure families whose cause is the harness rather than the candidate. A benchmark that
#: dies here never put the generated HLS-C in front of the oracle at all, so it is
#: *unreachable*, not failed, and reporting it as a zero flatters the harness by pretending
#: the measurement was taken.
UNREACHABLE_FAMILIES = frozenset({
    "original_c_not_valid_cpp",
    "golden_candidate_symbol_collision",
    "staging_not_applied",
    "main_rename_failed",
})


def _reachable(row: BenchmarkResult) -> bool:
    return row.ok or row.failure_family not in UNREACHABLE_FAMILIES


def _summarise(rows: list[BenchmarkResult], mode: str, elapsed: float, benchmarks: int,
               resumed: int = 0, arm: dict | None = None) -> dict:
    reached: dict[str, int] = {}
    families: dict[str, int] = {}
    for row in rows:
        reached[row.rung] = reached.get(row.rung, 0) + 1
        if row.failure_family:
            families[row.failure_family] = families.get(row.failure_family, 0) + 1
    reachable = [row for row in rows if _reachable(row)]
    mutations = {}
    for row in rows:
        if row.mutation_check:
            mutations[row.mutation_check] = mutations.get(row.mutation_check, 0) + 1
    return {
        "mode": mode,
        "arm": arm or {},
        "benchmarks": benchmarks,
        "completed": len(rows),
        "resumed": resumed,
        "passed": sum(1 for r in rows if r.ok),
        # Reachable = the candidate actually reached the oracle. Reported next to `passed`
        # because "0/12" and "0/7 of the 7 that could be scored" are different claims.
        "reachable": len(reachable),
        "unreachable": [r.benchmark for r in rows if not _reachable(r)],
        "passed_of_reachable": sum(1 for r in reachable if r.ok),
        "mutation_check": mutations,
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
    arm = summary.get("arm") or {}
    lines = ["# CHStone run — c2hlsc-agent", ""]
    lines.append(f"Mode: `{summary['mode']}` · arm: `{arm.get('label', summary['mode'])}` · "
                 f"benchmarks: {summary['benchmarks']} · "
                 f"passed: **{summary['passed']}/{summary['completed']}** · {summary['elapsed_s']}s")
    lines.append("")
    if arm:
        lines.append("## Configuration")
        lines.append("")
        lines.append("| setting | value |")
        lines.append("| --- | --- |")
        for key, value in arm.items():
            lines.append(f"| `{key}` | `{value}` |")
        lines.append("")
    lines.append(f"**Reachable** (candidate actually reached the oracle): "
                 f"{summary.get('reachable', summary['completed'])}/{summary['completed']}; "
                 f"passed of reachable: {summary.get('passed_of_reachable', summary['passed'])}. "
                 "A benchmark blocked by the harness is reported as unreachable, not as a zero.")
    lines.append("")
    lines.append("> **Ladder coverage.** " + summary["ladder_note"])
    lines.append("")
    # Column order is deliberate and load-bearing: benchmark/rung/ok/family/seconds keeps the
    # shape every earlier report used, so an old row is still greppable; the ablation columns
    # are appended rather than interleaved.
    lines.append("| benchmark | rung reached | ok | failure family | seconds | reachable | mutation check | stimuli |")
    lines.append("| --- | --- | :-: | --- | --: | :-: | :-: | --: |")
    for row in sorted(rows, key=lambda r: r.benchmark):
        lines.append(f"| `{row.benchmark}` | {row.rung} | {'PASS' if row.ok else 'FAIL'} | "
                     f"{row.failure_family or '-'} | {row.duration_s} | "
                     f"{'yes' if _reachable(row) else 'NO'} | {row.mutation_check or '-'} | "
                     f"{row.stimulus_count if row.stimulus_count is not None else '-'} |")
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
    parser.add_argument("--auto-repair", action="store_true",
                        help="allow repair rounds; needed for --max-iterations/--repair-rounds "
                             "to have any effect")
    parser.add_argument(
        "--max-iterations", type=int, default=2,
        help="VERIFICATION ATTEMPTS, matching `c2hlsc_agent.cli convert --max-iterations`. "
             "Repair rounds = max(0, N - 1), so 2 (the default) is one repair round -- the "
             "setting every published CHStone number in docs/ was measured under. Use "
             "--repair-rounds if you would rather say it the other way round.")
    parser.add_argument(
        "--repair-rounds", type=int, default=None, metavar="N",
        help="ablation spelling of --max-iterations: N repair rounds (N+1 verification "
             "attempts), N=0 meaning generation only. Implies --auto-repair for N > 0. "
             "Sweep 0 1 2 3 to isolate what repair is worth.")
    parser.add_argument(
        "--legacy-staging", dest="staged", action="store_false", default=True,
        help="use the pre-fix staging: golden reference #included into the C++ testbench, "
             "repair driven inside `convert`. This is the arm the 0/12 and 6/12 numbers in "
             "docs/chstone_rosetta.md come from; keep it to measure what the staging is worth")
    parser.add_argument(
        "--strict-narrowing", dest="relax_narrowing", action="store_false", default=True,
        help="do not add -Wno-narrowing to the candidate's C++ compile (staged flow only)")
    parser.add_argument(
        "--no-mutation-check", dest="mutation_check", action="store_false", default=True,
        help="skip the anti-false-green check that rebuilds every PASS against a candidate "
             "whose returned value is perturbed by one and requires the test to go red")
    parser.add_argument("--label", default=None, metavar="NAME",
                        help="name for this ablation arm, recorded in report.json/report.md")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--native-timeout", type=int, default=DEFAULT_NATIVE_TIMEOUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repair_rounds is not None:
        if args.repair_rounds < 0:
            raise SystemExit("--repair-rounds must be >= 0")
        if args.repair_rounds > 0:
            args.auto_repair = True
        # Keep both spellings consistent in the recorded configuration.
        args.max_iterations = args.repair_rounds + 1
    if args.native_baseline and (args.use_llm or args.auto_repair):
        raise SystemExit(
            "--native-baseline compiles and runs CHStone's own C; it constructs no candidate, "
            "so --use-llm/--auto-repair have nothing to act on. Run them into separate --out-dirs."
        )
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
    prior: dict[str, BenchmarkResult] = {}
    if args.resume and results_path.exists():
        wanted = {b.name for b in benchmarks}
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
                name = payload["benchmark"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            done.add(name)
            row = row_from_dict(payload)
            # Rows for benchmarks outside this selection stay in the file but out of the
            # report; the last row for a benchmark wins if it was recorded more than once.
            if row is not None and name in wanted:
                prior[name] = row
    elif results_path.exists() and results_path.stat().st_size:
        # Keep the previous sweep recoverable and stop this run's rows from being appended
        # to it: a mixed file makes a later --resume skip everything, and mixing a native
        # sweep's rows with an agent sweep's rows silently doubles the suite.
        os.replace(results_path, results_path.with_name(results_path.name + ".prev"))
    todo = [b for b in benchmarks if b.name not in done]
    mode = "native" if args.native_baseline else "agent"
    arm = arm_descriptor(args)
    log(f"CHStone [{mode}]: {len(benchmarks)} benchmarks, {len(todo)} to run, {args.workers} worker(s)")
    if args.resume:
        log(f"resume: {len(benchmarks) - len(todo)} benchmarks already done, {len(todo)} to run")
    if mode == "agent":
        log("note: vitis_hls is not used by this harness; CSim/CSynth/CoSim are NOT attempted")
        log(f"arm: {arm['label']} · generator={arm['generator']} · staging={arm['staging']} · "
            f"repair_rounds={arm['repair_rounds_allowed']} · mutation_check={arm['mutation_check']}")
        if not args.staged:
            log("WARNING: --legacy-staging compiles CHStone's C with g++ and inlines the golden "
                "reference into the candidate's binary. Five benchmarks cannot reach the oracle "
                "under it; their rows are reported unreachable, not as zeros.")

    started = time.time()

    def run_one(bench: ChstoneBenchmark) -> BenchmarkResult:
        if args.native_baseline:
            row = run_native(bench, out_dir / "native" / bench.name, args.native_timeout)
        elif args.staged:
            row = run_agent_staged(bench, out_dir, args)
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
        fresh = list(pool.map(run_one, todo))

    # The report covers the whole suite, not just what this invocation ran, so a resumed
    # sweep still reports 12/12 rather than the tail it happened to finish.
    rows = sorted([*prior.values(), *fresh], key=lambda r: r.benchmark)
    summary = _summarise(rows, mode, time.time() - started, len(benchmarks),
                         resumed=len(prior), arm=arm)
    (out_dir / "report.json").write_text(
        json.dumps({**summary, "results": [r.to_dict() for r in rows]}, indent=2, sort_keys=True),
        encoding="utf-8")
    (out_dir / "report.md").write_text(_render_markdown(rows, summary), encoding="utf-8")
    log(f"\n{mode}[{arm['label']}]: {summary['passed']}/{summary['completed']} passed "
        f"({summary['passed_of_reachable']}/{summary['reachable']} of reachable); "
        f"rungs {summary['rung_reached']}")
    if summary["unreachable"]:
        log(f"unreachable (harness, not candidate): {', '.join(summary['unreachable'])}")
    if summary["mutation_check"]:
        log(f"mutation check: {summary['mutation_check']}")
    log(f"report: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
