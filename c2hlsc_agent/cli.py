from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent_loop import classify_failure
from .analyze import analyze_source
from .candidates import select_best_candidate
from .config import AgentConfig, load_config, merge_cli_config
from .convert import ReferenceGenerationError, generate_hls_sources, generate_reference_c
from .equivalence import VerificationState
from .hlsc_repair_agent import clear_repair_audit, repair_project
from .hls_project import write_project
from .hls_runner import verify_project
from .llm import build_llm_client, missing_llm_reason
from .remote import RemoteVitis
from .report import final_status, write_reports
from .run_control import (
    RUN_LEDGER_FILENAME,
    BudgetedLLMClient,
    RunBudget,
    RunBudgetExceeded,
    RunClosed,
    RunController,
    RunLedger,
    RunStatus,
    derive_run_id,
    failure_fingerprint,
    files_fingerprint,
    fresh_run_id,
    snapshot_for_record,
    stable_fingerprint,
)


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
        help="run the Vitis phases (csim/csynth/cosim) on this SSH host, e.g. user@vitis-host; "
        "everything else (analysis, generation, host equivalence, LLM repair) stays local. "
        "Implies --run-vitis. Also honours C2HLSC_VITIS_SSH.",
    )
    parser.add_argument("--vitis-remote-dir", help="remote scratch directory, default ~/c2hlsc_runs")
    parser.add_argument(
        "--vitis-setup",
        help="remote shell prefix that puts vitis_hls on PATH, e.g. "
        "'source /tools/Xilinx/Vitis/2024.2/settings64.sh'; common locations are probed when unset",
    )
    parser.add_argument(
        "--vitis-bin",
        help="vitis_hls executable name or absolute path, for the remote host and for local "
        "runs alike. On Windows give the full path to vitis_hls.bat. Also honours C2HLSC_VITIS_BIN.",
    )


