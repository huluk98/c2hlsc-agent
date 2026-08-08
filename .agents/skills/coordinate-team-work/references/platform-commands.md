# Platform commands

Run commands from the repository root. The workflow is the same on every platform; only shell syntax and the common Python launcher differ.

## Native Windows PowerShell

One-time setup:

```powershell
gh auth login
gh repo clone huluk98/c2hlsc-agent
Set-Location c2hlsc-agent
git config user.name 'YOUR NAME'
git config user.email 'YOUR_EMAIL'
python -m pip install -r requirements.txt
python -m pip install -e .
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\team_preflight.ps1 -RunTests
```

If `python` is unavailable but the Python launcher is installed, substitute `py -3`.

Issue preflight:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\team_preflight.ps1 -Issue ISSUE_NUMBER
```

Clean-main synchronization:

```powershell
git status --short --branch
git fetch origin --prune
git switch main
git pull --ff-only origin main
```

## Ubuntu or WSL Bash

One-time setup:

```bash
gh auth login
gh repo clone huluk98/c2hlsc-agent
cd c2hlsc-agent
git config user.name 'YOUR NAME'
git config user.email 'YOUR_EMAIL'
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
bash scripts/team_preflight.sh --run-tests
```

Issue preflight:

```bash
bash scripts/team_preflight.sh --issue ISSUE_NUMBER
```

Clean-main synchronization:

```bash
git status --short --branch
git fetch origin --prune
git switch main
git pull --ff-only origin main
```

## Verify repository guardrails

Windows:

```powershell
python scripts\verify_github_guardrails.py
```

Ubuntu or WSL:

```bash
python3 scripts/verify_github_guardrails.py
```

The command is read-only and exits nonzero if protected `main` is missing the stable `ci` check, pull-request review, latest-push approval, resolved-conversation, force-push, or deletion controls.
