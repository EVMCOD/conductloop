"""Desktop Back/Home controls (desktop/src-tauri/src/nav.js).

The app window has no browser chrome; lib.rs evaluates nav.js in the main
webview after each page load. Checked in a real headless Chrome: the controls
appear on the app's own pages, never on the dashboard root, and never on
another origin (browser tabs show other sites).
"""
from __future__ import annotations

import functools
import html
import re
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.test_browser_visibility_js import CHROME

SRC = Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "src"
NAV = (SRC / "nav.js").read_text(encoding="utf-8")


def test_lib_injects_nav_into_the_main_webview_only():
    lib = (SRC / "lib.rs").read_text(encoding="utf-8")
    assert 'include_str!("nav.js")' in lib
    assert 'if webview.label() != "main" {' in lib
    finished = lib.index("PageLoadEvent::Finished => {")
    assert 'NAV_JS.replace("__CL_ORIGIN__", BACKEND_URL)' in lib[finished:finished + 400]
    for item in ('"go-back"', '"go-forward"', '"go-home"', '"CmdOrCtrl+["', '"CmdOrCtrl+]"'):
        assert item in lib
    assert "location.origin !== '__CL_ORIGIN__'" in NAV


def _dump(tmp_path: Path, url: str) -> str:
    out = subprocess.run(
        [str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
         "--disable-extensions", "--disable-background-networking", "--window-size=1200,800",
         f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=3000", "--dump-dom", url],
        capture_output=True, text=True, timeout=90, check=False)
    return out.stdout


@pytest.mark.skipif(CHROME is None, reason="no headless Chrome/Chromium")
@pytest.mark.parametrize("path,origin_matches,expected", [
    ("/chat", True, True),          # an app page: controls shown
    ("/", False, False),            # placeholder kept: wrong origin → nothing
    ("/", True, False),             # the dashboard root has its own navigation
    ("/chat", False, False),        # another site (a browser tab): nothing
    ("/browser", True, False),      # the browser page: tabs cover that corner, it has ← App
])
def test_controls_only_on_app_pages(tmp_path, path, origin_matches, expected):
    site = tmp_path / "site"
    site.mkdir()
    srv = ThreadingHTTPServer(("127.0.0.1", 0),
                              functools.partial(SimpleHTTPRequestHandler, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{srv.server_address[1]}"
    script = NAV.replace("__CL_ORIGIN__", origin if origin_matches else "http://localhost:1")
    # A page `button` rule like agents.html's must not leak into the controls.
    hostile = "<style>*{box-sizing:border-box} button{padding:11px 18px;margin-top:16px;font-size:20px}</style>"
    measure = ("var n = document.getElementById('cl-nav'); if (n) n.dataset.h = "
               "Math.round(n.getBoundingClientRect().height);")
    page = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>t</title>" + hostile +
            "</head><body><h1>Page</h1><script>window.addEventListener('load', function () {"
            + script + measure + "});</script></body></html>")
    (site / "index.html").write_text(page, encoding="utf-8")
    (site / "chat.html").write_text(page, encoding="utf-8")
    (site / "browser.html").write_text(page, encoding="utf-8")
    try:
        dom = _dump(tmp_path, origin + {"/chat": "/chat.html", "/browser": "/browser.html"}.get(path, "/"))
    finally:
        srv.shutdown()
    assert "<h1>Page</h1>" in dom, dom[-500:]
    has_nav = 'id="cl-nav"' in dom
    assert has_nav is expected
    if expected:
        labels = [html.unescape(b) for b in re.findall(r"<button[^>]*>(.*?)</button>", dom)]
        assert labels == ["← Back", "⌂"]
        height = int(re.search(r'data-h="(\d+)"', dom).group(1))
        assert height <= 40, height     # 32px buttons + 3px padding + border
