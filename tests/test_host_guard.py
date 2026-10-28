"""Loopback Host-header check (DNS rebinding), against the REAL local server."""
from __future__ import annotations

import http.client
import json
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import host_guard

INVALID = {"ok": False, "error": "invalid_host", "code": "invalid_host"}


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    dbp = tmp_path / "hg.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    monkeypatch.delenv("OPS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("OPS_DASHBOARD_BASE_URL", raising=False)
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
    server = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(method, path, host, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        data = json.dumps(body).encode() if body is not None else b""
        for k, v in {"Content-Type": "application/json", "Content-Length": str(len(data)),
                     **(headers or {})}.items():
            conn.putheader(k, v)
        conn.endheaders(data)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            js = json.loads(raw)
        except ValueError:
            js = {"raw": raw.decode("utf-8", "replace")}
        return r.status, js, r

    yield {"call": call, "port": port, "db": db}
    server.shutdown()


def _invalid(res):
    st, js, r = res
    assert st == 421, res
    assert {k: js[k] for k in INVALID} == INVALID and js["message"]
    assert r.getheader("Connection", "").lower() == "close" or r.will_close


def test_allowed_hosts(srv):
    p = srv["port"]
    allowed = host_guard.allowed_hosts(p)
    for h in (f"localhost:{p}", f"127.0.0.1:{p}", f"[::1]:{p}", "localhost:7777", "127.0.0.1:7777"):
        assert h in allowed
    assert "tauri.localhost" not in allowed and "localhost" not in allowed


@pytest.mark.parametrize("host", ["localhost:{p}", "127.0.0.1:{p}", "[::1]:{p}", "LOCALHOST:{p}",
                                  "localhost:7777", "127.0.0.1:7777"])
def test_loopback_with_own_host_works(srv, host):
    st, js, _ = srv["call"]("GET", "/api/auth/me", host.format(p=srv["port"]))
    assert st == 200, js


@pytest.mark.parametrize("host", ["evil.example:{p}", "evil.example", "localhost.:{p}",
                                  "localhost", "127.0.0.1:1", "tauri.localhost", "",
                                  "127.0.0.1.nip.io:{p}", "localhost:{p}@evil.example"])
def test_loopback_with_foreign_host_gets_421(srv, host):
    _invalid(srv["call"]("GET", "/api/auth/me", host.format(p=srv["port"])))


def test_rebinding_write_never_reaches_the_handler(srv):
    p = srv["port"]
    for m in ("POST", "PUT", "DELETE", "OPTIONS"):
        _invalid(srv["call"](m, "/api/agents/create", f"evil.example:{p}", {"name": "pwned"}))
    with srv["db"].get_connection() as c:
        assert c.execute("SELECT COUNT(*) FROM team_members WHERE name='pwned'").fetchone()[0] == 0
    # the same write with the right Host reaches the handler
    st, js, _ = srv["call"]("POST", "/api/agents/create", f"127.0.0.1:{p}", {})
    assert (st, js) == (400, {"error": "name required"})


def test_missing_host_is_allowed(srv):
    # HTTP/1.0 clients may omit Host; browsers never do.
    with socket.create_connection(("127.0.0.1", srv["port"]), timeout=10) as s:
        s.sendall(b"GET /api/auth/me HTTP/1.0\r\n\r\n")
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    assert data.startswith(b"HTTP/1.0 200")


def test_remote_requests_are_unchanged(srv):
    call = srv["call"]
    remote = {"X-Forwarded-For": "203.0.113.7"}
    # public endpoint over a tunnel keeps working with its public Host
    st, _, _ = call("GET", "/api/health", "abc.trycloudflare.com", headers=remote)
    assert st == 200
    # protected endpoint: still the normal auth answer, not 421
    st, _, _ = call("GET", "/api/auth/me", "abc.trycloudflare.com", headers=remote)
    assert st == 401


def test_webhooks_through_a_tunnel_still_arrive(srv):
    call = srv["call"]
    tunnel = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Host": "hooks.example.com"}
    st, _, _ = call("POST", "/api/telegram/webhook", "hooks.example.com", {"update_id": 1}, tunnel)
    assert st != 421


def test_env_hosts_are_allowed(srv, monkeypatch):
    p = srv["port"]
    monkeypatch.setenv("OPS_ALLOWED_ORIGINS", "https://my.host:8443, http://box.local:7777/")
    monkeypatch.setenv("OPS_DASHBOARD_BASE_URL", "https://dash.example.com")
    for h in ("my.host:8443", "box.local:7777", "dash.example.com", "dash.example.com:443"):
        st, js, _ = srv["call"]("GET", "/api/auth/me", h)
        assert st == 200, (h, js)
    _invalid(srv["call"]("GET", "/api/auth/me", f"evil.example:{p}"))


def test_netloc_parsing():
    assert host_guard._netloc("https://A.example:443/x") == "a.example"
    assert host_guard._netloc("http://[::1]:7777") == "[::1]:7777"
    assert host_guard._netloc("box.local:9000") == "box.local:9000"
    assert host_guard._netloc("http://x:notaport") is None
    assert host_guard._netloc("") is None