def _add_run_control_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        'bounded continuous run',
        'persistent budgets prevent runaway retries across repeated invocations',
    )
    group.add_argument('--run-id', help='stable run id; derived from inputs by default')
    group.add_argument(
        '--new-run',
        action='store_true',
        help='start a fresh run id after an intentional reset',
    )
    group.add_argument(
        '--max-wall-seconds',
        type=int,
        help='persistent wall-time budget in seconds (default 14400)',
    )
    group.add_argument(
        '--max-llm-calls',
        type=int,
        help='persistent model-call budget (default 8)',
    )
    group.add_argument(
        '--max-vitis-runs',
        type=int,
        help='persistent Vitis verification budget (default 8)',
    )


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
    _add_run_control_arguments(convert)
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
    optimize = sub.add_parser(
        "optimize",
        help="post-equivalence QoR (PPA) optimization of a verified project (rtl_optimizer_agent)",
    )
    optimize.add_argument("--project", required=True, help="existing generated project directory that passes verification")
    optimize.add_argument("--top", help="top function name; defaults to conversion_report.json top when available")
    optimize.add_argument("--input", help="original input C file; defaults to PROJECT/input.c")
    optimize.add_argument("--config", help="YAML/JSON config file used for the original conversion")
    optimize.add_argument(
        "--objective",
        choices=["latency", "area", "balanced"],
        default="latency",
        help="what to minimize; 'balanced' minimizes the latency*area product relative to the baseline",
    )
    optimize.add_argument("--iterations", type=int, default=4, help="number of LLM optimization candidates per round (default 4)")
    optimize.add_argument(
        "--no-cosim-winner",
        action="store_true",
        help="accept the winner after host equivalence only (skip the full Vitis re-ladder); NOT recommended",
    )
    optimize.add_argument(
        "--ppa-script",
        help="script run in the project dir after acceptance (e.g. syn/run_ppa.sh); its "
        "yosys_area.rpt / sta_report.txt enrich the QoR report with area/slack/power",
    )
    targets_group = optimize.add_argument_group(
        "PPA targets",
        "explicit goals the loop iterates toward (each round's winner becomes the new "
        "working point until every specified target is met, no candidate makes progress, "
        "or --max-rounds is exhausted). Slack/area/power targets enable the local "
        "synthesis+waveform+STA step (yosys + OpenSTA) on every scored candidate.",
    )
    targets_group.add_argument("--target-latency", type=int, help="max worst-case latency in cycles (Vitis csynth)")
    targets_group.add_argument("--target-slack", type=float, help="min worst setup slack in ns (OpenSTA on the mapped netlist)")
    targets_group.add_argument("--target-area", type=float, help="max std-cell area in um^2 (yosys stat)")
    targets_group.add_argument("--target-power", type=float, help="max total power in W (OpenSTA report_power), e.g. 2e-3")
    targets_group.add_argument("--max-rounds", type=int, default=5, help="max target-driven optimization rounds (default 5)")
    local = optimize.add_argument_group("local synthesis / STA (waveform PPA step)")
    local.add_argument(
        "--local-ppa",
        action="store_true",
        help="run the local yosys->gate-sim->OpenSTA step even without slack/area/power targets",
    )
    local.add_argument("--liberty", help="liberty file for synthesis/STA (default: syn/lib/*.lib or C2HLSC_LIBERTY)")
    local.add_argument("--sta-bin", help="OpenSTA binary (default: STA_BIN/C2HLSC_STA env, PATH, or ~/tools/eda/opensta/bin/sta)")
    local.add_argument("--clock-port", default="ap_clk", help="clock port name for STA (default ap_clk)")
    local.add_argument("--no-gate-sim", action="store_true", help="skip the gate-level waveform simulation step")
    optimize.add_argument("--verbose", action="store_true", help="print per-candidate progress")
    _add_remote_vitis_arguments(optimize)
    _add_llm_arguments(optimize)
    status = sub.add_parser(
        'status',
        help='show the latest persistent bounded-run state for a project',
    )
    status.add_argument('--project', required=True, help='generated project directory')
    status.add_argument('--run-id', help='specific run id; latest run by default')
    status.add_argument('--json', action='store_true', help='emit machine-readable JSON')
    return parser


def _run_identity(config: AgentConfig) -> dict[str, object]:
    source_hash = files_fingerprint(config.input_files) if config.input_files else None
    arguments = {
        name: {
            'direction': value.direction,
            'length': value.length,
            'range': value.range,
            'interface': value.interface,
        }
        for name, value in sorted(config.arguments.items())
    }
    return {
        'source_hash': source_hash,
        'nl_spec': config.nl_spec or '',
        'top': config.top,
        'part': config.part,
        'clock': config.clock,
        'seed': config.seed,
        'run_vitis': config.run_vitis,
        'arguments': arguments,
        'compiler_flags': list(config.compiler_flags),
        'num_tests': config.num_tests,
        'directed_tests': list(config.directed_tests),
        'interface_mode': config.interface_mode,
        'allow_pragmas': config.allow_pragmas,
        'cosim_tool': config.cosim_tool,
        'rtl': config.rtl,
        'llm_backend': config.llm_backend if config.use_llm else 'none',
        'llm_model': config.llm_model if config.use_llm else None,
        'llm_candidates': config.llm_candidates,
    }


def _start_run_controller(
    out_dir: Path,
    config: AgentConfig,
    args: argparse.Namespace,
) -> RunController:
    identity = _run_identity(config)
    identity_fingerprint = stable_fingerprint(identity)
    run_id = config.run_id or derive_run_id(identity)
    if args.new_run:
        run_id = fresh_run_id(run_id)
    budget = RunBudget(
        max_attempts=max(1, config.max_iterations),
        max_wall_seconds=config.max_wall_seconds,
        max_llm_calls=config.max_llm_calls,
        max_vitis_runs=config.max_vitis_runs,
    )
    return RunController(out_dir, run_id, budget, identity_fingerprint)


