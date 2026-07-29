from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyze import analyze_source
from .audit_memory import promote_run, resolve_store_path
from .candidates import select_best_candidate
from .config import load_config, merge_cli_config
from .contract_planner import plan_contracts
from .convert import ReferenceGenerationError, generate_hls_sources, generate_reference_c
from .hlsc_repair_agent import clear_repair_audit, repair_project
from .hls_project import write_project
from .hls_runner import verify_project
from .llm import build_llm_client, missing_llm_reason
from .local_hls import LocalHlsCosim, available as local_hls_available, resolve_cosim_backend
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
    convert.add_argument(
        "--llm-candidate-workers",
        type=int,
        help="how many independent LLM candidate generations to run concurrently "
        "(default 4; 1 serializes them)",
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
    convert.add_argument(
        "--cosim-backend",
        choices=["auto", "vitis", "vitis-ssh", "local-hls", "none"],
        help="who runs the csynth/cosim ladder: 'vitis'/'vitis-ssh' (Xilinx), 'local-hls' "
        "(local Bambu, no Vitis needed), 'none' (skip), or 'auto' (default: vitis-ssh if a "
        "remote host is set, else local vitis_hls, else local-hls, else skip)",
    )
    _add_llm_arguments(convert)
    plan = convert.add_mutually_exclusive_group()
    plan.add_argument(
        "--plan-contracts",
        action="store_true",
        help="run the live contract_planner: an LLM pass after static analysis that "
        "proposes per-argument direction/length/range where the regex inference is "
        "uncertain (user config wins per-field; the verifier still gates). Implies --use-llm.",
    )
    plan.add_argument(
        "--no-plan-contracts",
        action="store_true",
        help="disable the contract_planner pass even if the config enables it",
    )
    memory = convert.add_mutually_exclusive_group()
    memory.add_argument(
        "--audit-memory",
        action="store_true",
        help="opt into the audit-memory knowledge base: audited repair successes from "
        "passing runs are promoted into a card store and retrieved into future repair "
        "prompts as strategy hints (store: --audit-memory-path, C2HLSC_AUDIT_MEMORY, "
        "or ~/.c2hlsc/audit_memory.jsonl)",
    )
    memory.add_argument(
        "--no-audit-memory",
        action="store_true",
        help="disable audit memory even if the config enables it",
    )
    convert.add_argument(
        "--audit-memory-path",
        help="card store JSONL path for --audit-memory (implies --audit-memory)",
    )
    convert.add_argument("--seed", type=int, help="random seed")
    convert.add_argument("--max-iterations", type=int, help="max verification iterations (default 1); repaired reruns require --auto-repair")
    convert.add_argument("--auto-repair", action="store_true", help="apply mechanical and LLM repairs automatically between verification attempts")
    convert.add_argument("--keep-going", action="store_true", help="emit project even when static diagnostics contain errors")
    ppa_criteria = convert.add_argument_group(
        "PPA workflow criteria",
        "process node + slack gate for the local synthesis/STA step (config `ppa:` block); "
        "runs after the equivalence ladder passes and RTL exists",
    )
    ppa_criteria.add_argument(
        "--node",
        choices=["nangate45", "sky130hd", "asap7"],
        help="process node the PPA criteria are measured on (default nangate45; "
        "sky130hd is the manufacturable option, asap7 the 7 nm predictive datapoint)",
    )
    ppa_criteria.add_argument(
        "--min-slack",
        type=float,
        help="minimum worst setup slack in the node's time unit (ns; ps for asap7); "
        "implies the local PPA step and fails the run when unmet",
    )
    ppa_criteria.add_argument(
        "--local-ppa",
        action="store_true",
        help="run the local yosys/OpenSTA PPA step after a passing ladder even without a slack criterion",
    )
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
    local.add_argument(
        "--node",
        choices=["nangate45", "sky130hd", "asap7"],
        help="process node for the local PPA step (default: config ppa.node, else nangate45); "
        "slack targets are in the node's time unit (ns; ps for asap7)",
    )
    local.add_argument("--liberty", help="explicit liberty file override (default: the node's liberty set, or C2HLSC_LIBERTY)")
    local.add_argument("--sta-bin", help="OpenSTA binary (default: STA_BIN/C2HLSC_STA env, PATH, or ~/tools/eda/opensta/bin/sta)")
    local.add_argument("--clock-port", default="ap_clk", help="clock port name for STA (default ap_clk)")
    local.add_argument("--no-gate-sim", action="store_true", help="skip the gate-level waveform simulation step")
    optimize.add_argument("--verbose", action="store_true", help="print per-candidate progress")
    optimize.add_argument(
        "--cosim-backend",
        choices=["auto", "vitis", "vitis-ssh", "local-hls", "none"],
        help="scoring backend (default auto). With 'local-hls' (Bambu) the pragma-driven "
        "QoR loop does not apply — Bambu ignores HLS pragmas — so optimize reports a "
        "baseline PPA measurement of the local RTL instead of searching candidates",
    )
    _add_remote_vitis_arguments(optimize)
    _add_llm_arguments(optimize)
    ppa = sub.add_parser(
        "ppa",
        help="measure a project's RTL against the PPA workflow criteria (node + slack headroom) and write ppa_report.json",
    )
    ppa.add_argument("--project", required=True, help="generated project directory containing RTL (Vitis syn/verilog, rtl/, or C2HLSC_RTL_DIR)")
    ppa.add_argument("--top", help="top function name; defaults to conversion_report.json top when available")
    ppa.add_argument("--config", help="YAML/JSON config file (source of the ppa criteria block)")
    ppa.add_argument("--node", choices=["nangate45", "sky130hd", "asap7"], help="process node override")
    ppa.add_argument("--min-slack", type=float, help="minimum worst setup slack in the node's time unit; exit 1 when unmet")
    ppa.add_argument("--max-area", type=float, help="maximum std-cell area in um^2 (yosys); exit 1 when unmet")
    ppa.add_argument("--max-power", type=float, help="maximum total power in W (OpenSTA); exit 1 when unmet")
    ppa.add_argument("--max-latency", type=int, help="maximum worst-case latency in cycles (Vitis csynth); exit 1 when unmet")
    ppa.add_argument("--clock", type=float, help="clock period in ns (default: config clock, else 10.0)")
    ppa.add_argument("--liberty", help="explicit liberty file override")
    ppa.add_argument("--sta-bin", help="OpenSTA binary override")
    ppa.add_argument("--clock-port", default="ap_clk", help="clock port name for STA (default ap_clk)")
    ppa.add_argument("--no-gate-sim", action="store_true", help="skip the gate-level waveform simulation step")
    ppa.add_argument("--verbose", action="store_true", help="print progress")
    xref = sub.add_parser(
        "cross-reference",
        help="dual-generation differential oracle over HLS_NL records: two independent "
        "LLM generations (different framings, no shared context) are compiled into "
        "isolated namespaces and compared under shared stimulus; verdicts land in "
        "results.jsonl / cross_referenced_corpus.jsonl / needs_review.jsonl",
    )
    xref.add_argument("--records", required=True, help="HLS_NL records file (JSON array or JSONL)")
    xref.add_argument("--out", required=True, help="output directory")
    xref.add_argument("--offset", type=int, help="skip this many records (shards must use separate --out dirs)")
    xref.add_argument("--limit", type=int, help="process at most this many records")
    xref.add_argument("--record-id", type=int, help="process only this record id")
    xref.add_argument("--seed", type=int, help="shared stimulus seed (default 1)")
    xref.add_argument("--num-vectors", type=int, help="stimulus vectors per record (default 16)")
    _add_llm_arguments(xref)
    xref.add_argument("--verbose", action="store_true", help="print progress")
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
    # --spec (design intent), --candidates (best-of-N), and --plan-contracts are only
    # honoured on the LLM path; auto-enable it so they are not silently ignored, unless
    # --no-llm is explicit.
    elif (config.nl_spec or config.llm_candidates > 1 or config.plan_contracts) and not config.use_llm:
        if getattr(args, "no_llm", False):
            which = "--spec" if config.nl_spec else ("--candidates" if config.llm_candidates > 1 else "--plan-contracts")
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
    plan = None
    if config.plan_contracts and config.use_llm and llm is not None:
        # Live contract_planner (runs BEFORE the nl-only unbounded warning below, so
        # that warning only fires for bounds the planner left unset).
        original_text = config.input_files[0].read_text(encoding="utf-8")
        plan = plan_contracts(analysis, config, llm, original_text)
        if plan.changed:
            # Directions/bounds are baked into AnalysisResult at analyze time; the
            # merged contract only takes effect through a re-analyze.
            analysis = analyze_source(config.input_files[0], config.top, config)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "contract_plan.json").write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
        if args.verbose:
            applied = ", ".join(f"{name}({'/'.join(fields)})" for name, fields in plan.applied.items()) or "none"
            print(f"contract_planner: applied {applied}; plan written to {out_dir / 'contract_plan.json'}")
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
    if plan is not None and (plan.applied or plan.skipped):
        generated.transformations.append(
            f"contract_planner: applied={json.dumps(plan.applied, sort_keys=True)} "
            f"skipped={json.dumps(plan.skipped, sort_keys=True)}"
        )
    project = write_project(out_dir, analysis, generated, config)
    clear_repair_audit(out_dir)
    repair_history = []

    if analysis.diagnostics.has_errors and not config.keep_going:
        from .equivalence import VerificationState

        state = VerificationState()
        write_reports(project, analysis, generated, config, state, 0, repair_history)
        print(f"Static analysis failed; report written to {out_dir / 'conversion_report.md'}", file=sys.stderr)
        return 1

    # Choose who runs the csynth/cosim ladder. local-hls replaces Vitis entirely
    # (local Bambu); vitis/vitis-ssh keep the Xilinx path; none skips it.
    explicit_backend = (config.cosim_backend or "auto").lower() != "auto"
    backend = resolve_cosim_backend(config, remote)
    local = None
    if backend == "local-hls":
        remote = None
        # Run the local ladder when the user asked for RTL verification: either
        # --run-vitis (already set run_vitis) or an explicit --cosim-backend local-hls.
        # If local-hls was only auto-selected and nothing requested a run, skip it
        # (same as the Vitis path being available but --run-vitis absent).
        want_run = config.run_vitis or (explicit_backend and not getattr(args, "no_run_vitis", False))
        if want_run:
            local = LocalHlsCosim.from_config(config, analysis, out_dir)
            if local is None:
                _, reason = local_hls_available()
                raise SystemExit(f"--cosim-backend local-hls is unavailable: {reason}")
            config.run_vitis = True
    elif backend == "vitis":
        remote = None
    elif backend == "none":
        remote = None
        config.run_vitis = False
    if args.verbose:
        print(f"cosim backend: {backend}" + (" (running local Bambu ladder)" if local is not None else ""))

    iterations = max(1, config.max_iterations)
    state = None
    completed_iterations = 0
    audit_store = resolve_store_path(config) if config.audit_memory else None
    seen_signatures = {_project_signature(out_dir)}
    for iteration in range(iterations):
        completed_iterations = iteration + 1
        state = verify_project(out_dir, config.run_vitis, verbose=args.verbose, remote=remote, local=local)
        status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
        if status == "pass":
            break
        if completed_iterations >= iterations:
            break
        if not config.auto_repair:
            if args.verbose:
                print("Automatic repair is disabled; bring Vitis/CoSim evidence back with the repair command.")
            break
        repair = repair_project(out_dir, analysis, config, state, completed_iterations, llm=llm, audit_store=audit_store)
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
    if audit_store is not None and repair_history:
        # Promotion keys on the PRE-PPA functional verdict: PPA is a QoR gate and never
        # decides whether a repair functionally worked. The chain rule inside
        # promote_run keeps ineffective intermediate repairs out of the card store.
        functional_status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
        promoted = promote_run(audit_store, repair_history, out_dir.name, functional_status)
        if args.verbose and promoted:
            print(f"audit_memory: promoted {len(promoted)} repair-success card(s) to {audit_store}")
    # PPA workflow criteria run only on a functionally-signed-off design, so the slack
    # numbers always describe RTL that already passed the equivalence ladder. config
    # parsing guarantees run_local_ppa is True whenever any hard criterion is declared,
    # so this single switch never silently drops a criterion.
    if config.run_local_ppa and final_status(state, config.run_vitis, analysis.diagnostics.has_errors) == "pass":
        # Bambu RTL names its clock `clock` (Vitis uses `ap_clk`) and uses a
        # start/done + memory-bus protocol the Vitis-shaped self-checking gate
        # testbench does not match, so drive PPA off the Bambu netlist with the
        # right clock and no gate sim (Bambu's own cosim already checked function).
        local_hls_rtl = backend == "local-hls"
        ppa_phase = _ppa_gate_phase(
            out_dir, config, verbose=args.verbose,
            clock_port="clock" if local_hls_rtl else "ap_clk",
            gate_sim=not local_hls_rtl,
        )
        state.add_phase(ppa_phase)
        print(f"PPA[{config.node}]: {ppa_phase.status} — {ppa_phase.summary}")
    # final_status now accounts for a present ppa phase, so the headline status, the
    # report, and the exit code cannot disagree.
    write_reports(project, analysis, generated, config, state, completed_iterations, repair_history)
    status = final_status(state, config.run_vitis, analysis.diagnostics.has_errors)
    if args.verbose:
        print(f"Report: {out_dir / 'conversion_report.md'}")
    return 0 if status == "pass" else 1


