"""Conectores page + /api/connectors/* over a real in-process server on
127.0.0.1: same-origin guard, CSP, OAuth via the loopback receiver, and the
confirm-before-write flow. No network: the provider is ``connectors_fake``."""
from __future__ import annotations

import json
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from connectors_fake import FakeProvider, fake_spec

from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_tokens as tokens

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "routes.db")
    db.init_db()
    prev_b = tokens.use_backend(tokens.MemoryBackend())
    p = FakeProvider()
    prev_t = oauth.set_transport(p.transport())
    spec = registry.register(fake_spec())
    try:
        yield p
    finally:
        registry.unregister(spec.id)
        oauth.set_transport(prev_t)
        tokens.use_backend(prev_b)


@pytest.fixture
def server(env):
    from ops_dashboard.server import DashboardHandler

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", env
    finally:
        srv.shutdown()
        srv.server_close()


def _req(url, method="GET", body=None, headers=None, ctype="application/json"):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": ctype} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _json(t):
    return json.loads(t[2] or b"{}")


def test_page_has_strict_csp_and_routes_registered(server):
    base, _p = server
    with urllib.request.urlopen(base + "/connectors", timeout=10) as r:
        st, body = r.status, r.read()
        policies = r.headers.get_all("Content-Security-Policy") or []
    assert st == 200 and b'id="connectors-list"' in body
    # The server may add its own baseline CSP to every HTML response (#43);
    # browsers enforce every CSP header, so it is enough that one is strict.
    strict = ("default-src 'self'", "script-src 'self'", "object-src 'none'", "frame-ancestors 'none'",
              "form-action 'none'", "base-uri 'none'")
    assert any(all(d in csp for d in strict) for csp in policies), policies
    assert not any("unsafe-inline" in csp or "unsafe-eval" in csp for csp in policies), policies
    from ops_dashboard.routing import router
    reg = router.registered()
    for m, p in (("GET", "/connectors"), ("GET", "/api/connectors"), ("POST", "/api/connectors/<connector_id>/connect"),
                 ("POST", "/api/connectors/actions/<action_id>/confirm"), ("GET", "/api/connectors/audit")):
        assert (m, p) in reg


@pytest.mark.parametrize("headers,ctype,status,code", [
    ({"Origin": "https://evil.example"}, "application/json", 403, "cross_origin"),
    ({"Origin": "null"}, "application/json", 403, "cross_origin"),
    ({"Host": "evil.example"}, "application/json", 421, "invalid_host"),
    # Remote (relay/proxy) requests: the server's global POST gate answers 401
    # before routing; GETs reach the connectors guard (403 local_only).
    ({"X-Forwarded-For": "8.8.8.8"}, "application/json", 401, None),
    ({"X-Relay": "1"}, "application/json", 401, None),
    ({"Sec-Fetch-Site": "cross-site"}, "application/json", 403, "cross_site"),
    ({}, "text/plain", 415, "json_required"),
    ({}, "application/x-www-form-urlencoded", 415, "json_required"),
])
def test_guard_refuses_cross_origin_and_remote(server, headers, ctype, status, code):
    base, _p = server
    st, _h, body = _req(base + "/api/connectors/fakechat/client", "POST", {"client_id": "x"}, headers, ctype)
    assert st == status
    if code:
        # With #24 merged the server's global CORS/CSRF gate refuses first
        # ("forbidden_origin"); either way the request never reaches a handler.
        assert json.loads(body)["error"] in (code, "forbidden_origin")


@pytest.mark.parametrize("hdr", [{"X-Forwarded-For": "8.8.8.8"}, {"X-Relay": "1"}])
def test_remote_get_is_local_only(server, hdr):
    base, _p = server
    # Unauthenticated: the server's global gate answers 401 first.
    assert _req(base + "/api/connectors", headers=hdr)[0] == 401
    assert _req(base + "/connectors", headers=hdr)[0] == 401


def test_guard_is_local_only_even_for_authenticated_remote():
    """A paired device with a valid session still gets 403 from the guard."""
    from ops_dashboard import routes_connectors as rc

    class H:
        command = "GET"
        headers = {"Host": "127.0.0.1:1"}

        def _is_local(self):
            return False
    assert rc.guard_failure(H()) == (403, "local_only")


def test_guard_allows_same_origin_browser_request(server):
    base, _p = server
    host = base.split("//")[1]
    st, _h, body = _req(base + "/api/connectors", headers={"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"})
    assert st == 200 and json.loads(body)["keychain"] is True


