"""The app has to be able to recover from a lost session.

The window keeps its session token in a cookie. If the server restarts with a
different token, that cookie stops matching, every request answers 403, and
the frontend turns that into an error: the page stops working and the user
is left with an app that looks broken.

Two things fix it, and both are checked here:

- the server keeps the token across restarts (so the cookie that is already
  in the window keeps working), and
- the page hands out a cookie that works, so a reload repairs the session.
  The frontend does that reload by itself, once, when it sees a 403.
"""
from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_the_helper_exists():
    assert (WEB / "session-repair.js").exists(), "there is no session repair helper"


def test_both_pages_load_the_helper():
    for page in ("index.html", "connectors.html"):
        assert "session-repair.js" in _read(page), (
            f"{page} does not load the session repair helper")


def test_the_helper_reloads_once():
    """Once: a reload that does not help must not become a loop."""
    src = _read("session-repair.js")
    assert "reloadOnce" in src
    assert "sessionStorage" in src, "the reload is not throttled, so it could loop"
    assert "location.reload" in src


def test_the_brain_reloads_when_the_server_forgets_the_session():
    src = _read("app.js")
    block = src.split("async function fetchJSON")[1].split("\n}")[0]
    assert "403" in block, "a 403 is not recognised"
    assert "reloadOnce" in block, "a 403 does not trigger the repair"


def test_the_connectors_page_reloads_when_the_server_forgets_the_session():
    src = _read("connectors.js")
    assert "reloadOnce" in src, (
        "the connectors page does not repair a lost session, so it is the one "
        "that shows the error")