def _permit_optional_llm_fallback(
    controller: RunController,
    exc: RunBudgetExceeded,
    stage: str,
) -> None:
    if exc.resource == 'llm_calls':
        return
    controller.finish(RunStatus.EXHAUSTED, str(exc))
    raise SystemExit(f'{stage} stopped: {exc}') from exc


def _blocked_reason(state, config: AgentConfig, analysis) -> str | None:
    """The classifier's reason when the run is stuck on the ENVIRONMENT rather than on a
    defect in the design -- a missing toolchain, an unreachable remote host.

    Such a run must close as ``blocked`` and be handed to a human: closing it ``failed``
    with "no safe repair changed the failing project" points the reader at the design,
    which is exactly the wrong place to look, and leaves anything reading the ledger
    unable to tell a missing tool from a wrong answer.
    """

    decision = classify_failure(state, config.run_vitis, analysis.diagnostics.has_errors)
    if decision.status != 'blocked':
        return None
    return f'{decision.family}: {decision.next_action}'


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
    try:
        controller = _start_run_controller(out_dir, config, args)
    except (RunClosed, ValueError) as exc:
        raise SystemExit(f'cannot start bounded run: {exc}') from exc
    raw_llm = build_llm_client(config)
    llm = (
        BudgetedLLMClient(raw_llm, controller)
        if raw_llm is not None
        else None
    )
    if config.use_llm and llm is None:
        if nl_only:
            reason = (
                f'NL-only generation requires an LLM: '
                f'{missing_llm_reason(config)}'
            )
            controller.finish(RunStatus.BLOCKED, reason)
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
        except RunBudgetExceeded as exc:
            controller.finish(RunStatus.EXHAUSTED, str(exc))
            raise SystemExit(f'NL-only reference generation exhausted its budget: {exc}') from exc
        except ReferenceGenerationError as exc:
            controller.finish(
                RunStatus.BLOCKED,
                f'NL-only reference generation backend failed: {exc}',
            )
            raise SystemExit(f"NL-only reference generation failed (LLM backend error): {exc}")
        if not reference:
            controller.finish(
                RunStatus.FAILED,
                'model returned no usable golden C reference',
            )
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
        try:
            generated, scores = select_best_candidate(out_dir, analysis, config, llm)
        except RunBudgetExceeded as exc:
            _permit_optional_llm_fallback(
                controller,
                exc,
                'candidate generation',
            )
            print(
                f'LLM candidate budget exhausted ({exc}); using the '
                'deterministic generator.',
                file=sys.stderr,
            )
            generated = generate_hls_sources(analysis, config, llm=None)
            scores = []
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
        try:
            generated = generate_hls_sources(analysis, config, llm=llm)
        except RunBudgetExceeded as exc:
            _permit_optional_llm_fallback(controller, exc, 'HLS generation')
            print(
                f'LLM generation budget exhausted ({exc}); using the '
                'deterministic generator.',
                file=sys.stderr,
            )
            generated = generate_hls_sources(analysis, config, llm=None)
    project = write_project(out_dir, analysis, generated, config)
    clear_repair_audit(out_dir)
    repair_history = []

    if analysis.diagnostics.has_errors and not config.keep_going:
        from .equivalence import VerificationState

        state = VerificationState()
        controller.finish(
            RunStatus.BLOCKED,
            'static diagnostics contain errors; manual input is required',
        )
        write_reports(
            project,
            analysis,
            generated,
            config,
            state,
            0,
            repair_history,
            run_control=controller.snapshot(),
        )
        print(f"Static analysis failed; report written to {out_dir / 'conversion_report.md'}", file=sys.stderr)
        return 1

    iterations = max(1, config.max_iterations)
    state = None
    completed_iterations = 0
    seen_signatures = {_project_signature(out_dir)}
    for iteration in range(iterations):
        completed_iterations = iteration + 1
        source_signature = _project_signature(out_dir)
        try:
            controller.reserve_attempt(source_signature)
            if config.run_vitis:
                controller.reserve_vitis_run()
        except RunBudgetExceeded as exc:
            controller.finish(RunStatus.EXHAUSTED, str(exc))
            state = state or VerificationState()
            completed_iterations = controller.record.usage.attempts
            break
        state = verify_project(
            out_dir,
            config.run_vitis,
            verbose=args.verbose,
            remote=remote,
            hls_bin=config.vitis_bin,
        )
        status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
        if status == 'pass':
            controller.record_verification(source_signature, None)
            controller.finish(
                RunStatus.PASSED,
                'all required verification phases passed',
            )
            break
        failure = failure_fingerprint(state)
        repeat_count = controller.record_verification(
            source_signature,
            failure,
        )
        if repeat_count > 1:
            controller.finish(
                RunStatus.EXHAUSTED,
                'the same source and failure recurred; stopping oscillation',
            )
            break
        blocked = _blocked_reason(state, config, analysis)
        if not config.auto_repair:
            controller.finish(
                RunStatus.BLOCKED if blocked else RunStatus.FAILED,
                blocked or 'verification failed and automatic repair is disabled',
            )
            if args.verbose:
                print(
                    'Automatic repair is disabled; bring Vitis/CoSim evidence '
                    'back with the repair command.'
                )
            break
        if completed_iterations >= iterations:
            controller.finish(
                RunStatus.EXHAUSTED,
                'verification-attempt budget reached without a pass',
            )
            break
        try:
            repair = repair_project(
                out_dir,
                analysis,
                config,
                state,
                completed_iterations,
                llm=llm,
            )
        except RunBudgetExceeded as exc:
            _permit_optional_llm_fallback(controller, exc, 'project repair')
            print(
                f'LLM repair budget exhausted ({exc}); trying only safe '
                'deterministic repairs.',
                file=sys.stderr,
            )
            repair = repair_project(
                out_dir,
                analysis,
                config,
                state,
                completed_iterations,
                llm=None,
            )
        repair_history.append(repair)
        if args.verbose:
            print(f"Repair iteration {completed_iterations}: {repair.summary}")
        if not repair.changed:
            blocked = _blocked_reason(state, config, analysis)
            controller.finish(
                RunStatus.BLOCKED if blocked else RunStatus.FAILED,
                blocked or 'no safe repair changed the failing project',
            )
            break
        signature = _project_signature(out_dir)
        if signature in seen_signatures:
            controller.finish(
                RunStatus.EXHAUSTED,
                'repair returned to a previously seen project state',
            )
            if args.verbose:
                print("Repair reproduced a previously seen project state; stopping to avoid oscillation.")
            break
        seen_signatures.add(signature)
    assert state is not None
    completed_iterations = controller.record.usage.attempts
    write_reports(
        project,
        analysis,
        generated,
        config,
        state,
        completed_iterations,
        repair_history,
        run_control=controller.snapshot(),
    )
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


