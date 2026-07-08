from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyze import analyze_source
from .candidates import select_best_candidate
from .config import load_config, merge_cli_config
from .convert import ReferenceGenerationError, generate_hls_sources, generate_reference_c
from .hlsc_repair_agent import clear_repair_audit, repair_project
from .hls_project import write_project
from .hls_runner import verify_project
from .llm import build_llm_client, missing_llm_reason
from .remote import RemoteVitis
from .report import final_status, write_reports


def _add_llm_arguments(parser: argparse.ArgumentParser) -> None:
    llm = parser.add_mutually_exclusive_group()
    llm.add_argument(
        "--use-llm",
        action="store_true",
        help="use a model for HLS-C generation and repair (see --llm-backend)",
    )
    llm.add_argument("--no-llm", action="store_true", help="force the deterministic generator/repair (default)")
    parser.add_argument(
        "--llm-backend",
        choices=["auto", "none", "claude-cli", "anthropic", "openai"],
        help="LLM backend for --use-llm: 'claude-cli' drives the local Claude Code CLI "
        "(subscription auth, no API key; the default when 'claude' is on PATH); 'openai' is "
        "OpenAI Chat Completions-compatible and works with local models (Ollama/LM Studio/"
        "llama.cpp/vLLM via --llm-base-url) or OpenAI-compatible cloud; 'anthropic' uses "
        "the Claude API; default auto",
    )
    parser.add_argument(
        "--llm-base-url",
        help="base URL for --llm-backend openai, e.g. http://localhost:11434/v1 for a local Ollama",
    )
    parser.add_argument("--llm-model", help="model id for --use-llm (default per backend)")
    parser.add_argument(
        "--llm-cli-cmd",
        help="command for --llm-backend claude-cli, default 'claude'; may be multi-word",
    )


