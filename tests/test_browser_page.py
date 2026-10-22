"""/browser page (browser design PR 4): served, inert outside the app,
behind the experimental flag, and only talks to the browser_* commands."""
from __future__ import annotations

import re
from pathlib import Path

from ops_dashboard.routing import router

HTML = (Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "browser.html").read_text()


def test_route_registered():
    from ops_dashboard import routes_meta  # noqa: F401
    assert any(m == "GET" and p == "/browser" for m, _, _, p in router._routes)


def test_page_uses_only_browser_commands():
    cmds = set(re.findall(r'inv\("([a-z_]+)"', HTML))
    assert cmds and all(c.startswith("browser_") for c in cmds), cmds


def test_page_is_inert_without_tauri_and_behind_flag():
    assert "__TAURI_INTERNALS__" in HTML
    assert "only available inside the ConductLoop desktop app" in HTML
    assert 'cl.browser.experimental' in HTML


def test_no_innerhtml_with_page_data():
    # Titles/URLs come from untrusted pages: only textContent.
    for line in HTML.splitlines():
        if "innerHTML" in line:
            assert "t." not in line and "url" not in line.lower(), line


def test_hides_tabs_when_leaving():
    assert '"pagehide"' in HTML and "browser_set_visible" in HTML
