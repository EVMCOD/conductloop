"""What happens when the session token in the cookie stops matching.

The window keeps its session token in a cookie. If the server restarts with
a different token, that cookie stops matching and every request answers 403.

The token is now kept between launches, so the cookie in the window that is
already open keeps working. What is left is the case where the app was
updated under an open window: the window holds a token that no longer
exists.

That case is NOT repaired by reloading and it must NOT be repaired by
handing the token out with the page: every browser that visits localhost
would then get the session. The page refuses the request without a cookie,
and the app says what to do — reopen it, which boots with a one-time nonce.

These tests pin that: no silent reload, and a message that tells the user
what to do.
"""
from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_there_is_no_silent_reload():
    """A reload cannot repair this, and it would leave a blank page."""
    for name in ("app.js", "connectors.js"):
        src = _read(name)
        assert "reloadOnce" not in src, (
            f"{name} reloads the window when the session is lost; the page "
            "needs a token too, so the reload ends in a blank page")
        assert "session-repair.js" not in src, (
            f"{name} still loads the repair helper")


def test_the_pages_do_not_load_the_repair_helper():
    for page in ("index.html", "connectors.html"):
        assert "session-repair.js" not in _read(page), (
            f"{page} still loads the repair helper")
    assert not (WEB / "session-repair.js").exists(), (
        "the repair helper is still there, and it reloads the window")


def test_a_lost_session_says_what_to_do():
    for name in ("app.js", "connectors.js"):
        src = _read(name)
        assert "403" in src, f"{name} does not recognise a lost session"
        assert "reopen ConductLoop" in src, (
            f"{name} does not tell the user how to recover from a lost session")
