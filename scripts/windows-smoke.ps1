<#
Smoke-test the Windows installer of the desktop app: install it, start it the
way a new user would, close it, uninstall it.

  scripts\windows-smoke.ps1 -Setup path\to\ConductLoop_<v>_x64-setup.exe [-Out dir] [-Wait 120]

Checks, in order (each prints a check mark or a cross; the exit status is 1 if
any failed):
  1. the NSIS setup installs silently, per user, with conductloop-desktop.exe
     and conductloop-server.exe;
  2. the local server answers /api/health on 127.0.0.1:7777 within -Wait s;
  3. /api/platform says windows, with the in-app browser and code execution off;
  4. it is a first run: /api/onboarding/state is fresh and /welcome is served;
  5. the app has a "ConductLoop" window (and a screenshot is saved);
  6. 20 s later the app and its server are still running (parent_watch must
     not end anything on Windows);
  7. closing the window stops the server: the port is free and no
     conductloop-server.exe is left within 15 s;
  8. uninstalling (silently) keeps the user's data: the app keeps its data in
     %LOCALAPPDATA%\ConductLoop\data, inside the install folder.
Writes windows-smoke.png and windows-smoke.log in -Out (default: the current
folder). Talks only to 127.0.0.1:7777 with GETs. Publishes nothing.
Windows x86_64 only; meant for a disposable machine or CI runner (it installs
and uninstalls ConductLoop for the current user).
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Setup,
  [string]$Out = (Get-Location).Path,
  [int]$Wait = 120
)
$ErrorActionPreference = "Stop"
$Base = "http://127.0.0.1:7777"
$InstallDir = Join-Path $env:LOCALAPPDATA "ConductLoop"
$DataDir = Join-Path $InstallDir "data"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$Log = Join-Path $Out "windows-smoke.log"
$Shot = Join-Path $Out "windows-smoke.png"
$script:failures = 0

function Report([string]$line) {
  Write-Host $line
  Add-Content -Path $Log -Value $line
  if ($env:GITHUB_STEP_SUMMARY) { Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value "- $line" }
}
function Ok([string]$what) { Report "✓ $what" }
function Bad([string]$what) {
  Report "✗ $what"
  # An annotation shows on the run page even when the step may not fail the job.
  if ($env:GITHUB_ACTIONS) { Write-Host "::error title=Windows smoke test::$what" }
  $script:failures++
}
function Get-Json([string]$path) {
  try { return Invoke-RestMethod -Uri "$Base$path" -TimeoutSec 5 } catch { return $null }
}
function Test-Up {
  try { return (Invoke-WebRequest -UseBasicParsing -Uri "$Base/api/health" -TimeoutSec 2).StatusCode -eq 200 } catch { return $false }
}
function Get-Servers { @(Get-Process -Name "conductloop-server" -ErrorAction SilentlyContinue) }

if (-not (Test-Path -LiteralPath $Setup)) { throw "no installer at '$Setup'" }
if (Test-Up) { throw "something already answers on ${Base}: stop it first" }
if ($env:GITHUB_STEP_SUMMARY) {
  Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value "### Windows smoke test ($((Get-CimInstance Win32_OperatingSystem).Caption))"
}

# 1. Install.
Start-Process -FilePath (Resolve-Path -LiteralPath $Setup).Path -ArgumentList "/S" -Wait
$desktop = Join-Path $InstallDir "conductloop-desktop.exe"
$server = Join-Path $InstallDir "conductloop-server.exe"
if ((Test-Path $desktop) -and (Test-Path $server)) {
  Ok "installed per user in $InstallDir"
} else {
  Bad "the installer did not put conductloop-desktop.exe and conductloop-server.exe in $InstallDir"
  Get-ChildItem $InstallDir -Recurse -ErrorAction SilentlyContinue | Select-Object -First 20 | ForEach-Object { Write-Host "    $($_.FullName)" }
  exit 1
}

# 2. The server comes up.
$started = Get-Date
$app = Start-Process -FilePath $desktop -PassThru
$up = $false
for ($i = 0; $i -lt $Wait -and -not $up; $i++) {
  Start-Sleep -Seconds 1
  $up = Test-Up
  if ($app.HasExited) { break }
}
if ($up) {
  $health = Get-Json "/api/health"
  Ok ("server answered in {0:N0} s as {1}" -f ((Get-Date) - $started).TotalSeconds, $health.version)
} else {
  Bad ("the server did not answer within {0} s (app {1})" -f $Wait, $(if ($app.HasExited) { "exited with $($app.ExitCode)" } else { "running" }))
  exit 1
}

