#!/usr/bin/env python3
"""Frozen held-out evaluator for the c2hlsc-agent safety evolution.

The candidate repository is supplied with --repo.  This file intentionally lives
outside every candidate worktree so candidates cannot alter their own score.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


def _run(command: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(cwd) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _convert(repo: Path, config: Path, out_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            sys.executable,
            "-m",
            "c2hlsc_agent.cli",
            "convert",
            "--config",
            str(config),
            "--out",
            str(out_dir),
            "--no-run-vitis",
            "--no-llm",
        ],
        repo,
    )


def _write_config(
    path: Path,
    input_file: Path,
    top: str,
    arguments: dict[str, object] | None = None,
    num_tests: int = 8,
) -> None:
    path.write_text(
        json.dumps(
            {
                "input_files": [str(input_file)],
                "top": top,
                "num_tests": num_tests,
                "seed": 1,
                "arguments": arguments or {},
            }
        ),
        encoding="utf-8",
    )


def _report_passed(out_dir: Path) -> bool:
    report_path = out_dir / "conversion_report.json"
    if not report_path.exists():
        return False
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return report.get("phases", {}).get("software_equivalence", {}).get("status") == "pass"


def case_configless_length(repo: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="c2hlsc-evo-length-") as raw:
        root = Path(raw)
        source = root / "input.c"
        source.write_text(
            "void vector_add(const int *a, const int *b, int *out, int n) {\n"
            "  for (int i = 0; i < n; ++i) out[i] = a[i] + b[i];\n"
            "}\n",
            encoding="utf-8",
        )
        config = root / "config.json"
        _write_config(config, source, "vector_add", num_tests=8)
        proc = _convert(repo, config, root / "out")
        passed = proc.returncode == 0 and _report_passed(root / "out")
        detail = f"convert_rc={proc.returncode} software_equivalence={_report_passed(root / 'out')}"
        return passed, detail


def case_tail_clobber(repo: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="c2hlsc-evo-tail-") as raw:
        root = Path(raw)
        source = root / "input.c"
        source.write_text(
            "void vector_add(const int *a, const int *b, int *out, int n) {\n"
            "  for (int i = 0; i < n; ++i) out[i] = a[i] + b[i];\n"
            "}\n",
            encoding="utf-8",
        )
        config = root / "config.json"
        _write_config(
            config,
            source,
            "vector_add",
            {
                "a": {"direction": "input", "length": 4},
                "b": {"direction": "input", "length": 4},
                "out": {"direction": "output", "length": 4},
                "n": {"range": [0, 4]},
            },
        )
        out_dir = root / "out"
        initial = _convert(repo, config, out_dir)
        if initial.returncode != 0:
            return False, f"fixture conversion failed rc={initial.returncode}"
        hls = out_dir / "src" / "hls_top.cpp"
        text = hls.read_text(encoding="utf-8")
        mutant, replacements = re.subn(
            r"for\s*\(\s*int\s+i\s*=\s*0\s*;\s*i\s*<\s*n\s*;\s*\+\+i\s*\)",
            "for (int i = 0; i < 4; ++i)",
            text,
            count=1,
        )
        if replacements != 1:
            return False, "fixture mutation pattern not found"
        hls.write_text(mutant, encoding="utf-8")
        proc = _run(["make", "test"], out_dir)
        output = proc.stdout + proc.stderr
        passed = proc.returncode != 0 and "Mismatch" in output
        return passed, f"mutant_rc={proc.returncode} mismatch={'Mismatch' in output}"


def case_input_pointer_mutation(repo: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="c2hlsc-evo-pointer-") as raw:
        root = Path(raw)
        source = root / "input.c"
        source.write_text(
            "int sum_values(int *a, int n) {\n"
            "  int sum = 0;\n"
            "  for (int i = 0; i < n; ++i) sum += a[i];\n"
            "  return sum;\n"
            "}\n",
            encoding="utf-8",
        )
        config = root / "config.json"
        _write_config(
            config,
            source,
            "sum_values",
            {"a": {"length": 4}, "n": {"range": [1, 4]}},
        )
        out_dir = root / "out"
        initial = _convert(repo, config, out_dir)
        if initial.returncode != 0:
            return False, f"fixture conversion failed rc={initial.returncode}"
        hls = out_dir / "src" / "hls_top.cpp"
        text = hls.read_text(encoding="utf-8")
        mutant, replacements = re.subn(
            r"(?m)^(\s*)return\s+sum\s*;",
            r"\1a[0] = 0;\n\1return sum;",
            text,
            count=1,
        )
        if replacements != 1:
            return False, "fixture mutation pattern not found"
        hls.write_text(mutant, encoding="utf-8")
        proc = _run(["make", "test"], out_dir)
        output = proc.stdout + proc.stderr
        passed = proc.returncode != 0 and "Mismatch" in output
        return passed, f"mutant_rc={proc.returncode} mismatch={'Mismatch' in output}"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def case_batch_cosim_fail_closed(repo: Path) -> tuple[bool, str]:
    scripts = repo / "scripts"
    sys.path.insert(0, str(scripts))
    batch = _load_script("run_hls_nl_vitis_batch", scripts / "run_hls_nl_vitis_batch.py")
    with tempfile.TemporaryDirectory(prefix="c2hlsc-evo-batch-") as raw:
        design_dir = Path(raw)
        design = {"record_id": 1, "source_file": "1_hls.txt", "top": "tiny", "path": str(design_dir)}
        original = batch.run_vitis_command

        def fake_run(command, cwd, timeout):
            phase = command[-1]
            if phase == "run_csynth.tcl":
                rtl = design_dir / "hls_nl_project" / "solution1" / "syn" / "verilog" / "tiny.v"
                rtl.parent.mkdir(parents=True, exist_ok=True)
                rtl.write_text("module tiny; endmodule\n", encoding="utf-8")
                return batch.VitisProcessResult(0, "Finished CSynth")
            if phase == "run_cosim.tcl":
                report = design_dir / "hls_nl_project" / "solution1" / "sim" / "report" / "verilog" / "result.log"
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text("C/RTL co-simulation finished: FAIL\n", encoding="utf-8")
                return batch.VitisProcessResult(0, "C/RTL co-simulation finished: FAIL")
            return batch.VitisProcessResult(0, "Finished CSim")

        batch.run_vitis_command = fake_run
        try:
            row = batch.run_design("vitis_hls", design, 1, True, 20)
        finally:
            batch.run_vitis_command = original
        cosim_status = row.get("phases", {}).get("cosim", {}).get("status")
        passed = row.get("status") != "pass" and cosim_status != "pass"
        return passed, f"record_status={row.get('status')} cosim_status={cosim_status}"


def case_repair_parser_fail_closed(repo: Path) -> tuple[bool, str]:
    scripts = repo / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    if "run_hls_nl_vitis_batch" not in sys.modules:
        _load_script("run_hls_nl_vitis_batch", scripts / "run_hls_nl_vitis_batch.py")
    loop = _load_script("cosim_repair_loop", scripts / "cosim_repair_loop.py")
    declaration = loop.pick_code("```cpp\nvoid target(int value);\n```", "target")
    wrong_top = loop.pick_code("```cpp\nvoid different(int value) { (void)value; }\n```", "target")
    passed = declaration is None and wrong_top is None
    return passed, f"declaration_accepted={declaration is not None} wrong_top_accepted={wrong_top is not None}"


CASES: list[tuple[str, Callable[[Path], tuple[bool, str]]]] = [
    ("configless_length", case_configless_length),
    ("tail_clobber", case_tail_clobber),
    ("input_pointer_mutation", case_input_pointer_mutation),
    ("batch_cosim_fail_closed", case_batch_cosim_fail_closed),
    ("repair_parser_fail_closed", case_repair_parser_fail_closed),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    started = time.perf_counter()
    results: dict[str, dict[str, object]] = {}
    for name, case in CASES:
        case_started = time.perf_counter()
        try:
            passed, detail = case(repo)
        except Exception as exc:  # A broken case is a failed score, never a silent skip.
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        results[name] = {
            "passed": passed,
            "detail": detail,
            "seconds": round(time.perf_counter() - case_started, 4),
        }
    score = sum(1 for result in results.values() if result["passed"])
    payload = {
        "score": score,
        "max_score": len(CASES),
        "cases": results,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    print(json.dumps(payload, sort_keys=True))
    print(f"SCORE={score}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