def _ppa_gate_phase(
    project_dir: Path,
    config,
    verbose: bool = False,
    clock_port: str = "ap_clk",
    gate_sim: bool = True,
    sta_bin: str | None = None,
):
    """Run the local PPA step and grade it against the config's criteria.

    Grading: missing RTL or missing local tools -> "skipped" when no hard criteria are
    set, "fail" when a criterion (min_slack etc.) was declared but cannot be verified
    (an unverifiable criterion is not a met criterion). A failing gate-level sim always
    fails the phase — it is a functional verdict, not a QoR one.
    """

    from .equivalence import PhaseResult
    from .local_ppa import run_local_ppa
    from .qor import QoRMetrics, evaluate_targets, find_csynth_xml, parse_csynth_xml, slack_headroom, targets_from_config

    targets = targets_from_config(config)
    # A max_latency_cycles criterion is measured by Vitis csynth, not by the local
    # yosys/OpenSTA flow. Seed latency from the project's csynth.xml (when present) so
    # that criterion can actually be evaluated — matching what the optimize path does.
    # Without this, a declared latency target is always "no measurement yet" -> fail.
    seed = QoRMetrics()
    csynth = find_csynth_xml(project_dir)
    if csynth is not None:
        try:
            seed = parse_csynth_xml(csynth)
        except RuntimeError:
            seed = QoRMetrics()  # malformed report: leave latency unmeasured
    metrics, outcome = run_local_ppa(
        project_dir,
        config.top,
        config.clock,
        liberty=config.liberty,
        node=config.node,
        clock_port=clock_port,
        gate_sim=gate_sim,
        sta_bin=sta_bin,
        metrics=seed,
        verbose=verbose,
    )
    unit = outcome.time_unit
    report_path = project_dir / "ppa_report.json"
    payload: dict[str, object] = {
        "node": outcome.node or config.node,
        "time_unit": unit,
        "criteria": targets.to_dict(),
        "outcome": outcome.to_dict(),
        "metrics": metrics.to_dict() if metrics is not None else None,
    }
    if metrics is None or outcome.status != "ok":
        note = outcome.note or outcome.status
        status = "fail" if (outcome.status == "fail" or targets.specified) else "skipped"
        payload["status"] = status
        report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return PhaseResult("ppa", status, summary=f"node {config.node}: {note}")

    parts = [f"node {outcome.node}"]
    headroom = slack_headroom(metrics, targets)
    if metrics.sta_worst_slack_max_ns is not None:
        parts.append(f"worst slack {metrics.sta_worst_slack_max_ns:g} {unit}")
        parts.append(f"slack headroom {headroom:+.3f} {unit} (iteration budget)")
    if metrics.yosys_area_um2 is not None:
        parts.append(f"area {metrics.yosys_area_um2:g} um^2")
    if metrics.sta_total_power_w is not None:
        parts.append(f"power {metrics.sta_total_power_w:.3g} W")
    if outcome.gate_sim != "skipped":
        parts.append(f"gate-sim {outcome.gate_sim}")
    payload["slack_headroom"] = headroom

    if outcome.gate_sim == "fail":
        payload["status"] = "fail"
        report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return PhaseResult("ppa", "fail", summary="; ".join(parts) + f" — gate-level sim FAILED: {outcome.gate_sim_note}")
    if targets.specified:
        met, gaps, _gap_score = evaluate_targets(metrics, targets, time_unit=unit)
        if not met:
            payload["status"] = "fail"
            payload["gaps"] = gaps
            report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            return PhaseResult("ppa", "fail", summary="; ".join(parts) + " — criteria NOT met: " + "; ".join(gaps))
        parts.append("criteria met")
    payload["status"] = "pass"
    report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return PhaseResult("ppa", "pass", summary="; ".join(parts))


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


