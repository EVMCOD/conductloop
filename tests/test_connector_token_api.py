"""The route, end to end, over HTTP.

The unit tests call the functions. This one drives the actual HTTP endpoint
of a running server, because the thing that must work is the route the page
calls: POST /api/connectors/<id>/connect-token with a token in the body.

The provider is faked at the transport level, so nothing leaves the machine.

The behaviours that matter here:

- A good token connects the connector and returns ok.
- The token lands in the keychain, NOT in the database.
- A bad token is refused with a clear code, and nothing is stored.
- A connector that does not accept a token is refused.
- The route is local-only: it describes the owner's own configuration, so it
  must not be reachable through a tunnel.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import httpx
import pytest

from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "api.db")
    dbmod.init_db()
    tokens.use_backend(tokens.MemoryBackend())
    saved = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    registry.register(registry.ConnectorSpec(
        id="slack", name="Slack", auth="oauth_pkce",
        allowed_hosts=("slack.com",), token_auth=True,
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access"))
    oauth.register_token_verifier("slack", oauth._slack_token_label)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


@pytest.fixture
def server(env):
    import ops_dashboard.server as server_mod
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.DashboardHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def _post(port: int, path: str, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


def test_a_good_token_connects_the_connector(server, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "user": "ana"})

    oauth.set_transport(httpx.MockTransport(handler))

    status, data = _post(server, "/api/connectors/slack/connect-token", {"token": "xoxb-good"})
    assert status == 200, data
    assert data["ok"] is True
    rec = tokens.load_token("slack")
    assert rec is not None and rec["access_token"] == "xoxb-good"
    assert rec.get("auth_method") == "token"


def test_the_token_is_not_left_in_the_database(server, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "user": "ana"})

    oauth.set_transport(httpx.MockTransport(handler))
    _post(server, "/api/connectors/slack/connect-token", {"token": "xoxb-secret-value"})

    with dbmod.db() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            except Exception:
                continue
            for row in rows:
                for value in tuple(row):
                    if isinstance(value, str):
                        assert "xoxb-secret-value" not in value, f"the token is in {table}"


def test_a_bad_token_is_refused_and_nothing_is_stored(server, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": "invalid_auth"})

    oauth.set_transport(httpx.MockTransport(handler))
    status, data = _post(server, "/api/connectors/slack/connect-token", {"token": "xoxb-bad"})
    assert status >= 400, data
    assert tokens.load_token("slack") is None, "a rejected token was stored"


def test_an_empty_token_is_refused(server):
    status, data = _post(server, "/api/connectors/slack/connect-token", {"token": ""})
    assert status >= 400
    assert data.get("error") == "bad_token"


def test_a_connector_that_does_not_accept_a_token_is_refused(server):
    registry.register(registry.ConnectorSpec(
        id="nope", name="Nope", auth="oauth_pkce",
        allowed_hosts=("api.example.test",),
        authorize_url="https://api.example.test/authorize",
        token_url="https://api.example.test/token"))
    status, data = _post(server, "/api/connectors/nope/connect-token", {"token": "tok"})
    assert status >= 400
    assert data.get("error") == "token_not_supported"


def test_the_route_is_local_only():
    """The endpoint describes the owner's own connector configuration."""
    from ops_dashboard import routes_connectors as routes
    src = open(routes.__file__, encoding="utf-8").read()
    block = src.split("/api/connectors/<connector_id>/connect-token")[1].split("\n@router")[0]
    assert "_guarded" in block
