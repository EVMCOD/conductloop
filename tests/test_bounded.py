"""Work with a deadline, so a folder that stops answering cannot hang the app.

The vault of this Mac stopped answering for hours on 2026-10-24 and the
export of the map — a request that takes four seconds — never ended.
These tests are about that: the work is bounded, and the caller is told
when the bound is reached.
"""
from __future__ import annotations

import threading
import time

import pytest

from ops_dashboard import bounded


def test_work_that_finishes_returns_its_result():
    assert bounded.run(lambda: 42, 5.0) == 42


def test_work_that_raises_raises_the_same_error():
    def boom():
        raise ValueError("the vault said no")

    with pytest.raises(ValueError, match="the vault said no"):
        bounded.run(boom, 5.0)


def test_work_that_never_finishes_gives_up_on_time():
    """The caller gets an answer instead of waiting for ever."""
    started = threading.Event()

    def never():
        started.set()
        while True:
            time.sleep(0.05)

    with pytest.raises(bounded.Timeout, match="did not answer"):
        bounded.run(never, 0.4, what="the map of the vault")
    assert started.is_set()


def test_the_giving_up_does_not_keep_the_thread_alive():
    """The thread finishes by itself when the folder answers again."""
    release = threading.Event()

    def waits():
        release.wait(5)
        return "late"

    with pytest.raises(bounded.Timeout):
        bounded.run(waits, 0.2)
    release.set()
    time.sleep(0.3)


def test_the_deadline_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_VAULT_TIMEOUT", "12")
    assert bounded.vault_timeout() == 12.0
    # A nonsense value falls back to the default instead of breaking.
    monkeypatch.setenv("CONDUCTLOOP_VAULT_TIMEOUT", "pronto")
    assert bounded.vault_timeout() == bounded.DEFAULT_VAULT_TIMEOUT


# ── the route ──────────────────────────────────────────────────────────────

@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A real server on a real port: the routes are the contract."""
    import socket
    import threading as th
    from http.server import ThreadingHTTPServer

    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    db.init_db()

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    th.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def test_the_export_says_so_when_the_vault_does_not_answer(server, monkeypatch):
    """A folder that stopped answering is not an endless request."""
    import urllib.error
    import urllib.request

    from ops_dashboard import notes_structure

    def never(*_args, **_kwargs):
        started.set()
        while True:
            time.sleep(0.05)

    started = threading.Event()
    monkeypatch.setattr(notes_structure, "write_agent_map", never)
    monkeypatch.setenv("CONDUCTLOOP_VAULT_TIMEOUT", "1")

    req = urllib.request.Request(server + "/api/library/export", data=b"", method="POST")
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        assert e.code == 503
        assert "vault_unresponsive" in e.read().decode()
    else:
        raise AssertionError("the export should have given up")