def _optimize_local_hls_baseline(project_dir: Path, config, analysis, verbose: bool = False) -> int:
    """`optimize` under the local-hls backend: Bambu ignores HLS performance pragmas,
    so a pragma-variant search cannot move QoR. Synthesize the local RTL if needed and
    report its baseline PPA (graded against any configured criteria) instead."""

    rtl = sorted((project_dir / "rtl").glob("*.v"))
    if not rtl:
        backend = LocalHlsCosim.from_config(config, analysis, project_dir)
        if backend is None:
            _, reason = local_hls_available()
            raise SystemExit(f"no rtl/ to measure and local-hls is unavailable: {reason}")
        print("optimize[local-hls]: no rtl/ found; synthesizing with Bambu for a baseline...")
        phases = backend.run(project_dir)
        if phases["csynth"].status != "pass":
            raise SystemExit(f"local-hls synthesis failed: {phases['csynth'].summary}")
    config.run_local_ppa = True
    phase = _ppa_gate_phase(project_dir, config, verbose=verbose, clock_port="clock", gate_sim=False)
    print(
        "\noptimize[local-hls]: the Bambu backend ignores HLS performance pragmas, so the "
        "pragma-driven QoR search does not apply. Reporting the baseline PPA of the local "
        "RTL; use --cosim-backend vitis/vitis-ssh to search pragma candidates.\n"
    )
    print(f"baseline PPA[{config.node}]: {phase.status} — {phase.summary}")
    (project_dir / "qor_report.json").write_text(
        json.dumps(
            {
                "backend": "local-hls",
                "optimization": "not_applicable",
                "reason": "Bambu ignores HLS performance pragmas; no pragma-driven QoR search is possible.",
                "baseline_ppa_status": phase.status,
                "baseline_ppa": phase.summary,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # A "fail" here means a declared PPA criterion is unmet and this backend cannot
    # optimize toward it — surface that as a non-zero exit.
    return 1 if phase.status == "fail" else 0


def run_optimize(args: argparse.Namespace) -> int:
    from .qor import PPATargets
    from .qor_optimizer import optimize_project

    project_dir = Path(args.project).resolve()
    if not (project_dir / "src" / "hls_top.cpp").exists():
        raise SystemExit(f"{project_dir} does not look like a generated project (no src/hls_top.cpp)")
    config = merge_cli_config(load_config(Path(args.config).resolve() if args.config else None), args)
    # CLI target flags override the config's hardwired `ppa:` criteria; unset flags
    # fall back to the config so the workflow criteria apply without re-typing them.
    targets = PPATargets(
        max_latency_cycles=args.target_latency if args.target_latency is not None else config.max_latency_cycles,
        min_slack_ns=args.target_slack if args.target_slack is not None else config.min_slack,
        max_area_um2=args.target_area if args.target_area is not None else config.max_area_um2,
        max_power_w=args.target_power if args.target_power is not None else config.max_power_w,
    )
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
    if resolve_cosim_backend(config, remote) == "local-hls":
        # Bambu ignores HLS performance pragmas, so the pragma-variant candidate search
        # cannot change the RTL. Report the baseline PPA of the local RTL instead of
        # running a degenerate loop; pragma optimization requires the Vitis backend.
        return _optimize_local_hls_baseline(project_dir, config, analysis, args.verbose)
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
            local_ppa=args.local_ppa or config.run_local_ppa,
            liberty=args.liberty or config.liberty,
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


def run_ppa(args: argparse.Namespace) -> int:
    """Standalone PPA criteria check: the edit-RTL -> `make ppa` -> read-headroom loop."""

    project_dir = Path(args.project).resolve()
    config = merge_cli_config(load_config(Path(args.config).resolve() if args.config else None), args)
    if getattr(args, "clock", None) is not None:
        config.clock = float(args.clock)
    if not config.top:
        config.top = _load_project_top(project_dir)
    if not config.top:
        raise SystemExit("--top is required because conversion_report.json does not record a top function")
    config.run_local_ppa = True
    phase = _ppa_gate_phase(
        project_dir, config, verbose=args.verbose, clock_port=args.clock_port,
        gate_sim=not args.no_gate_sim, sta_bin=args.sta_bin,
    )
    print(f"PPA[{config.node}]: {phase.status} — {phase.summary}")
    print(f"Report: {project_dir / 'ppa_report.json'}")
    return 0 if phase.status in ("pass", "skipped") else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "convert":
        return run_convert(args)
    if args.command == "repair":
        return run_repair(args)
    if args.command == "optimize":
        return run_optimize(args)
    if args.command == "ppa":
        return run_ppa(args)
    if args.command == "cross-reference":
        from .cross_reference import run_cross_reference

        return run_cross_reference(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
