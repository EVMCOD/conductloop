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
    # The app hides the tabs when the main webview starts loading any page and
    # shows them when the browser page has loaded (also from the history cache).
    lib = (Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "src" / "lib.rs").read_text()
    started = lib.index("PageLoadEvent::Started => {")
    assert "browser::set_visible(webview.app_handle(), false)" in lib[started:started + 200]
    finished = lib.index("PageLoadEvent::Finished => {")
    assert "browser::set_visible(webview.app_handle(), true)" in lib[finished:finished + 300]
    assert '"/browser" | "/browser.html"' in lib
    # A visibility call from the page while it is being left gets frozen in the
    # history cache and delivered on the way back, hiding the tabs again.
    assert '"pagehide"' not in HTML and '"visibilitychange"' not in HTML
    assert '"pageshow"' in HTML


def test_save_page_not_send_to_vault():
    """Clips go into ConductLoop's index (source=web), not the notes folder.
    The old label "Send to vault" sounded like writing into the Obsidian vault."""
    assert "Send to vault" not in HTML
    assert "Saved to vault" not in HTML
    assert 'id="clip"' in HTML and ">Save page<" in HTML
    assert "not written into your notes folder" in HTML
    assert "Saved: " in HTML


def test_tab_strip_survives_a_refresh_during_a_click():
    # setInterval(refresh) used to rebuild #tabs between mousedown and click,
    # which drops the gesture. Act on pointerdown and skip identical rebuilds.
    assert 'addEventListener("pointerdown"' in HTML
    assert "tabsSig" in HTML
    assert "data-id" in HTML
    assert "browser_focus_ui" in HTML


def test_blank_tabs_do_not_steal_the_keyboard():
    rs = (Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "src" / "browser.rs").read_text()
    assert "fn tab_should_show" in rs
    assert "blank_user_tabs_stay_hidden" in rs
    assert "browser_focus_ui" in rs
    # The old 300ms sleep-then-focus race is gone; blank tabs stay hidden instead.
    assert "from_millis(300)" not in rs
    assert "focus_ui(app)" in rs
