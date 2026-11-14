<#
Build the Windows installer of the desktop app: an NSIS setup.exe (and, only if
asked for, an .msi). Windows x86_64 only.

  scripts\build-windows.ps1 [-SidecarOnly | -SkipSidecar] [-Bundles nsis[,msi]] [-Help]

What it does, in order:
  1. Freezes the local server (desktop\conductloop_server.spec) with
     PyInstaller, in a venv of its own under build\windows-venv, and copies it
     to desktop\src-tauri\binaries\conductloop-server-x86_64-pc-windows-msvc.exe,
     the name Tauri expects for an externalBin on this target (-<triple>.exe).
  2. Runs `tauri build --bundles <Bundles>` from desktop\. The Windows-only
     settings live in desktop\src-tauri\tauri.windows.conf.json, which Tauri
     merges over tauri.conf.json on Windows only; macOS builds do not read it.

Output: desktop\src-tauri\target\release\bundle\nsis\*.exe (and \msi\*.msi).

There is no code signing and no updater on Windows: the override turns off the
updater artifacts, so no signing key is needed, and any TAURI_SIGNING_*
variable in the environment is dropped. The installer is NOT signed with a
code-signing certificate, so Windows SmartScreen warns when it is run ("More
info", then "Run anyway"). This script never publishes anything. It does not
touch scripts\release-desktop.sh (the macOS release).

  -SidecarOnly  stop after step 1 (needs only Python).
  -SkipSidecar  reuse the sidecar already in desktop\src-tauri\binaries\.
  -Bundles      installer formats to build, "nsis" (default) and/or "msi".

Needs: Windows 10 or 11 on x86_64, Python >= 3.13 64-bit (set PYTHON to pick
one; default: python on PATH), Node/npm, a Rust toolchain for
x86_64-pc-windows-msvc with the Visual Studio Build Tools (C++ workload), and
internet access (Tauri downloads the NSIS tools; the installer downloads the
WebView2 runtime on machines that lack it).
#>
[CmdletBinding()]
param(
    [switch]$SidecarOnly,
    [switch]$SkipSidecar,
    [string[]]$Bundles = @("nsis"),
    [Alias("h")]
    [switch]$Help
)

$ErrorActionPreference = "Stop"

$Triple = "x86_64-pc-windows-msvc"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TauriDir = Join-Path $Root "desktop\src-tauri"
$SidecarDest = Join-Path $TauriDir "binaries\conductloop-server-$Triple.exe"
$Venv = if ($env:CONDUCTLOOP_WINDOWS_VENV) { $env:CONDUCTLOOP_WINDOWS_VENV } else { Join-Path $Root "build\windows-venv" }
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

function Fail([string]$message) {
    [Console]::Error.WriteLine("x $message")
    exit 1
}

# Run a native command and stop when it fails ($ErrorActionPreference does not
# cover exit codes of programs).
function Invoke-Native([string]$what, [scriptblock]$command) {
    & $command
    if ($LASTEXITCODE -ne 0) { Fail "$what failed (exit code $LASTEXITCODE)" }
}

if ($Help) {
    $text = Get-Content -LiteralPath $PSCommandPath -Raw
    $inner = $text -replace '(?s)^<#\r?\n', '' -replace '(?s)#>.*$', ''
    Write-Output $inner.TrimEnd()
    exit 0
}

if ($SidecarOnly -and $SkipSidecar) { Fail "-SidecarOnly and -SkipSidecar exclude each other (see -Help)" }
# PowerShell turns `-Bundles nsis,msi` into an array, and `-File x.ps1 -Bundles
# nsis,msi` from another program into one string: accept both.
$BundleList = @($Bundles | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $BundleList -or @($BundleList | Where-Object { $_ -notin @("nsis", "msi") })) {
    Fail "-Bundles must be nsis, msi or nsis,msi (got '$($Bundles -join ',')')"
}
$BundleArg = ($BundleList | Select-Object -Unique) -join ","

if ([System.Environment]::OSVersion.Platform -ne "Win32NT" -or -not [System.Environment]::Is64BitOperatingSystem) {
    Fail "build-windows.ps1 builds $Triple packages and must run on 64-bit Windows"
}

Set-Location $Root

# -- 1. Sidecar ---------------------------------------------------------------
if (-not $SkipSidecar) {
    Write-Output "> Building the sidecar (PyInstaller) for $Triple"
    & $Python -c "import sys, struct; sys.exit(0 if sys.version_info >= (3, 13) and struct.calcsize('P') == 8 else 1)"
    if ($LASTEXITCODE -ne 0) { Fail "$Python is not a 64-bit Python >= 3.13 (pyproject.toml: requires-python >= 3.13); set PYTHON=" }

    Invoke-Native "creating the venv" { & $Python -m venv $Venv }
    $VenvPython = Join-Path $Venv "Scripts\python.exe"
    $PyInstaller = Join-Path $Venv "Scripts\pyinstaller.exe"
    Invoke-Native "upgrading pip" { & $VenvPython -m pip install --quiet --upgrade pip }

    # The same dependencies the macOS release freezes, minus sentence-transformers
    # (it pulls in torch, which the spec excludes: embeddings run on ONNX).
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "build") | Out-Null
    $SidecarRequirements = Join-Path $Root "build\windows-sidecar-requirements.txt"
    $lines = Get-Content -LiteralPath (Join-Path $Root "requirements.txt") |
        Where-Object { $_ -notmatch '^\s*(#|-r|sentence-transformers|$)' } |
        ForEach-Object { $_ -replace '\s*#.*$', '' }
    [System.IO.File]::WriteAllLines($SidecarRequirements, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))

    Invoke-Native "installing the sidecar dependencies" {
        & $VenvPython -m pip install --quiet -r requirements-core.txt -r $SidecarRequirements pyinstaller
    }
    Invoke-Native "PyInstaller" {
        & $PyInstaller desktop\conductloop_server.spec --noconfirm --distpath dist --workpath build\pyi
    }
    $Frozen = Join-Path $Root "dist\conductloop-server.exe"
    if (-not (Test-Path -LiteralPath $Frozen)) { Fail "PyInstaller did not produce $Frozen" }
    New-Item -ItemType Directory -Force -Path (Split-Path $SidecarDest) | Out-Null
    Copy-Item -LiteralPath $Frozen -Destination $SidecarDest -Force
    Get-Item -LiteralPath $SidecarDest | Format-List Name, Length | Out-String | Write-Output
}
if ($SidecarOnly) { exit 0 }

