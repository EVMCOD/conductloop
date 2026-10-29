"""Windows packaging: the override config and scripts/build-windows.ps1.

What can be checked without a Windows machine: the Windows override is valid and
only touches ``bundle`` (so macOS and Linux builds, which never read it, do not
change), the merged Windows config builds an NSIS installer and needs no signing
key, there is no updater on Windows, and the script names the sidecar the way
Tauri expects. The PowerShell script is parsed (and its refusals run) when
PowerShell is installed, as it is on Windows and on the Windows CI runner. The
real PyInstaller/Tauri build and running the installer are NOT tested here: they
run in the manual "Windows bundles" workflow and need a Windows machine.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TAURI = ROOT / "desktop" / "src-tauri"
BASE = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
WINDOWS = json.loads((TAURI / "tauri.windows.conf.json").read_text(encoding="utf-8"))
SCRIPT = ROOT / "scripts" / "build-windows.ps1"
TRIPLE = "x86_64-pc-windows-msvc"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")


def merge(base, patch):
    """JSON Merge Patch (RFC 7396): what Tauri does with a platform config."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge(out.get(key), value)
    return out


MERGED = merge(BASE, {k: v for k, v in WINDOWS.items() if k != "$schema"})


def script_code() -> str:
    """The script without its help block and its comments."""
    text = SCRIPT.read_text(encoding="utf-8")
    text = re.sub(r"(?s)<#.*?#>", "", text)
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


# -- the override ---------------------------------------------------------------

def test_the_override_only_touches_the_bundle_section():
    # Anything else (app, plugins, identifier, version) must stay the shared one.
    assert set(WINDOWS) - {"$schema"} == {"bundle"}


def test_the_override_is_called_what_tauri_looks_for():
    assert (TAURI / "tauri.windows.conf.json").is_file()
    assert not (TAURI / "tauri.macos.conf.json").exists()


def test_macos_and_linux_config_are_unchanged_by_the_override():
    bundle = BASE["bundle"]
    assert bundle["targets"] == ["dmg", "app"]
    assert bundle["createUpdaterArtifacts"] is True
    assert bundle["externalBin"] == ["binaries/conductloop-server"]
    assert bundle["macOS"]["minimumSystemVersion"] == "13.0"
    assert "windows" not in bundle, "the Windows settings live in tauri.windows.conf.json"
    linux = json.loads((TAURI / "tauri.linux.conf.json").read_text(encoding="utf-8"))
    assert linux["bundle"]["targets"] == ["appimage", "deb"]
    assert "windows" not in linux["bundle"]


def test_windows_builds_an_nsis_installer():
    assert MERGED["bundle"]["targets"] == ["nsis"]
    # Per user: no administrator prompt, nothing written to Program Files.
    assert MERGED["bundle"]["windows"]["nsis"]["installMode"] == "currentUser"


def test_windows_installs_webview2_when_it_is_missing():
    # WebView2 ships with Windows 11 and current Windows 10; the installer
    # downloads the runtime on a machine that lacks it.
    assert MERGED["bundle"]["windows"]["webviewInstallMode"]["type"] == "downloadBootstrapper"


def test_windows_has_no_updater_artifacts_and_so_needs_no_signing_key():
    assert MERGED["bundle"]["createUpdaterArtifacts"] is False


def test_windows_has_no_code_signing_configured():
    # No certificate exists: the installer is unsigned and SmartScreen warns.
    windows = json.dumps(MERGED["bundle"]["windows"]).lower()
    for key in ("certificatethumbprint", "signcommand", "timestampurl", "tsp", "digestalgorithm"):
        assert key not in windows


def test_the_updater_plugin_config_is_kept():
    # The plugin is registered on every platform and fails to start without its
    # config (pubkey is required), so the override must not drop it.
    assert MERGED["plugins"] == BASE["plugins"]


def test_the_merged_config_keeps_the_sidecar_and_has_the_ico_icon():
    assert MERGED["bundle"]["externalBin"] == ["binaries/conductloop-server"]
    assert "icons/icon.ico" in MERGED["bundle"]["icon"]
    assert (TAURI / "icons" / "icon.ico").is_file()


def test_the_description_does_not_say_macos():
    assert "macOS" not in MERGED["bundle"]["shortDescription"]


# -- the updater is off on Windows -----------------------------------------------

