#Requires -Version 5.1
<#
.SYNOPSIS
    Build the RunnerDeck onedir bundle and zip it (docs/m2-design.md §6).

.DESCRIPTION
    uv sync --locked  ->  pyinstaller RunnerDeck.spec  ->  dist/RunnerDeck-<version>-win-x64.zip
    ->  (Inno Setup found) dist/RunnerDeck-<version>-setup.exe (packaging/RunnerDeck.iss).
    The zip root holds RunnerDeck.exe, runnerdeck-helper.exe, runnerdeck-cli.exe and _internal/.
    Works under Windows PowerShell 5.1 and PowerShell 7; any failure exits 1.
    The last lines of output are the zip path and, when built, the setup path.

.PARAMETER NoInstaller
    Skip the installer even when ISCC.exe is available.
#>

[CmdletBinding()]
param(
    [switch] $NoInstaller
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$distDir = Join-Path $repoRoot 'dist'
$bundleDir = Join-Path $distDir 'RunnerDeck'
$expectedExes = @('RunnerDeck.exe', 'runnerdeck-helper.exe', 'runnerdeck-cli.exe')

function Invoke-Native {
    # Run a native command; fail on its exit code, not on its stderr chatter (uv prints
    # progress there, which Windows PowerShell 5.1 would otherwise turn into errors).
    param(
        [Parameter(Mandatory = $true)] [string] $Label,
        [Parameter(Mandatory = $true)] [scriptblock] $Command
    )
    Write-Host "==> $Label"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Command 2>&1 | ForEach-Object { "$_" }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    $output | ForEach-Object { Write-Host $_ }
    if ($exitCode -ne 0) {
        throw "$Label failed with exit code $exitCode"
    }
    return $output
}

Push-Location $repoRoot
try {
    Invoke-Native 'uv sync --locked' { uv sync --locked } | Out-Null

    Invoke-Native 'pyinstaller RunnerDeck.spec' {
        uv run pyinstaller RunnerDeck.spec --noconfirm --clean
    } | Out-Null

    $versionLines = Invoke-Native 'read version' {
        uv run python -c "import runnerdeck; print(runnerdeck.__version__)"
    }
    $version = ($versionLines | Where-Object { $_ -match '^\S+$' } | Select-Object -Last 1)
    if (-not $version) {
        throw 'could not read runnerdeck.__version__'
    }
    $version = $version.Trim()
    Write-Host "version: $version"

    foreach ($exe in $expectedExes) {
        $path = Join-Path $bundleDir $exe
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "missing build output: $path"
        }
    }

    $zipPath = Join-Path $distDir "RunnerDeck-$version-win-x64.zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Write-Host "==> zip $zipPath"
    # .NET ZipFile instead of Compress-Archive: identical on 5.1 and 7, and 5.1's
    # Compress-Archive writes backslash entry names that non-Windows unzip tools mangle.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $bundleDir,
        $zipPath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )
    if (-not (Test-Path -LiteralPath $zipPath)) {
        throw "zip was not created: $zipPath"
    }
    $setupPath = $null
    if (-not $NoInstaller) {
        $iscc = (Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue).Source
        if (-not $iscc) {
            foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA 'Programs'))) {
                if (-not $base) { continue }
                $candidate = Join-Path $base 'Inno Setup 6\ISCC.exe'
                if (Test-Path -LiteralPath $candidate) { $iscc = $candidate; break }
            }
        }
        if (-not $iscc) {
            Write-Host 'Inno Setup (ISCC.exe) not found: skipping the installer'
        } else {
            $script = Join-Path $repoRoot 'packaging\RunnerDeck.iss'
            Invoke-Native "iscc $script" {
                & $iscc /Q "/DAppVersion=$version" "/DSourceDir=$bundleDir" "/DOutputDir=$distDir" $script
            } | Out-Null
            $setupPath = Join-Path $distDir "RunnerDeck-$version-setup.exe"
            if (-not (Test-Path -LiteralPath $setupPath)) {
                throw "installer was not created: $setupPath"
            }
        }
    }
    Write-Host 'build finished'
    Write-Output $zipPath
    if ($setupPath) { Write-Output $setupPath }
} catch {
    Write-Host "build failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}
