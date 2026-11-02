"""The boot must hand out the token the server actually accepts.

The window gets its session cookie from the boot (``/__cl/boot``), and every
request it makes afterwards has to carry that cookie. The server checks it
against the token it accepts.

Those two were not the same token. The boot handed out the one from the
environment — a new one on every launch — while the server checked the
persisted one, which does not change. So from the second launch onwards the
window held a cookie that answered 403 to everything: the page loaded with
no data, no buttons, nothing.

That is the same failure as before the token was persisted, in a new place:
the session was kept, but the cookie that carries it was the wrong one.
"""
from __future__ import annotations

import pytest

from ops_dashboard import app_token_guard as guard


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_DATA_DIR", tmp_path)
    return tmp_path


def test_the_boot_hands_out_the_token_the_server_accepts(data_dir, monkeypatch):
    monkeypatch.setenv(guard.TOKEN_ENV, "a" * 40)
    guard.resolve_token()

    # The next launch: the shell hands the server a different token.
    monkeypatch.setenv(guard.TOKEN_ENV, "b" * 40)

    cookie = guard.session_token()
    assert guard.has_valid_cookie({"Cookie": f"{guard.COOKIE}={cookie}"}), (
        "the cookie the boot hands out is not the one the server accepts: "
        "the window would answer 403 to everything")


def test_the_cookie_the_boot_hands_out_survives_a_restart(data_dir, monkeypatch):
    """The cookie in the window that is already open keeps working."""
    monkeypatch.setenv(guard.TOKEN_ENV, "a" * 40)
    first = guard.session_token()

    monkeypatch.setenv(guard.TOKEN_ENV, "b" * 40)
    second = guard.session_token()

    assert first == second, (
        "the token changed on restart, so the cookie in the open window "
        "stops matching and every request answers 403")


def test_the_boot_still_works_on_the_first_launch(data_dir, monkeypatch):
    """No token on disk yet: the one from the environment is used."""
    monkeypatch.setenv(guard.TOKEN_ENV, "a" * 40)
    cookie = guard.session_token()
    assert len(cookie) >= guard.MIN_LEN
    assert guard.has_valid_cookie({"Cookie": f"{guard.COOKIE}={cookie}"})


def test_the_boot_hands_out_nothing_without_a_token(data_dir, monkeypatch):
    """The development server has no token, and no session either."""
    monkeypatch.delenv(guard.TOKEN_ENV, raising=False)
    assert guard.session_token() == ""
