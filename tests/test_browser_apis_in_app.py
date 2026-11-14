"""Browser APIs that behave differently inside the desktop window (Tauri/wry).

Same class of bug as alert/confirm/prompt (tests/test_confirm_ui.py): code
that is correct in a browser and silently does nothing in the packaged app.
Checked in a packaged debug build (macOS, WKWebView) on 2026-11-05, before
the fix:

* ``window.open(...)`` returned ``null`` and ``<a target="_blank">`` did
  nothing: wry only creates a window when ``on_new_window`` is set, and the
  main window had none (only the browser tabs, browser.rs). The chat's links,
  the status page's links, the sidebar URLs and "↓ save" (export
  conversation, which used ``window.open``) were all dead.
* ``<a download>`` (same origin or blob) saved nothing: wry cancels a
  download unless ``on_download`` is set.
* A plain link to another site replaced the whole app with that site, with
  no way back (the window has no browser chrome).
* ``Notification.requestPermission()`` resolves ``"denied"``.
* ``navigator.clipboard.writeText`` works: the app's origin
  (http://localhost) is a secure context.

The main window is now built in main_window.rs with the three handlers.
These tests keep the pieces from going away.

Packaged check note (macOS WKWebView): a synthetic ``<a target="_blank">``
click without a real user gesture may not fire WebKit's new-window callback.
Handlers are in place; real user clicks on chat/status links are the path
that opens the system browser.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ops_dashboard" / "web"
TAURI = ROOT / "desktop" / "src-tauri"
LIB = (TAURI / "src" / "lib.rs").read_text(encoding="utf-8")
MAIN_WINDOW = (TAURI / "src" / "main_window.rs").read_text(encoding="utf-8")
CONF = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))


def _web_sources() -> list[Path]:
    return sorted(
        p for p in WEB.rglob("*")
        if p.suffix in {".js", ".html"} and "vendor" not in p.parts
    )


def _main_conf() -> dict:
    return next(w for w in CONF["app"]["windows"] if w["label"] == "main")


def test_the_main_window_is_built_with_its_handlers_not_from_the_config():
    # From the config it would get no on_new_window / on_download.
    assert _main_conf().get("create") is False
    assert "main_window::build(app, BACKEND_URL)" in LIB
    for handler in (".on_navigation(", ".on_new_window(", ".on_download("):
        assert handler in MAIN_WINDOW, handler


def test_new_windows_are_always_denied_and_routed():
    # The window never opens a second webview: other sites go to the system
    # browser, the app's own pages load in the main window.
    assert "NewWindowResponse::Deny" in MAIN_WINDOW
    assert "NewWindowResponse::Allow" not in MAIN_WINDOW
    assert "open_external" in MAIN_WINDOW


def test_internal_drag_and_drop_reaches_the_page():
    # With Tauri's file drop handler on, WKWebView never sees a drag, so the
    # page's own HTML5 drag and drop (the agents org chart) gets no `drop`.
    # A file dropped on the window is a file: navigation, refused by
    # on_navigation (Route::Block).
    assert _main_conf().get("dragDropEnabled") is False


def test_no_page_opens_an_app_url_in_a_new_window():
    # window.open('/api/...') was the conversation export; a same-origin
    # file must be an <a download> so the app saves it.
    same_origin_open = re.compile(r"window\s*\.\s*open\s*\(\s*['\"]/")
    hits = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in _web_sources()
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if same_origin_open.search(line)
    ]
    assert not hits, hits


def test_conversation_export_is_a_download():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    body = app[app.index("async function exportCurrentChat"):]
    body = body[: body.index("\n}\n")]
    assert "window.open(" not in body
    assert ".download = " in body
    server = (ROOT / "ops_dashboard" / "server.py").read_text(encoding="utf-8")
    export = server[server.index('path == "/api/brain/conversation/export"'):]
    export = export[: export.index("elif path ==")]
    assert "Content-Disposition" in export and "attachment" in export


def test_no_page_relies_on_notifications_or_beforeunload():
    # Notification.requestPermission() resolves "denied" in the app, and the
    # window has no beforeunload prompt: both would fail silently.
    banned = re.compile(r"\bNotification\s*[.(]|new\s+Notification\b|beforeunload")
    hits = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in _web_sources()
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if banned.search(line)
    ]
    assert not hits, hits


def test_the_note_copy_button_reports_on_itself():
    note = (WEB / "note.html").read_text(encoding="utf-8")
    start = note.index("querySelector('[data-copy]')")
    block = note[start: start + 800]
    assert "copyBtn.textContent = '✓ Copied'" in block
    assert "catch (e)" in block
