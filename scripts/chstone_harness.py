#!/usr/bin/env python3
"""Run the CHStone benchmark suite through the conversion agent and report pass@k.

CHStone's own HLS flow is, per each benchmark's ``hls.tcl``::

    add_files <bench>_driver.c -cflags "-Dmain=chstone_main"
    set_top chstone_main

That is, the synthesis top is the driver's ``main()`` renamed: **zero arguments**, with
the test vectors baked in as file-scope globals, and success defined as returning 0.
This harness reproduces that contract:

* the driver is assembled into one translation unit (CHStone drivers already
  ``#include`` their own ``.c`` files, so this only needs to inline local includes);
* ``main`` is renamed to ``chstone_main``;
* the agent converts ``chstone_main``;
* every outcome is classified by its *first* blocking cause, so a run reports where the
  suite actually stops rather than a bare pass/fail count.

pass@k uses the unbiased estimator from Chen et al. 2021 (Codex): with ``n`` independent
samples of which ``c`` pass, ``pass@k = 1 - C(n-c, k) / C(n, k)``. Taking "the first k of
n" instead would be biased, so ``--samples`` must be >= max(k).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from math import comb
from pathlib import Path

BENCHMARKS = (
    "adpcm", "aes", "blowfish", "dfadd", "dfdiv", "dfmul",
    "dfsin", "gsm", "jpeg", "mips", "motion", "sha",
)
TOP = "chstone_main"

#: First-blocker classification. Order matters: the first match wins, so a run is
#: attributed to the earliest thing that stopped it.
BLOCKERS: tuple[tuple[str, str, str], ...] = (
    ("file-io", r"\bfile-io\b", "printf/scanf in the top (CHStone self-checks print their result)"),
    ("dynamic-allocation", r"\bdynamic-allocation\b", "malloc/free in the top"),
    ("unsupported-stdlib-call", r"\bunsupported-stdlib-call\b", "exit/abort/rand in the top"),
    ("recursion", r"\brecursion\b", "the top calls itself"),
    ("pointer-arithmetic", r"\bpointer-arithmetic\b", "unrestricted pointer arithmetic on an argument"),
    ("variable-length-array", r"\bvariable-length-array\b", "local array with a non-literal bound"),
    ("function-pointer", r"\bfunction-pointer\b", "indirect call through a function pointer"),
    ("unbounded-loop", r"\bunbounded-loop\b", "for(;;) or while(1)"),
    ("kr-function-definition", r"redeclared as different kind of entity|declared void|expected unqualified-id before .\{.",
     "K&R-style function definitions, which C++ does not accept in any form"),
    ("cxx-narrowing", r"narrowing conversion of",
     "C brace-initialiser that C++11 rejects as narrowing"),
    ("missing-file-scope-context", r"was not declared in this scope|undeclared identifier",
     "generated hls_top.cpp drops the file-scope globals/helpers the top uses"),
    ("compile-error", r"\berror:", "generated source did not compile"),
    ("top-not-found", r"top function .* not found", "no chstone_main after renaming"),
)


@dataclass
class Sample:
    index: int
    status: str                      # convert's own verdict: pass | fail | blocked | skipped | error
    blocker: str | None = None
    detail: str = ""
    selfcheck: str = "not-run"       # CHStone's criterion against the generated design
    golden_selfcheck: str = "not-run"  # the same criterion against the adapted golden C

    @property
    def passed(self) -> bool:
        """All three legs, because any one alone can lie.

        * ``status`` -- the agent accepted the design. It can write an ``hls_top.cpp``
          while still reporting failure (mips: the agent refuses the benchmark's
          ``while(1)``), and scoring a refusal as a success because the leftover file
          happens to run would overstate what the agent did.
        * ``selfcheck`` -- CHStone's own criterion: the generated ``chstone_main``
          returns 0.
        * ``golden_selfcheck`` -- the control on this harness. If the adapted golden C
          does not itself return 0, the adaptation is what got measured, not the agent.
        """

        return (
            self.status == "pass"
            and self.selfcheck == "pass"
            and self.golden_selfcheck == "pass"
        )


@dataclass
class BenchResult:
    name: str
    samples: list[Sample] = field(default_factory=list)

    @property
    def passes(self) -> int:
        return sum(1 for s in self.samples if s.passed)

    @property
    def first_blocker(self) -> str | None:
        for sample in self.samples:
            if sample.blocker:
                return sample.blocker
        return None


# --------------------------------------------------------------------------- #
# Assembling one benchmark into a single translation unit
# --------------------------------------------------------------------------- #

_LOCAL_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"[ \t]*$', re.M)


_ADD_FILES = re.compile(r"^\s*add_files\s+(?!-tb\b)(\S+\.c)", re.M)


def driver_for(bench_dir: Path) -> Path:
    """The benchmark's synthesis driver, taken from its own ``hls.tcl``.

    Guessing from the directory name is wrong for two of the twelve: jpeg's driver is
    ``main.c`` and motion's is ``mpeg2.c``. The tcl is authoritative -- it is the file
    CHStone itself hands to the HLS tool as the top.
    """

    tcl = bench_dir / "hls.tcl"
    if tcl.exists():
        for name in _ADD_FILES.findall(tcl.read_text(encoding="utf-8", errors="replace")):
            candidate = bench_dir / name
            if candidate.exists():
                return candidate
    for guess in (bench_dir / f"{bench_dir.name}_driver.c", bench_dir / f"{bench_dir.name}.c"):
        if guess.exists():
            return guess
    candidates = sorted(bench_dir.glob("*.c"))
    if not candidates:
        raise FileNotFoundError(f"no .c files in {bench_dir}")
    return candidates[0]


def assemble(bench_dir: Path, extra_roots: list[Path]) -> str:
    """Inline local ``#include "..."`` recursively; leave ``<system>`` includes alone."""

    driver = driver_for(bench_dir)

    seen: set[Path] = set()
    roots = [bench_dir, *extra_roots]

    def expand(path: Path) -> str:
        resolved = path.resolve()
        if resolved in seen:
            return f"/* already inlined: {path.name} */\n"
        seen.add(resolved)
        text = path.read_text(encoding="utf-8", errors="replace")

        def replace(match: re.Match[str]) -> str:
            target = match.group(1)
            for root in roots:
                candidate = root / target
                if candidate.exists():
                    return expand(candidate)
            return match.group(0)  # not local; leave it for the compiler

        return _LOCAL_INCLUDE.sub(replace, text)

    return expand(driver)


