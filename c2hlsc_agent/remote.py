"""Run the Vitis phases of the verifier ladder on a remote Linux host over SSH.

Design goal: ONLY ``vitis_hls`` leaves the local machine. Analysis, generation, testbench
emission, host equivalence (``make test``), classification, and LLM repair all stay local;
the project directory is rsynced to the remote host, each phase runs as
``ssh <host> 'cd <dir> && timeout <t> vitis_hls -f run_<phase>.tcl'`` with the log
captured locally exactly like a local run, and the synthesis/cosim artifacts are pulled
back afterwards.

The remote host needs: ssh key auth, rsync, and Vitis HLS. ``vitis_hls`` is located via
(in order) an explicit ``vitis_setup`` shell prefix, an explicit ``vitis_bin`` path, or a
probe of the common Xilinx install locations (same list as scripts/run_vitis_linux.sh).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .equivalence import PhaseResult, run_command

# Common settings64 locations, probed on the remote host when no explicit setup is given.
_SETTINGS_CANDIDATES = tuple(
    f"{root}/Xilinx/{tool}/{version}/{script}"
    for root in ("/tools", "/opt")
    for version in ("2024.2", "2024.1", "2023.2", "2022.1")
    for tool, script in (("Vitis", "settings64.sh"), ("Vitis_HLS", ".settings64-Vitis_HLS.sh"))
)

# Grace added to the local ssh timeout on top of the remote `timeout`, so the remote
# guard fires first and the log tail still reaches the local side.
_SSH_GRACE = 60


@dataclass(frozen=True)
class RemoteVitis:
    """SSH target for the Vitis phases."""

    host: str
    remote_dir: str = "~/c2hlsc_runs"
    setup: str | None = None
    vitis_bin: str = "vitis_hls"

    @classmethod
    def from_config(cls, config: object) -> "RemoteVitis | None":
        host = getattr(config, "vitis_ssh_host", None) or os.environ.get("C2HLSC_VITIS_SSH")
        if not host:
            return None
        return cls(
            host=host,
            remote_dir=getattr(config, "vitis_remote_dir", None) or "~/c2hlsc_runs",
            setup=getattr(config, "vitis_setup", None),
            vitis_bin=getattr(config, "vitis_bin", None) or "vitis_hls",
        )

    def remote_project_dir(self, project_dir: Path) -> str:
        """Remote path for this project. ``~/`` is normalized to a home-relative path so
        it survives shell quoting; ssh commands start in $HOME and rsync resolves
        relative remote paths against $HOME, so both agree on the location.

        The leaf is ``<basename>-<hash>`` where the hash derives from the ABSOLUTE local
        path, so two projects that share a basename (e.g. ``fir/out`` and ``aes/out``) —
        or two concurrent runs — never collide on the remote under ``--delete``.
        """

        base = self.remote_dir.rstrip("/")
        if base == "~":
            base = ""
        elif base.startswith("~/"):
            base = base[2:]
        digest = hashlib.sha1(str(project_dir.resolve()).encode("utf-8")).hexdigest()[:8]
        leaf = f"{project_dir.name}-{digest}"
        return f"{base}/{leaf}" if base else leaf

    def _env_snippet(self) -> str:
        if self.setup:
            return f"{self.setup} && "
        if self.vitis_bin != "vitis_hls" or "/" in self.vitis_bin:
            return ""
        probe = " ".join(shlex.quote(c) for c in _SETTINGS_CANDIDATES)
        # Emit the exact "vitis_hls not found" marker on a probe miss so the local
        # classifier maps it to toolchain_unavailable (blocked, no source mutation).
        return (
            "if ! command -v vitis_hls >/dev/null 2>&1; then "
            f"for s in {probe}; do [ -f \"$s\" ] && . \"$s\" && break; done; fi && "
            "{ command -v vitis_hls >/dev/null 2>&1 || "
            "{ echo 'vitis_hls not found on remote (probed settings64 locations); "
            "pass --vitis-setup or --vitis-bin' >&2; exit 127; }; } && "
        )

    def phase_script(self, project_dir: Path, phase: str, timeout: int) -> str:
        rdir = self.remote_project_dir(project_dir)
        # -k 30s: if vitis_hls ignores SIGTERM at the deadline, SIGKILL it 30s later so
        # the ssh session actually closes instead of hanging on the local grace timeout.
        return (
            f"cd {shlex.quote(rdir)} && {self._env_snippet()}"
            f"timeout -k 30s {int(timeout)}s {shlex.quote(self.vitis_bin)} -f run_{phase}.tcl"
        )

    def phase_command(self, project_dir: Path, phase: str, timeout: int) -> list[str]:
        return ["ssh", self.host, "bash", "-lc", shlex.quote(self.phase_script(project_dir, phase, timeout))]

    def push(self, project_dir: Path) -> PhaseResult:
        """Sync the local project to the remote host (clean slate for the Vitis run)."""

        rdir = self.remote_project_dir(project_dir)
        mkdir = subprocess.run(
            ["ssh", self.host, f"mkdir -p {shlex.quote(rdir)}"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if mkdir.returncode != 0:
            return PhaseResult(
                "vitis_push",
                "fail",
                mkdir.returncode,
                mkdir.stdout,
                mkdir.stderr,
                summary=f"ssh mkdir on {self.host} failed: {mkdir.stderr.strip()[-400:]}",
            )
        return run_command(
            [
                "rsync",
                "-az",
                "--delete",
                "--exclude",
                ".candidates/",
                "--exclude",
                ".qor/",
                "--exclude",
                "c2hlsc_project/",
                # Never ship the local runner-written phase logs (software_equivalence.log,
                # csim.log, ...): the remote regenerates its own, and re-pulling these stale
                # copies would clobber the fresh local logs the repair agent reads.
                "--exclude",
                "*.log",
                f"{project_dir}/",
                f"{self.host}:{shlex.quote(rdir)}/",
            ],
            project_dir,
            "vitis_push",
            timeout=600,
        )

    def pull(self, project_dir: Path) -> PhaseResult:
        """Pull synthesis/cosim artifacts and Vitis-side reports back to the local project.

        Best-effort: brings back the generated RTL (``syn/``) and the Vitis-side logs and
        reports under ``c2hlsc_project/`` so the local report is complete. The root
        ``<phase>.log`` files are deliberately NOT pulled — ``run_command`` already wrote
        them locally with this run's ssh console output, and pulling would overwrite that
        fresh evidence with whatever stale copy happens to sit on the remote.
        """

        rdir = self.remote_project_dir(project_dir)
        return run_command(
            [
                "rsync",
                "-az",
                "--prune-empty-dirs",
                "--include",
                "*/",
                "--include",
                "c2hlsc_project/solution1/syn/**",
                "--include",
                "c2hlsc_project/solution1/sim/report/**",
                "--include",
                "c2hlsc_project/**/*.log",
                "--include",
                "c2hlsc_project/**/*.rpt",
                "--exclude",
                "*",
                f"{self.host}:{shlex.quote(rdir)}/",
                f"{project_dir}/",
            ],
            project_dir,
            "vitis_pull",
            timeout=600,
        )

    def run_phase(self, project_dir: Path, phase: str, timeout: int) -> PhaseResult:
        """Run one Vitis phase remotely; the log lands locally like a local run."""

        result = run_command(
            self.phase_command(project_dir, phase, timeout),
            project_dir,
            phase,
            timeout=timeout + _SSH_GRACE,
        )
        # `timeout` exits 124 (SIGTERM) or 137 (SIGKILL) when the deadline fires; relabel
        # so classify_log_family maps it to timeout_or_deadlock, matching the local path.
        if result.status != "pass" and result.returncode in (124, 137):
            result.summary = f"Vitis {phase} timed out after {timeout}s on {self.host}"
        # ssh transport failure (exit 255) is infrastructure, not a code defect: mark it
        # so classify_failure treats it as toolchain_unavailable (blocked, no repair).
        elif result.returncode == 255:
            result.summary = f"remote vitis unavailable: ssh to {self.host} failed for {phase} ({result.stderr.strip()[-300:]})"
        return result