def run_optimize(args: argparse.Namespace) -> int:
    from .qor import PPATargets
    from .qor_optimizer import optimize_project

    targets = PPATargets(
        max_latency_cycles=args.target_latency,
        min_slack_ns=args.target_slack,
        max_area_um2=args.target_area,
        max_power_w=args.target_power,
    )

    project_dir = Path(args.project).resolve()
    if not (project_dir / "src" / "hls_top.cpp").exists():
        raise SystemExit(f"{project_dir} does not look like a generated project (no src/hls_top.cpp)")
    config = merge_cli_config(load_config(Path(args.config).resolve() if args.config else None), args)
    if not config.input_files:
        config.input_files = [(project_dir / "input.c").resolve()]
    if not config.input_files[0].exists():
        raise SystemExit("--input is required because PROJECT/input.c does not exist")
    if not config.top:
        config.top = _load_project_top(project_dir)
    if not config.top:
        raise SystemExit("--top is required because conversion_report.json does not record a top function")
    # QoR optimization is LLM-centric; enable the LLM path unless explicitly refused.
    if not getattr(args, "no_llm", False):
        config.use_llm = True
    llm = build_llm_client(config)
    if config.use_llm and llm is None:
        print(
            f"LLM path unavailable: {missing_llm_reason(config)}; only the deterministic "
            "pipeline-pragma candidate will be tried.",
            file=sys.stderr,
        )
    remote = RemoteVitis.from_config(config)
    if remote is not None and args.verbose:
        print(f"Vitis phases will run on {remote.host}; everything else runs locally.")
    analysis = analyze_source(config.input_files[0], config.top, config)
    try:
        outcome = optimize_project(
            project_dir,
            analysis,
            config,
            llm,
            remote,
            objective=args.objective,
            iterations=args.iterations,
            cosim_winner=not args.no_cosim_winner,
            ppa_script=args.ppa_script,
            targets=targets if targets.specified else None,
            max_rounds=args.max_rounds,
            local_ppa=args.local_ppa,
            liberty=args.liberty,
            sta_bin=args.sta_bin,
            clock_port=args.clock_port,
            gate_sim=not args.no_gate_sim,
            verbose=args.verbose,
        )
    except RuntimeError as exc:
        raise SystemExit(f"QoR optimization could not run: {exc}")
    print(outcome.summary)
    print(f"QoR report: {project_dir / 'qor_report.json'} (+ .md" + (" + qor_table.tex)" if outcome.delta else ")"))
    if outcome.rolled_back:
        # The winner failed the acceptance ladder — the project was restored, but the
        # run needs attention; distinguish it from a clean accept/no-improvement.
        return 1
    if outcome.targets is not None and outcome.targets_met is False:
        # Explicit PPA targets were requested and the loop could not reach them.
        return 1
    if not outcome.accepted and outcome.candidates and not any(
        c.status in ("scored", "timing_regressed") for c in outcome.candidates
    ):
        # Every candidate died before scoring (toolchain outage, all-unparsable, all
        # equivalence failures): not a QoR verdict, so don't exit 0 as if it were.
        return 1
    return 0