_IO_CALLS = ("printf", "fprintf", "puts", "putchar", "scanf", "fscanf", "exit")


def _call_span(source: str, start: int) -> int | None:
    """Index just past the ``)`` closing the call whose ``(`` follows ``start``."""

    open_index = source.find("(", start)
    if open_index == -1:
        return None
    depth = 0
    quote: str | None = None
    escape = False
    for index in range(open_index, len(source)):
        char = source[index]
        if quote is not None:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _starts_statement(source: str, index: int) -> bool:
    """True when the call at ``index`` begins a statement, so its value is discarded.

    ``printf(...);`` on its own is removable. ``int n = printf(...);`` is not -- deleting
    it would leave ``int n = ;``. The preceding non-space character decides: a statement
    can only follow ``;``, a brace, a ``)`` (as in ``if (c) printf(...);``), a label, or
    the keywords ``else``/``do``.
    """

    return _statement_context(source, index) is not None


def _statement_context(source: str, index: int) -> str | None:
    """``"bare"`` when the call is the brace-less body of a control statement.

    ``"block"`` when it is an ordinary statement inside a block, and ``None`` when its
    value is consumed by an expression. The distinction matters: deleting the body of
    ``if (c) printf(...);`` outright would silently attach the *next* statement to the
    ``if``, so a bare body must leave ``;`` behind rather than nothing.
    """

    cursor = index - 1
    while cursor >= 0 and source[cursor] in " \t\r\n":
        cursor -= 1
    if cursor < 0:
        return "block"
    if source[cursor] in ";{}" or source[cursor] == ":":
        return "block"
    if source[cursor] == ")":
        return "bare"
    word = re.search(r"(\w+)$", source[:cursor + 1])
    if word and word.group(1) in {"else", "do"}:
        return "bare"
    return None


