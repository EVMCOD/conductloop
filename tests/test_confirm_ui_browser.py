"""A modal dialog must keep its keystrokes.

The dashboard has a document-level keydown handler: Escape navigates back
(`goBack()`), and Cmd+1..7 / Cmd+P / Cmd+J switch views and open the command
palette. Those handlers were harmless while a question was asked with the
native `confirm()`, because a native dialog blocks the page: the key went to
the dialog and the document never saw it.

The question is now asked in the page (web/confirm.js), so the dialog is
part of the document and a keydown inside it bubbles up to those handlers.
Cancelling a question with Escape would navigate the user away from what
they were doing.

Handling the dialog's own `cancel` event is not enough: `cancel` is a
different event from `keydown`, and by the time it fires the keydown has
already bubbled to the document.

So the dialog stops the propagation. This test drives a real browser: the
fake DOM in tests/test_confirm_ui.py cannot show bubbling, and bubbling is
the whole of the defect.

The page under test is a minimal one that loads the REAL helper from the
running server and registers a document-level keydown listener -- the shape
the dashboard's handler has. The control case (a key with no dialog open
DOES reach the document) proves the assertion is not vacuous.
"""
from __future__ import annotations

import os
import shutil
import socket
import threading

import pytest

REQUIRED = "1" in (os.environ.get("CONNECTORS_BROWSER_REQUIRED"), os.environ.get("LEARNING_BROWSER_REQUIRED"))
CHROME = (os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
          or shutil.which("chromium") or shutil.which("chromium-browser"))
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None  # type: ignore[assignment]
if not REQUIRED:
    pytestmark = pytest.mark.skipif(sync_playwright is None or not CHROME, reason="no Playwright/Chrome")


def _app_server(address, handler):
    """The app's own server class. The stdlib default queues 5 connections;
    Chrome opens six at once and a page now loads more files (appearance.js,
    app-nav.js), so the extra ones were dropped, a script never arrived and
    the test timed out once in six runs. The app serves with a queue of 128
    (server._LocalHTTPServer), and so must its tests."""
    from ops_dashboard.server import _LocalHTTPServer
    return _LocalHTTPServer(address, handler)


@pytest.fixture
def server(tmp_path, monkeypatch):
    assert sync_playwright is not None and CHROME, "Playwright and Chrome are required"
    dbp = tmp_path / "c.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = _app_server(("127.0.0.1", port), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


# A page with the shape the dashboard has: a document-level keydown handler.
PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>keys</title></head>
<body>
<button id="ask">Ask</button>
<script>
  window.__seen = [];
  // The dashboard's global hotkeys live exactly here, on the document.
  document.addEventListener('keydown', function (e) {
    window.__seen.push(e.key);
  });
  document.getElementById('ask').addEventListener('click', function () {
    window.__answer = confirmAction('Delete this?');
  });
</script>
</body></html>
"""


def test_a_modal_dialog_keeps_its_keystrokes(server):
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            pg = browser.new_context(locale="es-ES").new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            pg.set_content(PAGE)
            # The REAL helper, from the running server.
            pg.add_script_tag(url=server + "/confirm.js")

            # Control: with no dialog open, a key DOES reach the document.
            # Without this the assertion below would pass for a listener
            # that was never registered.
            pg.keyboard.press("Escape")
            assert pg.evaluate("window.__seen") == ["Escape"], pg.evaluate("window.__seen")

            pg.click("#ask")
            pg.wait_for_selector("#confirm-dialog[open]")

            # Escape cancels the question...
            pg.keyboard.press("Escape")
            pg.wait_for_selector("#confirm-dialog", state="detached")
            assert pg.evaluate("window.__answer") is False, "Escape must cancel the question"

            # ...and the document never saw the key. A native dialog used to
            # swallow it; an in-page one has to do the same, or the
            # dashboard's Escape handler navigates the user away.
            assert pg.evaluate("window.__seen") == ["Escape"], (
                "the key leaked to the document while the dialog was open: "
                + repr(pg.evaluate("window.__seen")))

            # The same for a key the dashboard uses for a view shortcut:
            # typing "n" in the prompt must not switch the view.
            pg.click("#ask")
            pg.wait_for_selector("#confirm-dialog[open]")
            pg.keyboard.press("n")
            assert pg.evaluate("window.__seen") == ["Escape"], (
                "a key leaked to the document while the dialog was open: "
                + repr(pg.evaluate("window.__seen")))
            pg.click("#confirm-no")
            pg.wait_for_selector("#confirm-dialog", state="detached")
        finally:
            browser.close()
    assert errors == [], errors