def run_status(args: argparse.Namespace) -> int:
    project_dir = Path(args.project).expanduser().resolve()
    ledger = RunLedger(project_dir / RUN_LEDGER_FILENAME)
    try:
        record = ledger.latest(args.run_id)
    except ValueError as exc:
        raise SystemExit(f'cannot read run ledger: {exc}') from exc
    if record is None:
        target = f' for run {args.run_id!r}' if args.run_id else ''
        raise SystemExit(
            f'no bounded-run state found{target} in {project_dir}'
        )
    snapshot = snapshot_for_record(record, ledger.path)
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 0
    usage = snapshot['usage']
    budget = snapshot['budget']
    run_id = snapshot['run_id']
    status = snapshot['status']
    reason = snapshot['reason'] or '-'
    elapsed = snapshot['elapsed_seconds']
    print(f'Run: {run_id}')
    print(f'Status: {status}')
    print(f'Reason: {reason}')
    attempts = usage['attempts']
    max_attempts = budget['max_attempts']
    llm_calls = usage['llm_calls']
    max_llm_calls = budget['max_llm_calls']
    vitis_runs = usage['vitis_runs']
    max_vitis_runs = budget['max_vitis_runs']
    max_wall_seconds = budget['max_wall_seconds']
    print(f'Attempts: {attempts}/{max_attempts}')
    print(f'LLM calls: {llm_calls}/{max_llm_calls}')
    print(f'Vitis runs: {vitis_runs}/{max_vitis_runs}')
    print(f'Elapsed: {elapsed}/{max_wall_seconds} seconds')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "convert":
        return run_convert(args)
    if args.command == "repair":
        return run_repair(args)
    if args.command == "optimize":
        return run_optimize(args)
    if args.command == 'status':
        return run_status(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