def strip_io(source: str) -> tuple[str, int]:
    """Remove console-I/O statements, preserving every value the benchmark computes.

    CHStone decides success by the value ``chstone_main`` **returns**, not by what it
    prints -- ``common/tb.c`` is just ``return chstone_main();``. Vitis HLS ignores
    printf during synthesis for the same reason. This agent, by contrast, hard-rejects
    console I/O inside the top (diagnostic ``file-io``), so the printing has to go for
    the benchmark to be expressible at all.

    Only whole call statements are removed, and only where the call's value is
    discarded; a call feeding an expression is left alone so nothing silently changes
    the computation. ``exit(N)`` becomes ``return N`` so control flow is preserved
    rather than deleted.
    """

    removed = 0
    for name in _IO_CALLS:
        pattern = re.compile(rf"\b{name}\s*\(")
        cursor = 0
        while True:
            match = pattern.search(source, cursor)
            if match is None:
                break
            call_start = match.start()
            end = _call_span(source, call_start)
            if end is None:
                break
            semicolon = re.match(r"\s*;", source[end:])
            context = _statement_context(source, call_start)
            if semicolon is None or context is None:
                cursor = end
                continue
            if name == "exit":
                argument = source[source.find("(", call_start) + 1:end - 1].strip()
                replacement = f"return {argument or 0};"
            else:
                # A brace-less control-statement body must stay a statement.
                replacement = ";" if context == "bare" else ""
            source = source[:call_start] + replacement + source[end + semicolon.end():]
            cursor = call_start + len(replacement)
            removed += 1
    # The includes stay. strip_io deliberately leaves calls whose value is consumed by an
    # expression, and dropping <stdio.h> under them would turn a working benchmark into
    # "'printf' was not declared in this scope".
    return source, removed


_TENTATIVE = re.compile(
    r"^[A-Za-z_][A-Za-z_0-9 \t]*[ \t*]+[A-Za-z_][A-Za-z_0-9]*(?:\[[^\]]*\])*[ \t]*;[ \t]*$"
)


def merge_tentative_definitions(source: str) -> tuple[str, int]:
    """Collapse repeated file-scope tentative definitions into one.

    C lets the same object be declared at file scope more than once without an
    initialiser -- these are *tentative definitions* and the translation unit merges
    them. CHStone relies on it: ``unsigned char *CurHuffReadBuf;`` appears in both
    ``huffman.h`` and ``init.h``. C++ has no tentative definitions, so once those headers
    are inlined into one unit the second is a hard redefinition.

    Only exact duplicate declarations at column zero with no initialiser are merged, so
    the value the program computes cannot change: in C these already denoted one object.
    """

    seen: set[str] = set()
    merged = 0
    lines: list[str] = []
    for line in source.splitlines():
        key = line.strip()
        if key and not line[:1].isspace() and _TENTATIVE.match(line) and not key.startswith(("return", "typedef")):
            if key in seen:
                lines.append(f"/* merged tentative definition: {key} */")
                merged += 1
                continue
            seen.add(key)
        lines.append(line)
    return "\n".join(lines), merged


def rename_main(source: str) -> tuple[str, bool]:
    """Rename the driver's ``main`` definition to ``chstone_main``.

    CHStone does this with ``-Dmain=chstone_main``. A textual rename is used instead so
    the agent's analyzer, which locates the top by name in the source, can find it.
    """

    pattern = re.compile(r"(^|\n)([ \t]*(?:int[ \t]+)?)main([ \t]*\()", re.M)
    renamed, count = pattern.subn(rf"\1\2{TOP}\3", source)
    return renamed, count > 0


# --------------------------------------------------------------------------- #
# Running one sample
# --------------------------------------------------------------------------- #


GOLDEN_DRIVER = """/* CHStone's own criterion, from common/tb.c. */
int chstone_main(void);
int main(void) { return chstone_main(); }
"""

HLS_DRIVER = """// CHStone's own criterion, from common/tb.c, against the generated design.
#include "hls_top.hpp"
int main() { return chstone_main(); }
"""


