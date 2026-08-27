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
| Tests | 300 passed, 3 skipped, offline |
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

## Three bugs fixed on the way here

Found by running `refine` through the CLI on a guarded design rather than through the
library. All three are on this branch; you will pull them. They matter to you because
two of them sit directly on the path you are about to walk.

1. **`refine` crashed on its own default.** `--input` defaults to `PROJECT/input.c`, and
   regenerating the project copied that file onto itself — `SameFileError`. `write_project`
   now skips the copy when source and destination are the same file, which also keeps the
   golden C byte-identical, as it must be.
2. **`refine` without `--config` silently dropped the argument contract.** Declared ranges
   and lengths were lost, so a scalar used as a loop bound was redrawn over all of `int`
   and the golden testbench read out of bounds — a segfault, reported only as "coverage:
   None". Projects now carry `tb/stimulus_contract.json` and `refine` reads it back.
   Older projects without one get a warning telling you to pass `--config`.
3. **A KLEE timeout produced a traceback instead of a report.** `TimeoutExpired` carries
   *undecoded bytes* even from a text-mode `subprocess.run`, and those bytes reached
   `json.dumps`. `coverage/klee_report.json` — the file Task A asks you to read — was
   never written. It is now written for every outcome.

## Two tasks

They are separate, and only one of them changes what the agent can do.

| | Task | Kind | Needs Docker? |
|---|---|---|---|
| **B** | Get KLEE actually running on Windows, via the `klee/klee` container | capability — symbolic stimulus does not work there today | yes, that is the task |
| **A** | Explain why the container route is refused by the *last* guard instead of the right one | diagnostic — behaviour is already correct | yes, to reproduce at all |

**Do B first.** It is short, it is the one with a user-visible payoff, and it is the
only way to get a machine where the container route can genuinely succeed — which is
what tells a wrong guard from a right one in A. Both are below, B first.

If Docker cannot be installed on that machine, neither task is possible. That is a
valid outcome: report it and change nothing.

## Task B — actually get KLEE running on that Windows box

Task A is diagnostic: the guards are wrong internally but the outcome is correct.
Task B is capability: **KLEE never actually runs on Windows today**, so
coverage-driven refinement silently degrades to widening the random schedule
instead of solving for the branch it cannot reach. Task B is the one that changes
what the agent can do. Do it first — it is short, and it also gives Task A a
machine where the container route can genuinely succeed, which is the only way to
tell a wrong guard from a right one.

KLEE has **no Windows build**. The container is not a workaround, it is the
supported route.

### Steps

1. Install Docker Desktop and put it in **Linux container** mode (right-click the
   tray whale → "Switch to Linux containers…" if the menu offers it; if it offers
   "Switch to Windows containers" instead, you are already in Linux mode).

   ```powershell
   winget install Docker.DockerDesktop
   ```

2. Confirm the daemon reports Linux. This is guard 1, verbatim:

   ```powershell
   docker info --format "{{.OSType}}"    # must print: linux
   ```

3. Pull the image **by hand**. The agent will not do this for you — the route
   never pulls on its own, by design, and that is not a bug to fix:

   ```powershell
   docker pull klee/klee:latest
   ```

   It is a multi-gigabyte download.

4. Confirm the agent now sees it:

   ```powershell
   python -m c2hlsc_agent doctor --tier symbolic --json
   ```

   `daemon` must be `ok`, `os_type` must be `linux`, `image_present` must be `true`.

