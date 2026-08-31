<#
.SYNOPSIS
    One-command setup and check for a fresh Windows clone of c2hlsc-agent.

.DESCRIPTION
    Native Windows only -- no MSYS, no Cygwin, no WSL. `make` is NOT required: every
    generated project recipe lives in tb/host_build.py, which the agent runs with its own
    interpreter.

    This installs the package, reports which external tools each verification tier needs,
    and runs the offline test suite.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1

.EXAMPLE
    # Also install the missing tools it finds, via winget.
    powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -InstallTools
#>
[CmdletBinding()]
param(
    [switch]$InstallTools,
    [switch]$SkipTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Find-Command {
    param([Parameter(Mandatory = $true)][string[]]$Names)
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    return $null
}

$repoRoot = (& git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $repoRoot) {
    throw 'Run this from inside the c2hlsc-agent checkout.'
}
Set-Location -LiteralPath $repoRoot.Trim()

Write-Host '== Python ==' -ForegroundColor Cyan
# The agent passes its own interpreter through to every generated script, so whichever
# name Python has here is the one that gets used. `python3` need not exist.
$python = Find-Command @('python', 'py', 'python3')
if (-not $python) {
    Write-Host 'Python was not found.' -ForegroundColor Red
    Write-Host '  winget install Python.Python.3.12'
    exit 1
}
& $python --version

Write-Host ''
Write-Host '== Installing c2hlsc-agent (editable) ==' -ForegroundColor Cyan
& $python -m pip install --disable-pip-version-check -e . | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

Write-Host ''
Write-Host '== C++ compiler ==' -ForegroundColor Cyan
# Generated projects use GCC-style flags. MSVC (cl.exe) is deliberately not usable:
# its flag syntax is different and mistranslating flags fails confusingly.
$cxx = Find-Command @('g++', 'clang++', 'c++')
if ($cxx) {
    Write-Host "  found $cxx" -ForegroundColor Green
} else {
    Write-Host '  no GCC/Clang-style compiler found.' -ForegroundColor Yellow
    Write-Host '    winget install LLVM.LLVM        (provides clang++)'
    Write-Host '    - or MSYS2 mingw-w64-gcc        (provides g++)'
    Write-Host '  MSVC (cl.exe) will not work: incompatible flag syntax.'
}

Write-Host ''
Write-Host '== Tool check (c2hlsc-agent doctor) ==' -ForegroundColor Cyan
$doctorArgs = @('-m', 'c2hlsc_agent', 'doctor')
if ($InstallTools) { $doctorArgs += '--install' }
& $python @doctorArgs
$doctorExit = $LASTEXITCODE

if (-not $SkipTests) {
    Write-Host ''
    Write-Host '== Offline test suite ==' -ForegroundColor Cyan
    # No network, no API key, no Vitis. Tests needing an absent tool skip and say so.
    & $python -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw 'Test suite failed.' }
}

Write-Host ''
if ($doctorExit -eq 0) {
    Write-Host 'Ready. Try:' -ForegroundColor Green
} else {
    Write-Host 'Set up, but a core tool is missing (see doctor output above). Once it is installed:' -ForegroundColor Yellow
}
Write-Host '  python -m c2hlsc_agent convert --input examples\vector_add\input.c --top vector_add --config examples\vector_add\config.yaml --out build\vector_add'
