# Handoff — KLEE container route on Windows

For a Claude Code instance running on the **Windows** machine. Everything below is
verified against CI logs and the working tree, not recalled.

## State

| Field | Value |
|---|---|
| Repo | `huluk98/c2hlsc-agent` |
| Branch | `claude/agent-component-scaffold-5cr39w` |
| Pull request | [#23](https://github.com/huluk98/c2hlsc-agent/pull/23) (draft, unmerged) |
| Base | `main` @ `0aca96e` — none of this is on `main` yet |
| CI on the branch head | **all six checks green**, including `Windows / Python 3.12` |
| Tests | 291 passed, 3 skipped, offline |
| Lint | `ruff check .` clean (CI gates on `F`, `E9`, `B904`) |

Pull it:

```powershell
git clone https://github.com/huluk98/c2hlsc-agent.git
cd c2hlsc-agent
git checkout claude/agent-component-scaffold-5cr39w
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
```

`setup_windows.ps1` installs the package, reports the toolchain, and runs the suite.
You need Python 3.10+ and a GCC/Clang-style compiler (`winget install LLVM.LLVM`).
**MSVC will not work** — the generated projects use GCC-style flags, and `tb/host_build.py`
detects `cl.exe` and refuses rather than mistranslating them.

`make` is **not** required. Every recipe lives in `tb/host_build.py`, which the agent runs
with its own interpreter; the Makefile is a thin alias.

## The issue

**Nothing is broken.** Behaviour is correct and CI is green. This is an unexplained
*internal* result, and leaving it unexplained is the risk: the container route has three
independent preconditions, and right now only the last line of defence is demonstrably
doing the work.

### What the route is

`tb/run_klee.py` runs KLEE natively when it is installed. KLEE has no Windows build, so
where it is absent the script falls back to the official `klee/klee` container. That
fallback is guarded three ways, in this order:

1. **`docker_available()`** — CLI exists, a daemon answers, and that daemon runs **Linux**
   containers (`docker info --format {{.OSType}}`). `klee/klee` is a Linux image.
2. **`image_present()`** — the image is already local (`docker image inspect`). The route
   deliberately **never pulls**: an unrequested multi-gigabyte download once turned a
   35-second CI suite into a 206-second one.
3. **`_container_failed()`** — if the container is reached and fails anyway, an
   *automatic* attempt degrades to `skipped` with exit 0, because an absent optional tool
   must never fail a build. Only a **forced** attempt (`C2HLSC_KLEE_DOCKER=1`) reports
   failure.

### What CI actually showed

On `windows-latest`, job `Windows / Python 3.12`:

```
KLEE coverage skipped: container unavailable (container exited 125) -- c2hlsc-agent doctor --install
```

That message is emitted by **guard 3**. For it to be reached, guards 1 and 2 must both
have said "usable":

- guard 1 concluded the daemon runs Linux containers, and
- guard 2 concluded `klee/klee:latest` was already present on a fresh runner

The second is implausible — a fresh runner has not pulled that image. So at least one
guard is returning a wrong answer on Windows, and the only reason the job is green is
that guard 3 caught it.

**The ask:** find out which guard is wrong and why, then fix it so the route is refused
by the *correct* guard with an accurate reason. Guard 3 must remain as the backstop; it
should just stop being the thing that does the work.

## How to reproduce

Requires **Docker Desktop** on the Windows box. Without Docker installed you cannot
reproduce this — `docker_available()` returns "not installed" immediately and the whole
path is skipped. That is a valid finding to report back, not a failure.

`doctor` now probes all three preconditions separately, which is the fastest way in:

```powershell
python -m c2hlsc_agent doctor --tier symbolic
python -m c2hlsc_agent doctor --tier symbolic --json    # full detail incl. stderr
```

Expected shape:

```json
{
  "image": "klee/klee:latest",
  "cli": "C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe",
  "daemon": "ok",
  "os_type": "windows",
  "image_present": false
}
```

Then run the raw probes yourself and compare — the discrepancy is the bug:

```powershell
docker info --format "{{.OSType}}"      # what does it ACTUALLY print on Windows?
echo "info exit: $LASTEXITCODE"
docker image inspect klee/klee:latest
echo "inspect exit: $LASTEXITCODE"
```

Then the end-to-end behaviour, from a generated project:

```powershell
python -m c2hlsc_agent convert --input examples\vector_add\input.c --top vector_add `
  --config examples\vector_add\config.yaml --out build\probe
cd build\probe
python tb\host_build.py klee-coverage
type coverage\klee_report.json
```

`klee_report.json` records `mode` (`native` / `docker` / `none`), `status`, and `reason`.
Whichever guard should have refused the route will be visible by comparing that `reason`
against the raw probe output above.

## Leading hypotheses, in order

1. **PowerShell mangles the `--format` argument.** `{{.OSType}}` contains braces that
   PowerShell may interpret. The agent calls docker through `subprocess.run` with a list
   (no shell), so this should not apply — but confirm `info_stderr` in the JSON is empty
   and `os_type` is a bare word.
2. **`docker info` succeeds with empty output** in some Docker Desktop states. Guard 1
   treats an empty `os_type` as acceptable (`if os_type and os_type != "linux"`), which
   is deliberately lenient. On Windows that leniency may be wrong — consider treating an
   empty OSType as unusable **on Windows only**, so Linux/macOS behaviour is unchanged.
3. **`docker image inspect` exits 0 unexpectedly.** Least likely, but it is the guard
   whose CI result is hardest to explain. Check `inspect_returncode` and `inspect_stderr`
   in the JSON.

## Constraints

From `AGENTS.md`, and non-negotiable here:

- **An absent optional tool must never fail a build.** Whatever changes, `klee-coverage`
  with no usable KLEE still reports `skipped` and exits 0. Guard 3 stays.
- **The route must never pull.** Automatic use requires an already-local image. Only
  `C2HLSC_KLEE_DOCKER=1` may pull, and only then is a failure reported as `fail`.
- Native KLEE keeps precedence over the container in every case.
- Do not weaken or skip a test to make this green; it is already green.
- Offline tests must stay offline: no network, no SDK, no API key.

## Files

| File | Role |
|---|---|
| `c2hlsc_agent/leveri_testgen.py` | `_klee_script()` generates `tb/run_klee.py` — the guards live in that generated text |
| `c2hlsc_agent/toolchain.py` | `container_diagnostics()`, the probe `doctor` reports |
| `c2hlsc_agent/cli.py` | `run_doctor()` renders it |
| `tests/test_shift_left.py` | `KleeContainerFallbackTests`, `ToolchainTests` — eight scenarios already pinned |

The generated script is a raw string inside `_klee_script()`. Edit it there, not in a
generated project, or the change is lost on the next `write_project`. `tests/` has
fixtures that assert the generated text parses.

## Definition of done

- The container route is refused by the guard that *should* refuse it, with a reason that
  matches the raw probe output.
- `python -m unittest discover -s tests` green on Windows.
- `ruff check .` clean.
- A regression test covering whatever the real Windows behaviour turns out to be, added
  to `KleeContainerFallbackTests`.
- Push to `claude/agent-component-scaffold-5cr39w`; PR #23 picks it up.
- If Docker is not installed on that machine: report that, change nothing, and say so on
  the PR.
