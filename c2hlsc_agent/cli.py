from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyze import analyze_source
from .config import load_config, merge_cli_config
from .convert import generate_hls_sources
from .hlsc_repair_agent import clear_repair_audit, repair_project
from .hls_project import write_project
from .hls_runner import verify_project
from .llm import build_llm_client, missing_llm_reason
from .report import final_status, write_reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="c2hlsc_agent", description="Conservative C to Vitis HLS C/C++ conversion agent")
    sub = parser.add_subparsers(dest="command", required=True)
    convert = sub.add_parser("convert", help="convert a C top function into a Vitis HLS project")
    convert.add_argument("--input", help="input C file")
    convert.add_argument("--top", help="top function name")
    convert.add_argument("--out", required=True, help="output project directory")
    convert.add_argument("--part", help="Vitis part name")
    convert.add_argument("--clock", type=float, help="clock period in ns")
    convert.add_argument("--num-tests", type=int, help="number of generated tests")
    convert.add_argument("--config", help="YAML/JSON config file")
    vitis = convert.add_mutually_exclusive_group()
    vitis.add_argument("--run-vitis", action="store_true", help="run vitis_hls after host equivalence")
    vitis.add_argument("--no-run-vitis", action="store_true", help="skip Vitis execution")
    convert.add_argument("--cosim-tool", help="cosim simulator tool, e.g. xsim")
    convert.add_argument("--rtl", default="verilog", help="RTL language for cosim, default verilog")
    llm = convert.add_mutually_exclusive_group()
    llm.add_argument(
        "--use-llm",
        action="store_true",
        help="use a model for HLS-C generation and repair (see --llm-backend)",
    )
    llm.add_argument("--no-llm", action="store_true", help="force the deterministic generator/repair (default)")
    convert.add_argument(
        "--llm-backend",
        choices=["auto", "none", "anthropic", "openai"],
        help="LLM backend for --use-llm: 'openai' is OpenAI Chat Completions-compatible "
        "and works with local models (Ollama/LM Studio/llama.cpp/vLLM via --llm-base-url) "
        "or OpenAI-compatible cloud; 'anthropic' uses the Claude API; default auto",
    )
    convert.add_argument(
        "--llm-base-url",
        help="base URL for --llm-backend openai, e.g. http://localhost:11434/v1 for a local Ollama",
    )
    convert.add_argument("--llm-model", help="model id for --use-llm (default per backend)")
    convert.add_argument("--seed", type=int, help="random seed")
    convert.add_argument("--max-iterations", type=int, default=1, help="max verification iterations including repaired reruns")
    convert.add_argument("--keep-going", action="store_true", help="emit project even when static diagnostics contain errors")
    convert.add_argument("--verbose", action="store_true", help="print command output")
    return parser


def run_convert(args: argparse.Namespace) -> int:
    config = merge_cli_config(load_config(Path(args.config).resolve() if args.config else None), args)
    if not config.input_files:
        raise SystemExit("--input or config input_files is required")
    if not config.top:
        raise SystemExit("--top or config top is required")
    out_dir = Path(args.out).resolve()
    llm = build_llm_client(config)
    if config.use_llm and llm is None:
        print(
            f"--use-llm requested but the LLM path is unavailable: {missing_llm_reason(config)}; "
            "using the deterministic generator and repair instead.",
            file=sys.stderr,
        )
    elif llm is not None and args.verbose:
        print(f"LLM generator/repair enabled (model={llm.model})")
    analysis = analyze_source(config.input_files[0], config.top, config)
    generated = generate_hls_sources(analysis, config, llm=llm)
    project = write_project(out_dir, analysis, generated, config)
    clear_repair_audit(out_dir)
    repair_history = []

    if analysis.diagnostics.has_errors and not config.keep_going:
        from .equivalence import VerificationState

        state = VerificationState()
        write_reports(project, analysis, generated, config, state, 0, repair_history)
        print(f"Static analysis failed; report written to {out_dir / 'conversion_report.md'}", file=sys.stderr)
        return 1

    iterations = max(1, config.max_iterations)
    state = None
    completed_iterations = 0
    for iteration in range(iterations):
        completed_iterations = iteration + 1
        state = verify_project(out_dir, config.run_vitis, verbose=args.verbose)
        status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
        if status == "pass":
            break
        if completed_iterations >= iterations:
            break
        repair = repair_project(out_dir, analysis, config, state, completed_iterations, llm=llm)
        repair_history.append(repair)
        if args.verbose:
            print(f"Repair iteration {completed_iterations}: {repair.summary}")
        if not repair.changed:
            break
    assert state is not None
    write_reports(project, analysis, generated, config, state, completed_iterations, repair_history)
    status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
    if args.verbose:
        print(f"Report: {out_dir / 'conversion_report.md'}")
    return 0 if status == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "convert":
        return run_convert(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