def _build_and_run(sources: list[str], compiler: list[str], out: Path, timeout: int) -> tuple[str, str]:
    """Compile and run; return ``(outcome, detail)`` where outcome is pass/fail/error."""

    build = subprocess.run([*compiler, *sources, "-o", str(out)], capture_output=True, text=True)
    if build.returncode != 0:
        errors = [line for line in build.stderr.splitlines() if "error" in line.lower()]
        return "error", (errors[0] if errors else build.stderr.strip())[:200]
    try:
        run = subprocess.run([str(out)], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "error", f"self-check exceeded {timeout}s"
    if run.returncode == 0:
        return "pass", ""
    return "fail", f"chstone_main returned {run.returncode}"


def self_check(prepared: Path, project: Path, work: Path, timeout: int) -> dict[str, str]:
    """Run CHStone's own success criterion against both the golden C and the design.

    CHStone does not decide success by comparing outputs against a reference: each
    benchmark checks itself and ``chstone_main`` returns 0 exactly when it agrees with
    its own baked-in expected values (``common/tb.c`` is just ``return chstone_main();``).

    The golden leg is a control on this harness. It compiles the *adapted* C -- after
    single-TU assembly, the main rename and the console-I/O removal -- and requires it to
    still return 0. If that leg fails, the adaptation broke the benchmark and the design
    leg says nothing about the agent.
    """

    work.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}

    golden_driver = work / "golden_tb.c"
    golden_driver.write_text(GOLDEN_DRIVER, encoding="utf-8")
    result["golden"], result["golden_detail"] = _build_and_run(
        [str(prepared), str(golden_driver)],
        ["gcc", "-std=gnu99", "-w"],
        work / "golden_tb",
        timeout,
    )

    design = project / "src" / "hls_top.cpp"
    if not design.exists():
        result["design"], result["design_detail"] = "error", "no src/hls_top.cpp"
        return result

    hls_driver = work / "hls_tb.cpp"
    hls_driver.write_text(HLS_DRIVER, encoding="utf-8")
    result["design"], result["design_detail"] = _build_and_run(
        [str(hls_driver), str(design)],
        ["g++", "-std=c++17", "-w", "-Wno-narrowing", "-I", str(project / "src")],
        work / "hls_tb",
        timeout,
    )
    return result


def classify(text: str) -> tuple[str | None, str]:
    for name, pattern, description in BLOCKERS:
        if re.search(pattern, text, re.I):
            return name, description
    return None, ""


def run_sample(
    bench: str,
    prepared: Path,
    out_dir: Path,
    index: int,
    extra_args: list[str],
    timeout: int,
) -> Sample:
    project = out_dir / f"{bench}_s{index}"
    command = [
        sys.executable, "-m", "c2hlsc_agent", "convert",
        "--input", str(prepared),
        "--top", TOP,
        "--out", str(project),
        "--seed", str(1000 + index),
        "--new-run",
        *extra_args,
    ]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Sample(index, "error", "timeout", f"exceeded {timeout}s")

    combined = (run.stdout or "") + (run.stderr or "")
    report = project / "conversion_report.json"
    if report.exists():
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        status = str(data.get("status", "error"))
        blob = combined + json.dumps(data)[:20000]
        blocker, detail = classify(blob)
    else:
        status = "error"
        blocker, detail = classify(combined)
        blocker = blocker or "no-report"
        detail = detail or combined.strip()[-300:]

    checks = self_check(prepared, project, project / "selfcheck", timeout)
    sample = Sample(
        index,
        status,
        blocker,
        detail,
        selfcheck=checks.get("design", "not-run"),
        golden_selfcheck=checks.get("golden", "not-run"),
    )
    if sample.passed:
        # A sample that meets CHStone's criterion has nothing blocking it. convert's own
        # static diagnostics can still fire -- mips trips `unbounded-loop` because its
        # simulator runs until a halt instruction -- but reporting that as a blocker on a
        # benchmark that passed would misstate the result.
        sample.blocker, sample.detail = None, ""
    elif not sample.blocker:
        if sample.status != "pass":
            sample.blocker = f"convert-{sample.status}"
        elif sample.golden_selfcheck != "pass":
            sample.blocker = "adaptation-broke-golden"
            sample.detail = checks.get("golden_detail", "") or "adapted golden C does not return 0"
        else:
            sample.blocker = f"selfcheck-{sample.selfcheck}"
            sample.detail = checks.get("design_detail", "") or "generated design did not return 0"
    return sample