5. Prove the container route is the one being taken. This is the `klee-coverage`
   target, which is what writes `coverage\klee_report.json`:

   ```powershell
   python -m c2hlsc_agent convert --input examples\vector_add\input.c --top vector_add `
     --config examples\vector_add\config.yaml --out build\probe
   cd build\probe
   python tb\host_build.py klee-coverage
   type coverage\klee_report.json
   ```

   It must show `"mode": "docker"`. `"status"` should be `pass`; if it is `fail` with
   `"reason": "timeout"`, raise the budget — `$env:C2HLSC_KLEE_TIMEOUT = "600"` — and
   re-run. 60 seconds is the default and a container adds startup on top of it.

6. Prove it end to end on a design that random stimulus cannot cover. Write this to
   `probe\input.c`:

   ```c
   #include <stdint.h>

   void guarded_scale(const int32_t *a, int32_t *out, int n) {
     for (int i = 0; i < n; ++i) {
       if (a[i] == 12345) {
         out[i] = a[i] * 2;
       } else {
         out[i] = a[i] + 1;
       }
     }
   }
   ```

   with `probe\config.yaml`:

   ```yaml
   input_files: [input.c]
   top: guarded_scale
   num_tests: 64
   seed: 7
   interface_mode: ap_memory
   arguments:
     a: {direction: input, length: 16}
     out: {direction: output, length: 16}
     n: {range: [0, 16]}
   ```

   then:

   ```powershell
   python -m c2hlsc_agent convert --input probe\input.c --top guarded_scale `
     --config probe\config.yaml --out build\guarded
   python -m c2hlsc_agent refine --project build\guarded --target 100 --verbose
   type build\guarded\coverage_refinement.json
   ```

   Note the artifact: `refine` drives KLEE through the library, not through the
   `klee-coverage` target, so its evidence is **`coverage_refinement.json`** —
   `klee_report.json` is written only by step 5. Look for
   `"strategy": "klee"` on the round, not `"widen"`.

### What success looks like

Reproduced on the Linux container against a real KLEE 3.3-pre build, through the CLI:

```
baseline gate coverage: 75.0
round 0 [klee]: gate coverage 100.0 (+64 vector(s))
Reached 100.00% gate coverage in 1 round(s) (target 100.00%).
```

The guard `a[i] == 12345` is unreachable for the random schedule; KLEE produced the
counterexample, `parse_ktest()` decoded it, and it was written back as a permanent
directed vector. If Windows produces the same jump with `"strategy": "klee"`, the
container route works there.

If the round says `"strategy": "widen"`, KLEE was never reached and this is still
Task A — the widening fallback is what runs when no KLEE is usable, and it reaches
100% on this design too, so read the strategy rather than the coverage number.

### Bind Task B back into Task A

Once step 4 reports `image_present: true`, re-run the Task A probes below on that
same machine. With the image genuinely local, guard 2 should say "present" for a
real reason rather than a wrong one, and any remaining discrepancy between
`container_diagnostics()` and the raw `docker` output is now unambiguous — it is
the bug. Report both readings, before and after the pull.

### Constraints specific to Task B

- Do **not** teach the automatic route to pull. If the fix you are tempted by is
  "just pull the image when it's missing", that is the 206-second CI regression
  being reintroduced. The manual pull is the contract.
- Do not make Docker a hard requirement anywhere. A machine without it must still
  convert, verify, and refine — with widening instead of symbolic stimulus.
- If Docker Desktop cannot be installed on that machine (licensing, policy, disk),
  stop and say so. Report it on PR #23 and leave Task A unattempted; without a
  working container there is nothing to diagnose.

## Task A — find out which guard is actually refusing the route

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

### How to reproduce

Requires Docker Desktop, i.e. Task B step 1. Without it `docker_available()` returns
"not installed" immediately and the whole path is skipped — there is nothing to observe.
Run these both **before** the Task B pull and **after**; the pair of readings is the
evidence, because only the second one has a legitimate reason to say the image is there.

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

### Leading hypotheses, in order

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

## Constraints — both tasks

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

**Task B** — no code change is expected; this is configuration plus proof:

- `doctor --tier symbolic --json` reports `daemon: ok`, `os_type: linux`,
  `image_present: true`.
- The `klee-coverage` target writes `coverage/klee_report.json` with `"mode": "docker"`.
- A `refine` run on the guarded design writes `coverage_refinement.json` with a round
  whose `"strategy"` is `"klee"`, and coverage moves 75% -> 100%.
- Both readings pasted onto PR #23, so the container route is documented as working on
  Windows rather than assumed.

**Task A** — this one does change code:

- The container route is refused by the guard that *should* refuse it, with a reason that
  matches the raw probe output.
- A regression test covering whatever the real Windows behaviour turns out to be, added
  to `KleeContainerFallbackTests`.

**Both:**

- `python -m unittest discover -s tests` green on Windows.
- `ruff check .` clean.
- Push to `claude/agent-component-scaffold-5cr39w`; PR #23 picks it up.
- If Docker cannot be installed on that machine: report that on PR #23, change nothing,
  and stop. Neither task is possible without it, and that is a valid outcome.