# 3. It knows it is on Windows.
$p = Get-Json "/api/platform"
foreach ($k in "browser", "code_execution", "keychain") {
  $v = $p.$k
  $line = ("    {0}: available={1} {2} {3}" -f $k, $v.available, $v.reason, $v.message).TrimEnd()
  Report $line
}
if ($p.platform -eq "windows" -and $p.browser.available -eq $false -and $p.code_execution.available -eq $false) {
  Ok "/api/platform: windows, in-app browser and code execution off"
} else {
  Bad "/api/platform does not say windows with the browser and code execution off (platform=$($p.platform))"
}

# 4. A first run.
$state = Get-Json "/api/onboarding/state"
$welcome = try { (Invoke-WebRequest -UseBasicParsing -Uri "$Base/welcome" -TimeoutSec 5).Content } catch { "" }
if ($state.fresh -eq $true -and $welcome -match "ConductLoop") {
  Ok "first run: onboarding is fresh and /welcome is served"
} else {
  Bad "first run: onboarding not fresh or /welcome missing"
}

# 5. A window, and what it shows.
Start-Sleep -Seconds 8
$proc = Get-Process -Id $app.Id -ErrorAction SilentlyContinue
if ($proc -and $proc.MainWindowTitle -eq "ConductLoop") {
  Ok "a ConductLoop window is open"
} else {
  Bad "no ConductLoop window (title: '$($proc.MainWindowTitle)')"
}
try {
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing
  $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  $graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
  $bitmap.Save($Shot, [System.Drawing.Imaging.ImageFormat]::Png)
  Write-Host "    screenshot: $Shot"
} catch {
  Write-Host "    (no screenshot: $($_.Exception.Message))"
}

# 6. Still running a little later.
Start-Sleep -Seconds 20
if (-not $app.HasExited -and (Test-Up) -and (Get-Servers).Count -ge 1) {
  Ok "20 s later the app and its server are still running"
} else {
  Bad "the app or its server stopped on its own (app exited: $($app.HasExited))"
}

# 7. Closing the window stops the server.
$closed = $false
if (-not $app.HasExited) { $app.Refresh(); $closed = $app.CloseMainWindow() }
if (-not $closed) { Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue }
$freed = $null
for ($i = 1; $i -le 15 -and $null -eq $freed; $i++) {
  Start-Sleep -Seconds 1
  if (-not (Test-Up) -and (Get-Servers).Count -eq 0) { $freed = $i }
}
$how = $(if ($closed) { "closing the window" } else { "ending the app" })
if ($null -ne $freed) {
  Ok "$how stopped its server ($freed s)"
} else {
  Bad "the server kept running 15 s after $how"
  Get-Servers | ForEach-Object { Write-Host "    left: pid $($_.Id), started $($_.StartTime)" }
}
Get-Process -Name "conductloop-desktop", "conductloop-server" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# 8. Uninstalling keeps the user's data.
$db = Join-Path $DataDir "ops.db"
if (-not (Test-Path $db)) {
  Bad "no database at $db after a run: the data folder is not where expected"
} else {
  $uninstaller = Get-ChildItem $InstallDir -Filter "uninstall*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $uninstaller) {
    Bad "no uninstaller in $InstallDir"
  } else {
    Start-Process -FilePath $uninstaller.FullName -ArgumentList "/S" -Wait
    # The NSIS uninstaller copies itself elsewhere and returns at once: wait for it.
    for ($i = 0; $i -lt 30 -and (Test-Path $desktop); $i++) { Start-Sleep -Seconds 1 }
    if (Test-Path $desktop) {
      Bad "the uninstaller did not remove the app"
    } elseif (Test-Path $db) {
      Ok "uninstalling removed the app and kept the user's data ($db)"
    } else {
      Bad "uninstalling DELETED the user's data ($db is gone)"
    }
  }
}

if ($script:failures -gt 0) { exit 1 }
exit 0