# --------------------------------------------------------------------------- #
# pass@k
# --------------------------------------------------------------------------- #


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k (Chen et al. 2021). ``n`` samples, ``c`` of them correct."""

    if k > n:
        raise ValueError(f"pass@{k} needs at least {k} samples, got {n}")
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chstone", default="third_party/CHStone", help="CHStone checkout")
    parser.add_argument("--out", default="build/chstone", help="where to write projects and the report")
    parser.add_argument("--benchmarks", default="", help="comma-separated subset (default: all 12)")
    parser.add_argument("--samples", type=int, default=1, help="independent conversion attempts per benchmark")
    parser.add_argument("--k", default="1", help="comma-separated k values for pass@k")
    parser.add_argument("--timeout", type=int, default=900, help="per-sample timeout in seconds")
    parser.add_argument("--convert-arg", action="append", default=[], help="extra arg passed through to convert")
    parser.add_argument("--json", default="", help="also write the report here")
    parser.add_argument(
        "--keep-io",
        action="store_true",
        help="keep printf/exit in the driver (the agent rejects console I/O in the top, "
        "so every benchmark then stops at the file-io diagnostic)",
    )
    args = parser.parse_args(argv)

    chstone = Path(args.chstone)
    if not chstone.is_dir():
        print(f"{chstone} not found; run: python scripts/fetch_chstone.py", file=sys.stderr)
        return 1

    names = [n.strip() for n in args.benchmarks.split(",") if n.strip()] or list(BENCHMARKS)
    ks = sorted({int(k) for k in args.k.split(",") if k.strip()})
    if max(ks) > args.samples:
        print(
            f"pass@{max(ks)} needs --samples >= {max(ks)} (got {args.samples}); "
            "the estimator is undefined otherwise",
            file=sys.stderr,
        )
        return 2

    out_dir = Path(args.out)
    prepared_dir = out_dir / "prepared"
    prepared_dir.mkdir(parents=True, exist_ok=True)

    results: list[BenchResult] = []
    adaptations: dict[str, dict[str, object]] = {}
    for name in names:
        bench_dir = chstone / name
        result = BenchResult(name)
        try:
            source = assemble(bench_dir, [chstone / "common", chstone])
            source, renamed = rename_main(source)
            if not args.keep_io:
                source, stripped = strip_io(source)
            else:
                stripped = 0
            source, merged = merge_tentative_definitions(source)
            if not renamed:
                result.samples.append(Sample(0, "error", "top-not-found", "no main() to rename"))
                results.append(result)
                print(f"{name:10} -> error (no main to rename)")
                continue
            prepared = prepared_dir / f"{name}.c"
            prepared.write_text(source, encoding="utf-8")
            adaptations[name] = {
                "driver": driver_for(bench_dir).name,
                "io_statements_removed": stripped,
                "tentative_definitions_merged": merged,
            }
        except Exception as exc:  # assembling is best-effort; report, do not crash the sweep
            result.samples.append(Sample(0, "error", "assembly-failed", str(exc)[:300]))
            results.append(result)
            print(f"{name:10} -> error (assembly: {exc})")
            continue

        for index in range(args.samples):
            sample = run_sample(name, prepared, out_dir, index, args.convert_arg, args.timeout)
            result.samples.append(sample)
        results.append(result)
        blocker = result.first_blocker or "-"
        legs = f"golden={result.samples[0].golden_selfcheck} design={result.samples[0].selfcheck}"
        print(f"{name:10} -> {result.passes}/{args.samples} pass   {legs:34} blocker={blocker}")

    total = len(results)
    report: dict[str, object] = {
        "benchmarks": total,
        "samples_per_benchmark": args.samples,
        "lines": {
            r.name: {
                "passes": r.passes,
                "samples": len(r.samples),
                "blocker": r.first_blocker,
                "statuses": [s.status for s in r.samples],
                "selfcheck": [s.selfcheck for s in r.samples],
                "golden_selfcheck": [s.golden_selfcheck for s in r.samples],
                "detail": next((s.detail for s in r.samples if s.detail), ""),
            }
            for r in results
        },
        "pass_at_k": {
            f"pass@{k}": round(sum(pass_at_k(len(r.samples), r.passes, k) for r in results) / total, 4)
            for k in ks
        } if total else {},
        "blocker_histogram": {},
        "adaptations": adaptations,
    }
    histogram: dict[str, int] = {}
    for r in results:
        key = r.first_blocker or ("none" if r.passes else f"unclassified:{r.samples[0].status if r.samples else '?'}")
        histogram[key] = histogram.get(key, 0) + 1
    report["blocker_histogram"] = dict(sorted(histogram.items(), key=lambda kv: -kv[1]))

    print()
    for label, value in report["pass_at_k"].items():  # type: ignore[union-attr]
        print(f"{label:8} = {value:.4f}  ({value * total:.1f}/{total} benchmarks)")
    print("\nblockers:")
    for key, count in report["blocker_histogram"].items():  # type: ignore[union-attr]
        print(f"  {count:2d}  {key}")

    destination = Path(args.json) if args.json else out_dir / "chstone_report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
