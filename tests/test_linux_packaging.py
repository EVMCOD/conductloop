"""Linux packaging: the override config and scripts/build-linux.sh.

What can be checked without building: the Linux override is valid and only
touches ``bundle`` (so macOS builds, which never read it, do not change), the
merged Linux config has what an AppImage/.deb build needs and nothing that
needs a signing key, and the script names the sidecar the way Tauri expects.
The actual PyInstaller/Tauri build is not run here (no WebKitGTK needed for
these tests): it runs in CI and on a Linux machine.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TAURI = ROOT / "desktop" / "src-tauri"
BASE = json.loads((TAURI / "tauri.conf.json").read_text())
LINUX = json.loads((TAURI / "tauri.linux.conf.json").read_text())
SCRIPT = ROOT / "scripts" / "build-linux.sh"
TRIPLE = "x86_64-unknown-linux-gnu"


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


MERGED = merge(BASE, {k: v for k, v in LINUX.items() if k != "$schema"})


def test_the_override_only_touches_the_bundle_section():
    # Anything else (app, plugins, identifier, version) must stay the shared one.
    assert set(LINUX) - {"$schema"} == {"bundle"}


def test_the_override_is_called_what_tauri_looks_for():
    # Tauri merges tauri.<platform>.conf.json for the platform it builds on.
    assert (TAURI / "tauri.linux.conf.json").is_file()
    assert not (TAURI / "tauri.macos.conf.json").exists()


def test_macos_config_is_unchanged_by_the_override():
    bundle = BASE["bundle"]
    assert bundle["targets"] == ["dmg", "app"]
    assert bundle["createUpdaterArtifacts"] is True
    assert bundle["externalBin"] == ["binaries/conductloop-server"]
    assert bundle["macOS"]["dmg"]["appPosition"] == {"x": 175, "y": 190}
    assert bundle["macOS"]["minimumSystemVersion"] == "13.0"
    assert BASE["plugins"]["updater"]["pubkey"]


def test_linux_builds_an_appimage_and_a_deb_and_nothing_else():
    assert MERGED["bundle"]["targets"] == ["appimage", "deb"]


def test_linux_has_no_updater_artifacts_and_so_needs_no_signing_key():
    assert MERGED["bundle"]["createUpdaterArtifacts"] is False
    assert "linux" not in MERGED["bundle"] or "sign" not in json.dumps(MERGED["bundle"]["linux"]).lower()


def test_the_updater_plugin_config_is_kept():
    # The plugin is registered on every platform and fails to start without its
    # config (pubkey is required), so the override must not drop it.
    assert MERGED["plugins"] == BASE["plugins"]


def test_the_merged_config_keeps_the_sidecar_and_the_png_icons():
    assert MERGED["bundle"]["externalBin"] == ["binaries/conductloop-server"]
    pngs = [i for i in MERGED["bundle"]["icon"] if i.endswith(".png")]
    assert pngs and all((TAURI / i).is_file() for i in pngs)


def test_the_script_puts_the_sidecar_where_tauri_expects_it():
    text = SCRIPT.read_text()
    assert f'TRIPLE="{TRIPLE}"' in text
    assert '"$TAURI_DIR/binaries/conductloop-server-$TRIPLE"' in text
    # externalBin "binaries/conductloop-server" + "-<triple>" is what Tauri looks for.
    assert MERGED["bundle"]["externalBin"] == ["binaries/conductloop-server"]
    assert "--bundles appimage,deb" in text


def test_the_script_signs_and_publishes_nothing():
    code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    assert "unset TAURI_SIGNING_PRIVATE_KEY TAURI_SIGNING_PRIVATE_KEY_PASSWORD" in code
    for word in ("gh release", "gh api", "git push", "curl", "codesign", "notarytool", "release-desktop"):
        assert word not in code


def test_the_macos_release_script_is_not_wired_to_the_linux_one():
    assert "build-linux" not in (ROOT / "scripts" / "release-desktop.sh").read_text()


@pytest.mark.posix_perms
def test_the_script_is_executable_and_valid_bash():
    assert os.access(SCRIPT, os.X_OK)
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(SCRIPT)], check=True)


def test_help_works_anywhere_and_prints_the_header():
    out = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True, check=True).stdout
    assert out.startswith("Build the Linux packages")
    assert "--sidecar-only" in out and "--skip-sidecar" in out


def test_unknown_option_is_refused():
    r = subprocess.run([str(SCRIPT), "--nope"], capture_output=True, text=True)
    assert r.returncode == 2


@pytest.mark.parametrize("system,machine", [("Darwin", "arm64"), ("Linux", "aarch64")])
def test_it_refuses_to_run_off_linux_x86_64(tmp_path, system, machine):
    fake = tmp_path / "uname"
    fake.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "$1" in -s) echo {system};; -m) echo {machine};; *) echo {system};; esac
        """))
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert r.returncode == 1
    assert "must run on Linux x86_64" in r.stderr
    # It stopped before building anything.
    assert not (tmp_path / "build").exists() and not (tmp_path / "dist").exists()


def test_the_startup_update_check_is_off_on_linux_and_unchanged_elsewhere():
    lib = (TAURI / "src" / "lib.rs").read_text()
    assert 'if !cfg!(debug_assertions) && !cfg!(target_os = "linux") {' in lib
    assert "updates::check_after_startup(app.handle());" in lib
    # The plugin stays registered on every platform (the commands need its state).
    assert ".plugin(tauri_plugin_updater::Builder::new().build())" in lib
