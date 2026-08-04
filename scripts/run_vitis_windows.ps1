[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [Parameter(Mandatory = $true)]
    [string]$Out,

    [string]$Python = "python",
    [string]$VitisSettings = $(
        if ($env:VIVADO_SETTINGS) { $env:VIVADO_SETTINGS } else { $env:VITIS_SETTINGS }
    ),
    [string]$VitisBin = $(
        if ($env:VIVADO_HLS_BIN) { $env:VIVADO_HLS_BIN } else { $env:VITIS_HLS_BIN }
    ),
    [string]$VitisRoot = $(
        if ($env:VIVADO_ROOT) { $env:VIVADO_ROOT } else { $env:VITIS_HLS_ROOT }
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-LeafPath {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $resolved = Resolve-Path -LiteralPath $Value -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $resolved.Path -PathType Leaf)) {
        throw "$Label is not a file: $Value"
    }
    return $resolved.Path
}

function Resolve-DirectoryPath {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $resolved = Resolve-Path -LiteralPath $Value -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $resolved.Path -PathType Container)) {
        throw "$Label is not a directory: $Value"
    }
    return $resolved.Path
}

function Get-InstallRoots {
    param([string]$RequestedRoot)

    $roots = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($RequestedRoot)) {
        [void]$roots.Add((Resolve-DirectoryPath $RequestedRoot "VITIS_HLS_ROOT"))
    }
    foreach ($knownRoot in @("C:\AMD", "C:\Xilinx")) {
        if (Test-Path -LiteralPath $knownRoot -PathType Container) {
            [void]$roots.Add($knownRoot)
        }
    }
    return @($roots | Select-Object -Unique)
}

function Get-VersionDirectories {
    param([string[]]$Roots)

    $versions = New-Object System.Collections.Generic.List[string]
    foreach ($root in $Roots) {
        [void]$versions.Add($root)
        foreach ($product in @("Vivado", "Vitis", "Vitis_HLS")) {
            $productRoot = Join-Path $root $product
            if (-not (Test-Path -LiteralPath $productRoot -PathType Container)) {
                continue
            }
            Get-ChildItem -LiteralPath $productRoot -Directory -ErrorAction Stop |
                Sort-Object -Property Name -Descending |
                ForEach-Object { [void]$versions.Add($_.FullName) }
        }
    }
    return @($versions | Select-Object -Unique)
}

function Find-SettingsBatch {
    param(
        [string]$RequestedSettings,
        [string[]]$VersionDirectories
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedSettings)) {
        $settings = Resolve-LeafPath $RequestedSettings "VITIS_SETTINGS"
        if ([IO.Path]::GetExtension($settings) -notin @(".bat", ".cmd")) {
            throw "VITIS_SETTINGS must name a Windows .bat or .cmd file: $settings"
        }
        return $settings
    }
    foreach ($version in $VersionDirectories) {
        $candidate = Join-Path $version "settings64.bat"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-LeafPath $candidate "Vitis settings batch")
        }
    }
    return $null
}

function Import-VitisEnvironment {
    param([Parameter(Mandatory = $true)][string]$Settings)

    # cmd.exe is required only to evaluate AMD's native settings batch. Reject command
    # metacharacters before composing the one command string; normal spaces are safe.
    if ($Settings -match '["&|<>^%\r\n]') {
        throw "VITIS_SETTINGS contains characters unsafe for cmd.exe: $Settings"
    }
    $cmd = Get-Command -Name "cmd.exe" -CommandType Application -ErrorAction Stop
    $commandLine = 'call "' + $Settings + '" >nul && set'
    $environmentLines = & $cmd.Path /d /s /c $commandLine
    if ($LASTEXITCODE -ne 0) {
        throw "AMD settings batch failed with exit code $LASTEXITCODE`: $Settings"
    }

    foreach ($line in $environmentLines) {
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) {
            continue
        }
        $name = $line.Substring(0, $separator)
        $value = $line.Substring($separator + 1)
        if (
            $name -eq "PATH" -or
            $name -eq "LM_LICENSE_FILE" -or
            $name -eq "XILINXD_LICENSE_FILE" -or
            $name.StartsWith("XILINX_") -or
            $name.StartsWith("VITIS_") -or
            $name.StartsWith("RDI_")
        ) {
            Set-Item -Path ("Env:{0}" -f $name) -Value $value
        }
    }
}

function Assert-SupportedLauncher {
    param([Parameter(Mandatory = $true)][string]$Launcher)

    $name = [IO.Path]::GetFileName($Launcher).ToLowerInvariant()
    if ($name -notin @(
        "vitis-run", "vitis-run.bat", "vitis-run.exe",
        "vitis_hls", "vitis_hls.bat", "vitis_hls.exe",
        "vivado_hls", "vivado_hls.bat", "vivado_hls.exe"
    )) {
        throw "Unsupported HLS launcher '$name'; expected vitis-run, vitis_hls, or vivado_hls"
    }
}

