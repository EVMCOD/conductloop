"""Static checks on the Tauri ACL (desktop/src-tauri) — browser design PR 1.

The in-app browser (design doc 2026-10-21, §1.2) will host remote pages in
child webviews labelled ``tab-*``. Those must never match a capability, so
they get no IPC at all (https://v2.tauri.app/reference/acl/capability/: "If a
webview or its window is not matching any capability then it has no access to
the IPC layer at all"). These tests keep that true as capabilities evolve.
No Rust toolchain needed.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import pytest

TAURI = Path(__file__).resolve().parent.parent / "desktop" / "src-tauri"
CAPS = sorted((TAURI / "capabilities").glob("*.json"))

# Labels the browser tabs will use, plus a few adversarial variants.
TAB_LABELS = ["tab-0", "tab-3f2a", "tab-agent-1", "tab-main", "tab-"]
# Permissions no capability may grant to a frontend: they would let a
# compromised page open new (privileged-label) windows/webviews.
FORBIDDEN = {
    "core:window:allow-create",
    "core:webview:allow-create-webview-window",
    "core:webview:allow-create-webview",
    "core:webview:allow-reparent",
    "core:window:default",
    "core:webview:default",
}


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


def _perm_ids(cap: dict) -> set[str]:
    return {p if isinstance(p, str) else p["identifier"] for p in cap.get("permissions", [])}


def test_capabilities_exist():
    assert CAPS, "no capability files found"


@pytest.mark.parametrize("path", CAPS, ids=lambda p: p.name)
def test_bound_by_webview_label_not_window(path):
    cap = _load(path)
    # A window-label capability applies to every webview in that window,
    # including child browser tabs. Use `webviews` instead.
    assert not cap.get("windows"), f"{path.name}: use `webviews`, not `windows`"
    assert cap.get("webviews"), f"{path.name}: must name its webviews explicitly"


@pytest.mark.parametrize("path", CAPS, ids=lambda p: p.name)
def test_no_capability_matches_browser_tabs(path):
    cap = _load(path)
    for pattern in cap.get("webviews", []) + cap.get("windows", []):
        for label in TAB_LABELS:
            assert not fnmatch.fnmatchcase(label, pattern), (
                f"{path.name}: pattern {pattern!r} matches browser tab label {label!r}")


@pytest.mark.parametrize("path", CAPS, ids=lambda p: p.name)
def test_forbidden_permissions_not_granted(path):
    bad = _perm_ids(_load(path)) & FORBIDDEN
    assert not bad, f"{path.name} grants {sorted(bad)}"


@pytest.mark.parametrize("path", CAPS, ids=lambda p: p.name)
def test_remote_only_for_the_ui_webview_on_loopback(path):
    cap = _load(path)
    remote = cap.get("remote")
    if not remote:
        return
    assert cap.get("webviews") == ["main"], f"{path.name}: remote access only for webview `main`"
    for url in remote.get("urls", []):
        assert re.fullmatch(r"http://localhost:7777(/.*)?", url), f"{path.name}: remote url {url!r}"


def _handler_commands() -> list[str]:
    src = (TAURI / "src" / "lib.rs").read_text()
    m = re.search(r"generate_handler!\[(.*?)\]", src, re.S)
    assert m, "generate_handler! not found in lib.rs"
    # strip module paths (browser::browser_open -> browser_open)
    return sorted(c.strip().split("::")[-1] for c in m.group(1).split(",") if c.strip())


def _manifest_commands() -> list[str]:
    src = (TAURI / "build.rs").read_text()
    m = re.search(r"AppManifest::new\(\)\s*\.commands\(&\[(.*?)\]\)", src, re.S)
    assert m, "build.rs must restrict app commands with AppManifest::commands"
    return sorted(re.findall(r'"([a-z0-9_]+)"', m.group(1)))


def test_app_manifest_lists_exactly_the_registered_commands():
    assert _manifest_commands() == _handler_commands()


def test_app_command_permissions_reference_known_commands():
    known = {"allow-" + c.replace("_", "-") for c in _manifest_commands()}
    for path in CAPS:
        for pid in _perm_ids(_load(path)):
            if ":" not in pid and pid.startswith(("allow-", "deny-")):
                assert pid.replace("deny-", "allow-", 1) in known, f"{path.name}: unknown app permission {pid}"


def test_tauri_minor_is_pinned_and_lockfile_committed():
    cargo = (TAURI / "Cargo.toml").read_text()
    assert re.search(r'^tauri = \{ version = "~2\.\d+', cargo, re.M), "pin tauri with ~2.<minor>"
    assert (TAURI / "Cargo.lock").is_file(), "commit desktop/src-tauri/Cargo.lock"


# ── Browser tabs (browser design PR 4) ─────────────────────────────────────

def _cap(identifier: str) -> dict:
    for p in CAPS:
        c = _load(p)
        if c["identifier"] == identifier:
            return c
    raise AssertionError(f"capability {identifier!r} not found")


def test_browser_ui_capability_is_remote_only_and_minimal():
    cap = _cap("browser-ui")
    assert cap["webviews"] == ["main"]
    assert cap.get("local") is False, "browser controls only for the UI served on localhost:7777"
    assert cap["remote"]["urls"] == ["http://localhost:7777/*"]
    perms = _perm_ids(cap)
    assert perms and all(p.startswith("allow-browser-") for p in perms), perms


def test_folder_picker_capability_is_remote_only_and_minimal():
    cap = _cap("folder-picker")
    assert cap["webviews"] == ["main"]
    assert cap.get("local") is False
    assert cap["remote"]["urls"] == ["http://localhost:7777/*"]
    assert _perm_ids(cap) == {"allow-pick-folder"}


def test_the_dialog_plugin_commands_are_granted_to_nobody():
    # pick_folder uses the plugin from Rust; its own commands (open/save/message
    # with arbitrary paths and text) must stay out of every webview's reach.
    for p in CAPS:
        assert not any(x.startswith("dialog:") for x in _perm_ids(_load(p))), p.name


def test_browser_commands_not_granted_to_local_pages():
    for p in CAPS:
        c = _load(p)
        if c.get("local", True):
            assert not any(x.startswith("allow-browser-") for x in _perm_ids(c)), p.name


def test_every_browser_command_has_a_permission():
    cmds = [c for c in _manifest_commands() if c.startswith("browser_")]
    assert len(cmds) >= 10
    granted = _perm_ids(_cap("browser-ui"))
    for c in cmds:
        assert "allow-" + c.replace("_", "-") in granted, c


def test_tabs_are_isolated_in_rust():
    src = (TAURI / "src" / "browser.rs").read_text()
    for needle in [".incognito(true)", ".on_navigation(", "check_navigation(", ".on_download(|_, _| false)",
                   "PermissionResponse::Deny", "NewWindowResponse::Deny", "devtools(cfg!(debug_assertions))",
                   'TAB_PREFIX: &str = "tab-"']:
        assert needle in src, needle


def test_unstable_feature_and_policy_dependency():
    cargo = (TAURI / "Cargo.toml").read_text()
    assert re.search(r'^tauri = \{[^}]*features = \[[^]]*"unstable"', cargo, re.M)
    assert "conductloop-url-policy" in cargo


def test_tabs_go_through_the_filtering_proxy():
    src = (TAURI / "src" / "browser.rs").read_text()
    assert ".proxy_url(proxy)" in src
    assert 'state.proxy_url.clone().ok_or("proxy_unavailable")' in src, "fail closed without proxy"
    assert 'Err("macos_14_required".into())' in src
    cargo = (TAURI / "Cargo.toml").read_text()
    assert '"macos-proxy"' in cargo
    build = (TAURI / "build.rs").read_text()
    assert "-weak_framework,Network" in build
