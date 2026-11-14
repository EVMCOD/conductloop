"""A browser dropping a connection is not a server error.

The stdlib server printed a full traceback for every "Connection reset by
peer" (a tab closed, a stream given up on), so the app's log filled with them
and hid real errors. Those must still be printed.
"""
import pytest

from ops_dashboard.server import _LocalHTTPServer


def _handled(exc: BaseException, capsys) -> str:
    server = _LocalHTTPServer.__new__(_LocalHTTPServer)  # no socket needed
    try:
        raise exc
    except BaseException:
        server.handle_error(None, ("127.0.0.1", 1234))
    return capsys.readouterr().err


@pytest.mark.parametrize("exc", [ConnectionResetError(54, "Connection reset by peer"),
                                 BrokenPipeError(32, "Broken pipe"),
                                 ConnectionAbortedError(53, "aborted")])
def test_a_dropped_connection_is_not_logged(exc, capsys):
    assert _handled(exc, capsys) == ""


def test_a_real_error_is_still_logged(capsys):
    err = _handled(ValueError("boom"), capsys)
    assert "Traceback" in err and "ValueError: boom" in err


def test_a_route_that_loses_its_client_is_not_a_500(caplog):
    """The route-level handler logged BrokenPipeError as "[500 …] ERROR" and
    tried to answer a socket that was gone."""
    from ops_dashboard.server import DashboardHandler

    h = DashboardHandler.__new__(DashboardHandler)
    h.command, h.path = "GET", "/api/health"
    sent = []
    h._send = lambda *a, **k: sent.append(a)
    with caplog.at_level("DEBUG", logger="conductloop.server"):
        h._send_500(BrokenPipeError(32, "Broken pipe"))
    assert sent == []
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_a_real_route_error_is_still_a_500(caplog, monkeypatch):
    from ops_dashboard import logging_setup
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setattr(logging_setup, "report_exception", lambda exc: None)
    h = DashboardHandler.__new__(DashboardHandler)
    h.command, h.path = "GET", "/api/x"
    sent = []
    h._send = lambda *a, **k: sent.append(a)
    with caplog.at_level("ERROR", logger="conductloop.server"):
        h._send_500(ValueError("boom"))
    assert sent and sent[0][0] == 500
    assert [r for r in caplog.records if r.levelname == "ERROR"]