def test_end_to_end_connect_write_confirm(server):
    base, provider = server
    st, _h, body = _req(base + "/api/connectors")
    c = json.loads(body)["connectors"][0]
    assert c["id"] == "fakechat" and c["status"] == "disconnected" and c["client_id_set"] is False

    r = _json(_req(base + "/api/connectors/fakechat/connect", "POST", {"permission": "write"}))
    assert r["error"] == "client_id_required"
    assert _json(_req(base + "/api/connectors/fakechat/client", "POST", {"client_id": "my-cid"}))["ok"]

    r = _json(_req(base + "/api/connectors/fakechat/connect", "POST", {"permission": "write"}))
    assert r["authorize_url"].startswith("https://auth.fake.test/authorize?")
    cb = provider.authorize(r["authorize_url"])
    with urllib.request.urlopen(f"{cb['redirect_uri']}?state={cb['state']}&code={cb['code']}", timeout=5) as resp:
        assert resp.status == 200
    flow = _json(_req(base + "/api/connectors/flows/" + r["state"]))
    assert flow["status"] == "connected" and flow["permission"] == "write"

    from ops_dashboard import connector_actions as actions
    q = actions.invoke("fakechat", "post_message", {"channel": "ops", "text": "deploy ok"})
    assert q["status"] == "confirmation_required" and provider.messages == []
    pend = _json(_req(base + "/api/connectors/actions"))["actions"]
    assert [a["id"] for a in pend] == [q["action_id"]] and pend[0]["payload"]["text"] == "deploy ok"

    bad = _req(base + f"/api/connectors/actions/{q['action_id']}/confirm", "POST", {"payload_hash": "0" * 64})
    assert bad[0] == 409 and provider.messages == []
    cross = _req(base + f"/api/connectors/actions/{q['action_id']}/confirm", "POST",
                 {"payload_hash": q["payload_hash"]}, {"Origin": "https://evil.example"})
    assert cross[0] == 403 and provider.messages == []
    ok = _json(_req(base + f"/api/connectors/actions/{q['action_id']}/confirm", "POST",
                    {"payload_hash": q["payload_hash"]}))
    assert ok["status"] == "done" and provider.messages == [{"channel": "ops", "text": "deploy ok"}]

    assert _json(_req(base + "/api/connectors/fakechat/permission", "POST", {"permission": "write"}))["error"] \
        == "reconnect_required"
    acc = _json(_req(base + "/api/connectors/fakechat/permission", "POST", {"permission": "read"}))["connector"]
    assert acc["permission"] == "read"
    acc = _json(_req(base + "/api/connectors/fakechat/disconnect", "POST", {}))["connector"]
    assert acc["status"] == "disconnected"
    events = [e["event"] for e in _json(_req(base + "/api/connectors/audit"))["events"]]
    for ev in ("connect_started", "connected", "write_requested", "write_confirmed", "write_done",
               "permission_changed", "disconnected", "client_saved"):
        assert ev in events
    assert "deploy ok" not in json.dumps(_json(_req(base + "/api/connectors/audit")))


def test_unknown_connector_and_bad_ids(server):
    base, _p = server
    assert _req(base + "/api/connectors/nope/connect", "POST", {})[0] == 404
    assert _req(base + "/api/connectors/actions/abc/confirm", "POST", {})[0] == 404
    assert _req(base + "/api/connectors/fakechat/client", "POST", {"client_id": "bad id with spaces"})[0] == 400


# ── static UI rules ─────────────────────────────────────────────────────────

def test_ui_static_rules():
    js = re.sub(r"(?m)^\s*//.*$", "", (WEB / "connectors.js").read_text(encoding="utf-8"))
    html = (WEB / "connectors.html").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                   "setTimeout('", 'setTimeout("'):
        assert banned not in js, banned
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script>"
    assert " style=" not in html and not re.search(r"\son[a-z]+=", html)
    assert "rel = 'noopener noreferrer'" in js and "^https:" in js


def test_ui_es_en_parity_and_html_keys():
    js = (WEB / "connectors.js").read_text(encoding="utf-8")
    html = (WEB / "connectors.html").read_text(encoding="utf-8")
    es = js[js.index("es: {"):js.index("en: {")]
    en = js[js.index("en: {"):js.index("var T =")]
    key = re.compile(r"^\s*([a-z_]+):|,\s*([a-z_]+):", re.M)

    def keys(block):
        return {a or b for a, b in key.findall(block)} - {"es", "en"}
    assert keys(es) == keys(en) and len(keys(es)) > 30
    for k in re.findall(r'data-i18n="([a-z_]+)"', html):
        assert k in keys(es), k