def test_the_startup_update_check_runs_on_macos_only():
    lib = (TAURI / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'if !cfg!(debug_assertions) && cfg!(target_os = "macos") {' in lib
    assert "updates::check_after_startup(app.handle());" in lib
    # The menu item "Check for Updates…" is macOS-only too.
    assert 'if i == 0 && cfg!(target_os = "macos") {' in lib


def test_the_updater_endpoint_is_still_the_macos_one():
    # Windows must never read the macOS manifest as its own.
    assert BASE["plugins"]["updater"]["endpoints"] == [
        "https://github.com/EVMCOD/conductloop-releases/releases/latest/download/update-macos-aarch64.json"
    ]


# -- the sidecar spec --------------------------------------------------------------

def test_the_frozen_server_runs_in_utf8_mode_on_windows_only():
    spec = (ROOT / "desktop" / "conductloop_server.spec").read_text(encoding="utf-8")
    assert 'options = [("X utf8", None, "OPTION")] if sys.platform == "win32" else []' in spec
    exe = spec[spec.index("EXE("):]
    assert re.search(r"a\.scripts,\s*options,\s*a\.binaries", exe)


# -- the script -----------------------------------------------------------------------

def test_the_script_puts_the_sidecar_where_tauri_expects_it():
    code = script_code()
    assert f'$Triple = "{TRIPLE}"' in code
    # externalBin "binaries/conductloop-server" + "-<triple>.exe" is what Tauri looks for.
    assert MERGED["bundle"]["externalBin"] == ["binaries/conductloop-server"]
    assert 'binaries\\conductloop-server-$Triple.exe' in code
    assert 'dist\\conductloop-server.exe' in code
    assert "--bundles $BundleArg" in code
    assert '[string[]]$Bundles = @("nsis")' in code


def test_the_script_signs_and_publishes_nothing():
    code = script_code()
    assert "Remove-Item Env:TAURI_SIGNING_PRIVATE_KEY " in code
    assert "Remove-Item Env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD" in code
    for word in ("gh release", "gh api", "git push", "curl", "Invoke-WebRequest", "Invoke-RestMethod",
                 "signtool", "Set-AuthenticodeSignature", "codesign", "notarytool", "release-desktop"):
        assert word not in code


def test_the_script_is_plain_ascii():
    # Windows PowerShell 5.1 reads a script without a byte order mark as ANSI.
    SCRIPT.read_bytes().decode("ascii")


def test_the_macos_release_script_is_not_wired_to_the_windows_one():
    assert "build-windows" not in (ROOT / "scripts" / "release-desktop.sh").read_text(encoding="utf-8")


def test_the_linux_script_is_not_wired_to_the_windows_one():
    assert "build-windows" not in (ROOT / "scripts" / "build-linux.sh").read_text(encoding="utf-8")


def _run(*args):
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=120)


@needs_powershell
def test_the_script_parses_without_errors():
    command = ("$e = $null; $t = $null; "
               f"[void][System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}', [ref]$t, [ref]$e); "
               "if ($e.Count) { $e | ForEach-Object { $_.ToString() }; exit 1 }")
    r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_powershell
def test_help_works_anywhere_and_prints_the_header():
    r = _run("-Help")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("Build the Windows installer")
    assert "-SidecarOnly" in r.stdout and "-SkipSidecar" in r.stdout and "-Bundles" in r.stdout
    assert "SmartScreen" in r.stdout


@needs_powershell
@pytest.mark.parametrize("args,message", [
    (["-Bundles", "dmg"], "-Bundles must be nsis, msi or nsis,msi"),
    (["-Bundles", "nsis;calc"], "-Bundles must be nsis, msi or nsis,msi"),
    (["-SidecarOnly", "-SkipSidecar"], "exclude each other"),
])
def test_bad_options_are_refused_before_anything_is_built(args, message):
    r = _run(*args)
    assert r.returncode == 1
    assert message in r.stderr
    assert not (TAURI / "binaries" / f"conductloop-server-{TRIPLE}.exe").exists()


@needs_powershell
@pytest.mark.skipif(sys.platform == "win32", reason="on Windows the script would start building")
@pytest.mark.parametrize("args", [[], ["-Bundles", "nsis,msi"], ["-Bundles", "msi"]])
def test_it_refuses_to_run_off_windows(args):
    # (The options are valid, so the refusal is the OS check, not the option check.)
    r = _run(*args)
    assert r.returncode == 1
    assert "must run on 64-bit Windows" in r.stderr


def _in_powershell(bundles: str):
    command = f"& '{SCRIPT}' -Bundles {bundles}; exit $LASTEXITCODE"
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=120)


@needs_powershell
def test_bundles_given_as_a_powershell_list_are_understood():
    # `-Bundles nsis,msi` typed in PowerShell is an array, not a string: this is
    # how the workflow calls the script (it failed to bind as a string). An
    # invalid list is used so that nothing is built on Windows.
    r = _in_powershell("nsis,dmg")
    assert "Cannot process argument" not in r.stderr, r.stderr
    assert r.returncode == 1 and "got 'nsis,dmg'" in r.stderr


@needs_powershell
@pytest.mark.skipif(sys.platform == "win32", reason="on Windows the script would start building")
def test_a_valid_powershell_list_gets_past_the_option_check():
    r = _in_powershell("nsis,msi")
    assert r.returncode == 1 and "must run on 64-bit Windows" in r.stderr


# -- the docs --------------------------------------------------------------------------

def test_the_docs_tell_about_smartscreen_and_webview2():
    doc = (ROOT / "docs" / "PORTABILITY.md").read_text(encoding="utf-8")
    assert "### Windows status" in doc
    section = doc[doc.index("### Windows status"):]
    for text in ("SmartScreen", "More info", "Run anyway", "WebView2", "scripts/build-windows.ps1",
                 "tauri.windows.conf.json"):
        assert text in section, text