def _add_remote_vitis_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--vitis-ssh",
        help="run the Vitis phases (csim/csynth/cosim) on this SSH host, e.g. user@linux-box; "
        "everything else (analysis, generation, host equivalence, LLM repair) stays local. "
        "Implies --run-vitis. Also honours C2HLSC_VITIS_SSH.",
    )
    parser.add_argument("--vitis-remote-dir", help="remote scratch directory, default ~/c2hlsc_runs")
    parser.add_argument(
        "--vitis-setup",
        help="remote shell prefix that puts vitis_hls on PATH, e.g. "
        "'source /tools/Xilinx/Vitis/2024.2/settings64.sh'; common locations are probed when unset",
    )
    parser.add_argument("--vitis-bin", help="remote vitis_hls executable name or absolute path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="c2hlsc_agent", description="Conservative C to Vitis HLS C/C++ conversion agent")
    sub = parser.add_subparsers(dest="command", required=True)
    convert = sub.add_parser("convert", help="convert a C top function (and/or an NL spec) into a Vitis HLS project")
    convert.add_argument("--input", help="input C file; may be omitted when --spec/--spec-file is given (NL-only mode)")
    convert.add_argument("--top", help="top function name")
    convert.add_argument("--out", required=True, help="output project directory")
    convert.add_argument(
        "--spec",
        help="natural-language design intent; with --input it guides the LLM generator, "
        "without --input the model first writes the golden C reference from it (requires --top)",
    )
    convert.add_argument("--spec-file", help="read --spec text from a file")
    convert.add_argument(
        "--candidates",
        type=int,
        help="best-of-N LLM generation: score candidates with local host equivalence and "
        "send only the winner to Vitis (default 1)",
    )
    convert.add_argument("--part", help="Vitis part name")
    convert.add_argument("--clock", type=float, help="clock period in ns")
    convert.add_argument("--num-tests", type=int, help="number of generated tests")
    convert.add_argument("--config", help="YAML/JSON config file")
    vitis = convert.add_mutually_exclusive_group()
    vitis.add_argument("--run-vitis", action="store_true", help="run vitis_hls after host equivalence")
    vitis.add_argument("--no-run-vitis", action="store_true", help="skip Vitis execution")
    _add_remote_vitis_arguments(convert)
    convert.add_argument("--cosim-tool", help="cosim simulator tool, e.g. xsim")
    convert.add_argument("--rtl", default="verilog", help="RTL language for cosim, default verilog")
    _add_llm_arguments(convert)
    convert.add_argument("--seed", type=int, help="random seed")
    convert.add_argument("--max-iterations", type=int, help="max verification iterations (default 1); repaired reruns require --auto-repair")
    convert.add_argument("--auto-repair", action="store_true", help="apply mechanical and LLM repairs automatically between verification attempts")
    convert.add_argument("--keep-going", action="store_true", help="emit project even when static diagnostics contain errors")
    convert.add_argument("--verbose", action="store_true", help="print command output")
    repair = sub.add_parser("repair", help="apply a repair from externally supplied Vitis/verification evidence")
    repair.add_argument("--project", required=True, help="existing generated project directory")
    repair.add_argument("--stage", required=True, choices=["software_equivalence", "csim", "csynth", "cosim"], help="earliest failing stage from the external run")
    repair.add_argument("--evidence", action="append", default=[], help="path to a log/report file from the failing stage; may be repeated")
    repair.add_argument("--evidence-text", default="", help="inline failing-stage evidence text")
    repair.add_argument("--input", help="original input C file; defaults to PROJECT/input.c")
    repair.add_argument("--top", help="top function name; defaults to conversion_report.json top when available")
    repair.add_argument("--config", help="YAML/JSON config file used for the original conversion")
    repair.add_argument("--iteration", type=int, default=1, help="repair iteration number recorded in the audit")
    _add_llm_arguments(repair)
    return parser


def run_convert(args: argparse.Namespace) -> int:
    config = merge_cli_config(load_config(Path(args.config).resolve() if args.config else None), args)
    nl_only = bool(config.nl_spec) and not config.input_files
    if not config.input_files and not config.nl_spec:
        raise SystemExit("--input (or config input_files) or --spec/--spec-file is required")
    if not config.top:
        raise SystemExit("--top or config top is required")
    if nl_only:
        config.use_llm = True  # NL-only generation is inherently LLM-driven
    # --spec (design intent) and --candidates (best-of-N) are only honoured on the LLM
    # path; auto-enable it so they are not silently ignored, unless --no-llm is explicit.
    elif (config.nl_spec or config.llm_candidates > 1) and not config.use_llm:
        if getattr(args, "no_llm", False):
            which = "--spec" if config.nl_spec else "--candidates"
            print(f"{which} has no effect with --no-llm; using the deterministic generator.", file=sys.stderr)
        else:
            config.use_llm = True
    out_dir = Path(args.out).resolve()
    llm = build_llm_client(config)
    if config.use_llm and llm is None:
        if nl_only:
            raise SystemExit(f"NL-only generation requires an LLM: {missing_llm_reason(config)}")
        print(
            f"--use-llm requested but the LLM path is unavailable: {missing_llm_reason(config)}; "
            "using the deterministic generator and repair instead.",
            file=sys.stderr,
        )
    elif llm is not None and args.verbose:
        print(f"LLM generator/repair enabled (model={llm.model})")
    remote = RemoteVitis.from_config(config)
    if remote is not None and args.verbose:
        print(f"Vitis phases will run on {remote.host}:{remote.remote_dir}; everything else runs locally.")

    if nl_only:
        try:
            reference = generate_reference_c(config.nl_spec, config.top, llm)
        except ReferenceGenerationError as exc:
            raise SystemExit(f"NL-only reference generation failed (LLM backend error): {exc}")
        if not reference:
            raise SystemExit(
                "the model did not return a usable golden C reference from the spec; "
                "retry or refine --spec/--spec-file"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        reference_path = out_dir / "nl_reference.c"
        reference_path.write_text(reference, encoding="utf-8")
        config.input_files = [reference_path]
        if args.verbose:
            print(f"Golden C reference generated from the NL spec: {reference_path}")

    analysis = analyze_source(config.input_files[0], config.top, config)
    if nl_only:
        # The reference is now the golden oracle. If it left any array parameter unbounded,
        # the testbench must guess a length (16) and may drive it with a random count,
        # which is unsound — warn loudly so the user can add sized arrays or a config bound.
        unbounded = [
            d.message.split("'")[1]
            for d in analysis.diagnostics.items
            if d.code == "missing-pointer-bound" and "'" in d.message
        ]
        if unbounded:
            print(
                f"warning: the NL-generated reference has unbounded array parameter(s) ({', '.join(sorted(set(unbounded)))}); "
                "the testbench will assume length 16 and may drive them unsoundly. Refine --spec to use "
                "fixed-size arrays, or pass --config with arguments.<name>.length for a sound equivalence check.",
                file=sys.stderr,
            )
    generated = None
    if llm is not None and config.use_llm and config.llm_candidates > 1:
        generated, scores = select_best_candidate(out_dir, analysis, config, llm)
        if scores:
            (out_dir / "candidate_scores.json").parent.mkdir(parents=True, exist_ok=True)
            (out_dir / "candidate_scores.json").write_text(
                json.dumps([score.to_dict() for score in scores], indent=2), encoding="utf-8"
            )
        if args.verbose and scores:
            print(f"Best-of-{config.llm_candidates}: scored {len(scores)} candidate(s) with local host equivalence.")
        if generated is None:
            # Every candidate was unparsable or failed to build: take the deterministic
            # copy directly rather than spending one more unscored, unverified LLM call.
            generated = generate_hls_sources(analysis, config, llm=None)
            generated.transformations.append(
                f"Best-of-{config.llm_candidates} produced no usable candidate; "
                "fell back to the conservative deterministic copy."
            )
    if generated is None:
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
    seen_signatures = {_project_signature(out_dir)}
    for iteration in range(iterations):
        completed_iterations = iteration + 1
        state = verify_project(out_dir, config.run_vitis, verbose=args.verbose, remote=remote)
        status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
        if status == "pass":
            break
        if completed_iterations >= iterations:
            break
        if not config.auto_repair:
            if args.verbose:
                print("Automatic repair is disabled; bring Vitis/CoSim evidence back with the repair command.")
            break
        repair = repair_project(out_dir, analysis, config, state, completed_iterations, llm=llm)
        repair_history.append(repair)
        if args.verbose:
            print(f"Repair iteration {completed_iterations}: {repair.summary}")
        if not repair.changed:
            break
        signature = _project_signature(out_dir)
        if signature in seen_signatures:
            if args.verbose:
                print("Repair reproduced a previously seen project state; stopping to avoid oscillation.")
            break
        seen_signatures.add(signature)
    assert state is not None
    write_reports(project, analysis, generated, config, state, completed_iterations, repair_history)
    status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
    if args.verbose:
        print(f"Report: {out_dir / 'conversion_report.md'}")
    return 0 if status == "pass" else 1


def _project_signature(project_dir: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for rel in ("src/hls_top.cpp", "src/hls_top.hpp"):
        path = project_dir / rel
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_project_top(project_dir: Path) -> str | None:
    report = project_dir / "conversion_report.json"
    if not report.exists():
        return None
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    top = data.get("top")
    return str(top) if top else None


def _read_evidence(paths: list[str], inline: str) -> str:
    parts = [inline] if inline else []
    for item in paths:
        path = Path(item).expanduser().resolve()
        parts.append(f"--- {path} ---\n{path.read_text(encoding='utf-8', errors='replace')}")
    return "\n\n".join(part for part in parts if part)


def _external_failure_state(stage: str, evidence: str, run_vitis: bool):
    from .equivalence import PhaseResult, VerificationState

    state = VerificationState()
    phases = ["software_equivalence"]
    if run_vitis:
        phases.extend(["csim", "csynth", "cosim"])
    if stage not in phases:
        # Never silently drop the operator-declared failing stage.
        phases.append(stage)
    for phase in phases:
        if phase == stage:
            state.add_phase(PhaseResult(phase, "fail", stdout=evidence, summary="external evidence supplied"))
            break
        state.add_phase(PhaseResult(phase, "pass", summary="assumed pass before external failing stage"))
    for phase in phases[phases.index(stage) + 1 :]:
        state.add_phase(PhaseResult(phase, "blocked", summary=f"{stage} failed"))
    return state


def run_repair(args: argparse.Namespace) -> int:
    project_dir = Path(args.project).resolve()
    config = merge_cli_config(load_config(Path(args.config).resolve() if args.config else None), args)
    if not config.input_files:
        config.input_files = [(project_dir / "input.c").resolve()]
    if not config.input_files[0].exists():
        raise SystemExit("--input is required because PROJECT/input.c does not exist")
    if not config.top:
        config.top = _load_project_top(project_dir)
    if not config.top:
        raise SystemExit("--top is required because conversion_report.json does not record a top function")
    config.run_vitis = args.stage != "software_equivalence"
    evidence = _read_evidence(args.evidence, args.evidence_text)
    if not evidence:
        raise SystemExit("--evidence or --evidence-text is required")

    llm = build_llm_client(config)
    if config.use_llm and llm is None:
        print(
            f"--use-llm requested but the LLM path is unavailable: {missing_llm_reason(config)}; "
            "applying mechanical repairs only.",
            file=sys.stderr,
        )
    analysis = analyze_source(config.input_files[0], config.top, config)
    state = _external_failure_state(args.stage, evidence, config.run_vitis)
    repair = repair_project(project_dir, analysis, config, state, args.iteration, llm=llm)
    manual_report = project_dir / "manual_repair_report.json"
    manual_report.write_text(
        json.dumps(
            {
                "mode": "external_evidence_manual_repair",
                "project": str(project_dir),
                "stage": args.stage,
                "repair": repair.to_dict(),
                "next_step": "rerun verification or CoSim from the beginning on the Vitis machine",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(repair.summary)
    print(f"Manual repair report: {manual_report}")
    return 0 if repair.changed else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "convert":
        return run_convert(args)
    if args.command == "repair":
        return run_repair(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