function Find-VitisLauncher {
    param(
        [string]$RequestedLauncher,
        [string[]]$VersionDirectories
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedLauncher)) {
        if (Test-Path -LiteralPath $RequestedLauncher -PathType Leaf) {
            $launcher = Resolve-LeafPath $RequestedLauncher "VITIS_HLS_BIN"
            Assert-SupportedLauncher $launcher
            return $launcher
        }
        if ($RequestedLauncher -match '[*?\[\]]') {
            throw "VITIS_HLS_BIN command names may not contain wildcard characters"
        }
        $command = Get-Command -Name $RequestedLauncher -CommandType Application -ErrorAction Stop
        Assert-SupportedLauncher $command.Path
        return $command.Path
    }

    foreach ($name in @(
        "vitis-run.exe", "vitis-run.bat", "vitis-run",
        "vitis_hls.exe", "vitis_hls.bat", "vitis_hls",
        "vivado_hls.exe", "vivado_hls.bat", "vivado_hls"
    )) {
        $command = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            Assert-SupportedLauncher $command.Path
            return $command.Path
        }
    }

    foreach ($version in $VersionDirectories) {
        foreach ($relative in @(
            "bin\vitis-run.exe", "bin\vitis-run.bat",
            "bin\vitis_hls.exe", "bin\vitis_hls.bat",
            "bin\vivado_hls.exe", "bin\vivado_hls.bat"
        )) {
            $candidate = Join-Path $version $relative
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $launcher = Resolve-LeafPath $candidate "Vitis HLS launcher"
                Assert-SupportedLauncher $launcher
                return $launcher
            }
        }
    }
    throw "No native AMD HLS launcher was found. Set VIVADO_HLS_BIN, VIVADO_SETTINGS, or VIVADO_ROOT."
}

function Resolve-Application {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (Test-Path -LiteralPath $Value -PathType Leaf) {
        return (Resolve-LeafPath $Value $Label)
    }
    if ($Value -match '[*?\[\]]') {
        throw "$Label command names may not contain wildcard characters"
    }
    $command = Get-Command -Name $Value -CommandType Application -ErrorAction Stop
    return $command.Path
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Resolve-HostTool {
    param(
        [Parameter(Mandatory = $true)][string[]]$Names,
        [Parameter(Mandatory = $true)][string]$Label
    )

    foreach ($name in $Names) {
        $command = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return $command.Path
        }
    }
    throw "$Label was not found after loading the AMD environment (tried: $($Names -join ', '))"
}

function Get-LauncherVersion {
    param([Parameter(Mandatory = $true)][string]$Launcher)

    $name = [IO.Path]::GetFileName($Launcher).ToLowerInvariant()
    $arguments = if ($name.StartsWith("vitis-run")) {
        @("--version")
    } elseif ($name.StartsWith("vitis_hls")) {
        @("-version")
    } else {
        @("-v")
    }
    $output = & $Launcher @arguments 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($output)) {
        throw "Unable to record the AMD HLS version from $Launcher"
    }
    return $output.Trim()
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$configPath = if ([IO.Path]::IsPathRooted($Config)) {
    Resolve-LeafPath $Config "config"
} else {
    Resolve-LeafPath (Join-Path $repoRoot $Config) "config"
}
$outPath = if ([IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path $repoRoot $Out }
$pythonPath = Resolve-Application $Python "Python"
$roots = Get-InstallRoots $VitisRoot
$versions = Get-VersionDirectories $roots
$settings = Find-SettingsBatch $VitisSettings $versions
if ($null -ne $settings) {
    Write-Host "Loading AMD environment from: $settings"
    Import-VitisEnvironment $settings
}
$launcher = Find-VitisLauncher $VitisBin $versions
$makePath = Resolve-HostTool @("make.exe", "make") "GNU Make"
$cxxPath = Resolve-HostTool @("g++.exe", "g++", "clang++.exe", "clang++") "a C++17 compiler"
$launcherVersion = Get-LauncherVersion $launcher
$env:CXX = $cxxPath
$env:PYTHON = $pythonPath
Write-Host "Using native AMD HLS launcher: $launcher"
Write-Host "Using GNU Make: $makePath"
Write-Host "Using C++ compiler: $cxxPath"

$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $repoRoot
    Invoke-NativeChecked -Executable $pythonPath -Arguments @(
        "-m", "c2hlsc_agent.cli", "convert",
        "--config", $configPath,
        "--out", $outPath,
        "--no-llm",
        "--shift-left",
        "--run-vitis",
        "--cosim-backend", "vitis",
        "--vitis-bin", $launcher
    ) -Label "C-to-HLSC conversion and native Vitis verification"

    $conversionReport = Get-Content -LiteralPath (Join-Path $outPath "conversion_report.json") -Raw |
        ConvertFrom-Json
    $provenance = [ordered]@{
        schema = "c2hlsc-amd-hls-toolchain-v1"
        os = [Environment]::OSVersion.VersionString
        architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        python = (& $pythonPath --version 2>&1 | Out-String).Trim()
        make = $makePath
        cxx = $cxxPath
        launcher = $launcher
        launcher_version = $launcherVersion
        top = $conversionReport.top
        part = $conversionReport.part
        clock_ns = $conversionReport.clock_ns
        seed = $conversionReport.seed
        num_tests = $conversionReport.num_tests
        github_sha = $env:GITHUB_SHA
    }
    $provenance | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath (Join-Path $outPath "toolchain_provenance.json") -Encoding UTF8

    # This validator is the sign-off boundary: it rejects missing/stale synthesis
    # reports or RTL, incomplete QoR metrics, and CoSim without a positive PASS marker.
    # It runs after provenance is written so the knowledge graph can link that artifact.
    Invoke-NativeChecked -Executable $pythonPath -Arguments @(
        "-m", "c2hlsc_agent.vitis_evidence", $outPath
    ) -Label "Native Vitis evidence validation"
} finally {
    Set-Location -LiteralPath $previousLocation.Path
}
