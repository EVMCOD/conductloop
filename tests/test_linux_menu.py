"""Static checks on the File menu of the desktop app (desktop/src-tauri/src/lib.rs).

Tauri's default menu has a File menu on macOS and Windows but none on Linux, so
Close Tab and Close Window would have neither a menu entry nor a shortcut
there. These checks keep the macOS path (items appended to the default File
menu) and the Linux fallback (a File menu created first) from drifting. They
need no Rust toolchain; the same facts are also pinned by Rust unit tests that
``cargo test`` runs in the desktop CI job. The menu itself is untested on a
real Linux desktop.
"""
from __future__ import annotations

import re
from pathlib import Path

LIB = (Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "src" / "lib.rs").read_text()


def _app_menu() -> str:
    start = LIB.index("fn app_menu<")
    return LIB[start : LIB.index("let go = Submenu", start)]


def test_file_items_are_the_macos_ones():
    items = re.search(r"const FILE_ITEMS[^=]*=\s*\[(.*?)\];", LIB, re.S)
    assert items, "FILE_ITEMS missing"
    assert re.findall(r'\("([^"]+)",\s*"([^"]+)",\s*"([^"]+)"\)', items.group(1)) == [
        ("close-tab", "Close Tab", "CmdOrCtrl+W"),
        ("close-window", "Close Window", "CmdOrCtrl+Shift+W"),
    ]


def test_shortcuts_use_cmd_or_ctrl():
    items = re.search(r"const FILE_ITEMS[^=]*=\s*\[(.*?)\];", LIB, re.S).group(1)
    accels = re.findall(r'"([^"]*\+[^"]*)"', items)
    assert len(accels) == 2 and all(a.startswith("CmdOrCtrl+") for a in accels)


def test_default_file_menu_still_gets_the_items_appended():
    body = _app_menu()
    # macOS and Windows: the existing File submenu is extended, as before.
    assert 'if sub.text()? == "File" {' in body
    assert "sub.append(&it)?" in body
    assert "has_file = true" in body


def test_fallback_creates_a_file_menu_only_when_there_is_none():
    body = _app_menu()
    fallback = body[body.index("if !has_file {") :]
    assert 'Submenu::with_items(handle, "File", true, &refs)' in fallback
    assert "menu.insert(" in fallback and ", 0)" in fallback
    # The fallback is decided by the menu content, not by a target_os cfg that
    # could change what macOS builds.
    assert "target_os" not in fallback


def test_macos_only_app_menu_entry_is_untouched():
    body = _app_menu()
    assert 'if i == 0 && cfg!(target_os = "macos")' in body
    assert '"check-updates"' in body


def test_menu_events_handle_both_ids():
    assert '("close-tab", Some(tab))' in LIB
    assert '("close-tab" | "close-window", _)' in LIB
