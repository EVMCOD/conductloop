"""The session token has to survive a restart of the server.

The desktop app generates a token per launch and passes it to the server in
the environment. The browser gets that token as a cookie, and every API call
from the window has to carry it.

So when the server restarts — an update, a crash, the scheduler — the token
changes, the cookie in the open window stops matching, and **every request
answers 403 `app_token_required`**. The frontend turns that into an error
and the page stops working: no data, no buttons, nothing.

The user sees an app that has stopped working, and the only way out is to
quit and reopen it.

The token is a session key for this app on this computer. It is persisted,
so it keeps working across restarts, and the window that is already open
keeps its cookie.

A note on the development server: with no token in the environment the
guard stays off, exactly as before. Otherwise `make serve` would suddenly
need a cookie and nobody would be able to open the app.
"""
from __future__ import annotations

import pytest

from ops_dashboard import app_token_guard as guard


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_DATA_DIR", tmp_path)
    return tmp_path


def test_the_token_from_the_environment_is_persisted(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv(guard.TOKEN_ENV, "a" * 40)
    token = guard.resolve_token()
    assert len(token) >= guard.MIN_LEN
    assert (tmp_path / "app-token").exists(), "the token was not persisted"


def test_the_persisted_token_wins_over_a_new_one(tmp_path, data_dir, monkeypatch):
    """The next launch brings a DIFFERENT token in the environment.

    The one on disk is the one the open window already has as a cookie, so
    it is the one that is used. Otherwise every restart would break the app.
    """
    monkeypatch.setenv(guard.TOKEN_ENV, "a" * 40)
    first = guard.resolve_token()
    assert (tmp_path / "app-token").read_text(encoding="utf-8").strip() == first

    monkeypatch.setenv(guard.TOKEN_ENV, "b" * 40)
    second = guard.resolve_token()
    assert second == first, (
        "the token changed on restart: the cookie in the open window stops "
        "matching and every request answers 403")


def test_a_cookie_from_before_the_restart_still_works(tmp_path, data_dir, monkeypatch):
    """The whole point: the window that is already open keeps working."""
    monkeypatch.setenv(guard.TOKEN_ENV, "c" * 40)
    token = guard.resolve_token()

    # The server restarts and the shell brings a new token.
    monkeypatch.setenv(guard.TOKEN_ENV, "d" * 40)
    guard.resolve_token()

    assert guard.has_valid_cookie({"Cookie": f"{guard.COOKIE}={token}"})


def test_a_wrong_cookie_is_still_refused(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv(guard.TOKEN_ENV, "a" * 40)
    guard.resolve_token()
    assert not guard.has_valid_cookie({"Cookie": f"{guard.COOKIE}=" + "x" * 40})
    assert not guard.has_valid_cookie({})


def test_the_token_file_is_not_world_readable(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv(guard.TOKEN_ENV, "a" * 40)
    guard.resolve_token()
    path = tmp_path / "app-token"
    mode = path.stat().st_mode & 0o777
    assert mode & 0o077 == 0, f"the token file is readable by others: {oct(mode)}"


def test_the_development_server_stays_open(tmp_path, data_dir, monkeypatch):
    """No token in the environment: the guard stays off, as it always was.

    Otherwise `make serve` would need a cookie and the app could not be
    opened in development at all.
    """
    monkeypatch.delenv(guard.TOKEN_ENV, raising=False)
    assert not guard.enabled()


def test_the_guard_is_on_when_the_app_passes_a_token(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv(guard.TOKEN_ENV, "a" * 40)
    assert guard.enabled()


def test_a_token_that_is_too_short_does_not_count(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv(guard.TOKEN_ENV, "short")
    assert not guard.enabled()
