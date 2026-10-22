"""CORS allowlist + cross-site write protection, against the REAL local server.

Covers allowed / forbidden origins, OPTIONS preflight, Sec-Fetch-Site
cross-site, Origin: null, no-Origin clients from loopback (curl/CLI), DNS
rebinding to loopback, side-effect GETs, seed-starter force confirmation, and
the formerly browser-CSRF-able routes (/api/brain/providers/key,
/api/agents/create, /api/agents/run).
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import origin_guard

EVIL = "https://evil.example"


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    dbp = tmp_path / "og.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    monkeypatch.delenv("OPS_ALLOWED_ORIGINS", raising=False)
    from ops_dashboard import brain, db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    key_calls: list[dict] = []

    def fake_set_key(provider, **kw):        # never touch a real keychain
        key_calls.append({"provider": provider, **kw})
        return {"ok": True, "provider": provider}
    monkeypatch.setattr(brain, "set_provider_key", fake_set_key)
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

    def call(method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw, status, hdrs = r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:
            raw, status, hdrs = e.read(), e.code, e.headers
        try:
            js = json.loads(raw)
        except ValueError:
            js = {"raw": raw.decode("utf-8", "replace")}
        return status, js, hdrs

    yield {"call": call, "port": port, "db": db, "key_calls": key_calls}
    server.shutdown()


def _forbidden(r, reason):
    st, js, _ = r
    assert st == 403, r
    assert js == {"ok": False, "error": "forbidden_origin", "code": "forbidden_origin",
                  "reason": reason, "message": origin_guard.MESSAGES[reason]}


def test_allowlist_contents(srv):
    allowed = origin_guard.allowed_origins(srv["port"])
    for o in ("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
              "http://localhost:7777", "http://127.0.0.1:7777",
              f"http://localhost:{srv['port']}", f"http://127.0.0.1:{srv['port']}"):
        assert o in allowed
    assert EVIL not in allowed and "http://localhost:3000" not in allowed


def test_preflight_options(srv):
    call = srv["call"]
    for o in ("http://localhost:7777", "tauri://localhost", f"http://127.0.0.1:{srv['port']}"):
        st, _, h = call("OPTIONS", "/api/agents/create", headers={"Origin": o})
        assert st == 204
        assert h["Access-Control-Allow-Origin"] == o
        assert h["Access-Control-Allow-Credentials"] == "true"
    for o in (EVIL, "null", "http://localhost:3000"):
        st, _, h = call("OPTIONS", "/api/agents/create", headers={"Origin": o})
        assert st == 204
        assert h.get("Access-Control-Allow-Origin") is None
        assert h.get("Access-Control-Allow-Credentials") is None
    # public read-only GET keeps * for the marketing site, never with credentials
    st, _, h = call("OPTIONS", "/api/pricing-tier", headers={"Origin": "https://conductloop.com"})
    assert h["Access-Control-Allow-Origin"] == "*" and h.get("Access-Control-Allow-Credentials") is None


def test_normal_responses_no_longer_reflect_foreign_origins(srv):
    call = srv["call"]
    _, _, h = call("GET", "/api/health", headers={"Origin": EVIL})
    assert h["Access-Control-Allow-Origin"] == "*" and h.get("Access-Control-Allow-Credentials") is None
    _, _, h = call("GET", "/api/projects", headers={"Origin": EVIL})
    assert h.get("Access-Control-Allow-Origin") is None
    _, _, h = call("GET", "/api/projects", headers={"Origin": "http://localhost:7777"})
    assert h["Access-Control-Allow-Origin"] == "http://localhost:7777"
    assert h["Access-Control-Allow-Credentials"] == "true"


@pytest.mark.parametrize("path,body", [
    ("/api/brain/providers/key", {"provider": "openai", "api_key": "x"}),
    ("/api/agents/create", {"name": "pwned"}),
    ("/api/agents/run", {"id": 1}),
    ("/api/boardroom/seed-starter", {}),
    ("/api/docs/save", {}),
])
def test_foreign_writes_refused_even_from_loopback(srv, path, body):
    call = srv["call"]
    _forbidden(call("POST", path, body, {"Origin": EVIL}), "origin_not_allowed")
    _forbidden(call("POST", path, body, {"Origin": "http://localhost:3000"}), "origin_not_allowed")
    _forbidden(call("POST", path, body, {"Origin": "null"}), "origin_null")
    _forbidden(call("POST", path, body, {"Sec-Fetch-Site": "cross-site"}), "cross_site")
    _forbidden(call("POST", path, body, {"Origin": "http://localhost:7778",
                                          "Sec-Fetch-Site": "cross-site"}), "cross_site")
    assert srv["key_calls"] == []
    with srv["db"].get_connection() as c:
        assert c.execute("SELECT COUNT(*) FROM team_members WHERE name='pwned'").fetchone()[0] == 0


def test_put_and_delete_are_guarded_too(srv):
    call = srv["call"]
    _forbidden(call("PUT", "/api/ideas/1", {"title": "x"}, {"Origin": EVIL}), "origin_not_allowed")
    _forbidden(call("DELETE", "/api/ideas/1", None, {"Sec-Fetch-Site": "cross-site"}), "cross_site")


def test_dns_rebinding_to_loopback_is_refused(srv):
    # evil.example resolved to 127.0.0.1: Host and Origin agree, but loopback
    # never falls back to "same Host". Since the loopback Host check (#26) runs
    # before the Origin guard, a foreign Host is refused with 421 invalid_host.
    h = {"Host": f"evil.example:{srv['port']}", "Origin": f"http://evil.example:{srv['port']}"}
    st, js, _ = srv["call"]("POST", "/api/agents/create", {"name": "pwned"}, h)
    assert st == 421, (st, js)
    assert js["error"] == "invalid_host" and js["code"] == "invalid_host"


def test_allowed_origins_and_no_origin_clients_reach_the_handlers(srv):
    call = srv["call"]
    # No Origin (curl / CLI on the machine): still allowed from loopback.
    st, js, _ = call("POST", "/api/agents/create", {})
    assert (st, js) == (400, {"error": "name required"})
    st, js, _ = call("POST", "/api/agents/run", {})
    assert (st, js) == (400, {"error": "id required"})
    st, js, _ = call("POST", "/api/brain/providers/key", {"provider": "ollama"})
    assert st == 200 and srv["key_calls"] == [{"provider": "ollama", "api_key": None, "model": None,
                                                "base_url": None, "make_default": False}]
    # The app's own origins (desktop webview / Tauri) are allowed.
    for o in ("http://localhost:7777", "tauri://localhost", f"http://127.0.0.1:{srv['port']}"):
        st, js, _ = call("POST", "/api/agents/create", {}, {"Origin": o, "Sec-Fetch-Site": "same-origin"})
        assert (st, js) == (400, {"error": "name required"}), o
    st, js, _ = call("POST", "/api/agents/create", {"name": "ok-agent"},
                     {"Origin": "http://localhost:7777"})
    assert st == 200, js


def test_extra_origins_via_env(srv, monkeypatch):
    monkeypatch.setenv("OPS_ALLOWED_ORIGINS", "https://my.host:8443, http://box.local:7777/")
    st, js, _ = srv["call"]("POST", "/api/agents/create", {}, {"Origin": "https://my.host:8443"})
    assert st == 400 and js == {"error": "name required"}
    st, js, _ = srv["call"]("POST", "/api/agents/create", {}, {"Origin": "http://box.local:7777"})
    assert st == 400


def test_remote_same_host_origin_still_needs_auth(srv):
    call = srv["call"]
    host = f"192.168.1.20:{srv['port']}"
    remote = {"X-Forwarded-For": "192.168.1.30", "Host": host, "Origin": f"http://{host}"}
    st, js, _ = call("POST", "/api/ideas", {"title": "x"}, remote)
    assert st == 401                          # origin ok (same Host), but no session/token
    st, js, _ = call("POST", "/api/ideas", {"title": "x"}, {**remote, "Origin": EVIL})
    assert st == 403 and js["error"] == "forbidden_origin"


def test_side_effect_gets_are_guarded(srv):
    call = srv["call"]
    for p in ("/api/revenue/seed-mock", "/api/webhooks/test", "/api/telegram/clear_webhook",
              "/api/auth/logout", "/api/brain/ask", "/api/brain/briefing", "/api/digest",
              "/api/github/sync"):
        _forbidden(call("GET", p, headers={"Origin": EVIL}), "origin_not_allowed")
        _forbidden(call("GET", p, headers={"Sec-Fetch-Site": "cross-site"}), "cross_site")
    # Any other /api GET from a foreign page is refused too (its response was
    # unreadable anyway), so a side-effect GET missing from the list is covered.
    _forbidden(call("GET", "/api/license/status", headers={"Sec-Fetch-Site": "cross-site"}),
               "cross_site")
    # Same-origin reads (no Origin on a same-origin GET), CLI clients and the
    # public CORS paths are unaffected.
    st, _, _ = call("GET", "/api/license/status", headers={"Sec-Fetch-Site": "same-origin"})
    assert st == 200
    st, _, _ = call("GET", "/api/license/status")
    assert st == 200
    st, _, _ = call("GET", "/api/health", headers={"Sec-Fetch-Site": "cross-site", "Origin": EVIL})
    assert st == 200
    # Non-API pages (the UI itself, notes) are not origin-checked.
    assert not origin_guard.needs_check("GET", "/")
    assert origin_guard.SIDE_EFFECT_GETS >= {"/api/billing/cancel-subscription", "/api/docs/create"}


def test_seed_starter_force_requires_explicit_confirmation(srv):
    call, db = srv["call"], srv["db"]
    db.seed_default_teams()   # a fresh DB has no team yet (seed-starter itself can't create one)
    st, js, _ = call("POST", "/api/boardroom/seed-starter", {}, {"Origin": "http://localhost:7777"})
    assert st == 200, js
    with db.get_connection() as c:
        n = c.execute("SELECT COUNT(*) FROM team_members WHERE type='ai_agent'").fetchone()[0]
        c.execute("INSERT INTO team_members (team_id, name, type, level, manager_id, created_at, updated_at) "
                  "SELECT team_id, 'Custom CEO', 'ai_agent', 'ceo', id, 0, 0 FROM team_members "
                  "WHERE manager_id IS NULL LIMIT 1")
        c.commit()
    assert n > 0

    def custom():
        with db.get_connection() as c:
            return c.execute("SELECT COUNT(*) FROM team_members WHERE name='Custom CEO'").fetchone()[0]
    assert custom() == 1
    for body in ({"force": True}, {"force": True, "confirm": "yes"}, {"force": True, "confirm": True}):
        st, js, _ = call("POST", "/api/boardroom/seed-starter", body)
        assert st == 400 and js["error"] == "confirmation_required", (body, js)
        assert custom() == 1
    _forbidden(call("POST", "/api/boardroom/seed-starter", {"force": True, "confirm": "delete"},
                    {"Origin": EVIL}), "origin_not_allowed")
    assert custom() == 1
    st, js, _ = call("POST", "/api/boardroom/seed-starter", {"force": True, "confirm": "delete"})
    assert st == 200, js
    assert custom() == 0


def test_seed_starter_is_no_longer_public_for_remote(srv):
    st, js, _ = srv["call"]("POST", "/api/boardroom/seed-starter", {},
                            {"X-Forwarded-For": "10.0.0.9"})
    assert st == 401