if (-not (Test-Path -LiteralPath $SidecarDest)) {
    Fail "$SidecarDest is missing: run without -SkipSidecar first"
}

# -- 2. Installer -------------------------------------------------------------
$rustHost = ((& rustc -vV) | Where-Object { $_ -like "host:*" }) -replace '^host:\s*', ''
if ($rustHost -ne $Triple) {
    Fail "the Rust host is '$rustHost', not $Triple (install the x86_64 MSVC toolchain: rustup default stable-x86_64-pc-windows-msvc)"
}

Write-Output "> Building the installer ($BundleArg) (tauri build)"
# No signing on Windows: make sure a key left in the environment is not used.
Remove-Item Env:TAURI_SIGNING_PRIVATE_KEY -ErrorAction SilentlyContinue
Remove-Item Env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD -ErrorAction SilentlyContinue

# Panic locations embed absolute source paths (...\.cargo\registry\...): do not
# publish the builder's home directory in the binary (same as the macOS release).
# RUSTFLAGS is split on spaces, so a home directory with a space cannot be mapped.
$savedRustflags = $env:RUSTFLAGS
if ($env:USERPROFILE -and $env:USERPROFILE -notmatch '\s') {
    $env:RUSTFLAGS = "$env:RUSTFLAGS --remap-path-prefix=$($env:USERPROFILE)=/build".Trim()
} else {
    [Console]::Error.WriteLine("! the home directory has a space: its path stays in the binary's panic messages")
}
Push-Location (Join-Path $Root "desktop")
try {
    Invoke-Native "npm ci" { & npm ci --no-audit --no-fund }
    Invoke-Native "tauri build" { & npm run --silent tauri -- build --bundles $BundleArg }
} finally {
    Pop-Location
    $env:RUSTFLAGS = $savedRustflags
}

$Bin = Join-Path $TauriDir "target\release\conductloop-desktop.exe"
if ((Test-Path -LiteralPath $Bin) -and $env:USERPROFILE) {
    $bytes = [System.IO.File]::ReadAllBytes($Bin)
    $latin1 = [System.Text.Encoding]::GetEncoding(28591).GetString($bytes)
    if ($latin1.IndexOf($env:USERPROFILE, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
        [Console]::Error.WriteLine("! the desktop binary still contains $($env:USERPROFILE)")
    }
}

Write-Output ""
Write-Output "Built:"
$found = @()
foreach ($dir in "nsis", "msi") {
    $found += Get-ChildItem -Path (Join-Path $TauriDir "target\release\bundle\$dir\*") -Include *.exe, *.msi -File -ErrorAction SilentlyContinue
}
if (-not $found) { Fail "no installer was produced under $TauriDir\target\release\bundle" }
foreach ($f in $found) {
    "{0}  {1:N0} bytes" -f $f.FullName, $f.Length
    "  sha256 {0}" -f (Get-FileHash -Algorithm SHA256 -LiteralPath $f.FullName).Hash.ToLower()
}
