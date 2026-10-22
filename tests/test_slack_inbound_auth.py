"""Slack inbound (Events API) fails closed: with no SLACK_SIGNING_SECRET every
request is rejected; with one, only correctly signed, fresh requests pass.
No network: in-process server on 127.0.0.1, synthetic secrets only."""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import slack_bridge as sb

SECRET = "test-signing-secret-not-real"


def _sign(body: bytes, ts: int, secret: str = SECRET) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:%d:" % ts + body, hashlib.sha256).hexdigest()


@pytest.fixture
def no_secret(monkeypatch):
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.setattr(sb, "WEBHOOK_SECRET", "")


@pytest.fixture
def with_secret(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setattr(sb, "WEBHOOK_SECRET", "")


def test_no_signing_secret_rejects_everything(no_secret):
    body = b'{"type":"event_callback"}'
    ts = int(time.time())
    assert sb.authorize_request(body=body, timestamp=str(ts), signature=_sign(body, ts)) == \
        (False, "signing_secret_not_configured")
    assert sb.verify_signature(timestamp=str(ts), body=body, signature=_sign(body, ts)) is False
    assert sb._authorized(None) is False and sb._authorized("anything") is False
    assert sb.inbound_enabled() is False and sb.status()["signing_secret_configured"] is False


def test_legacy_query_secret_alone_is_not_enough(no_secret, monkeypatch):
    monkeypatch.setattr(sb, "WEBHOOK_SECRET", "legacy")
    assert sb._authorized("legacy") is False


def test_signed_requests(with_secret, monkeypatch):
    body = b'{"type":"event_callback","event":{}}'
    now = 1_800_000_000
    ok = sb.authorize_request(body=body, timestamp=str(now), signature=_sign(body, now), now=now)
    assert ok == (True, "ok")
    cases = {
        "bad_signature": dict(body=body, timestamp=str(now), signature=_sign(body + b" ", now)),
        "bad_signature_wrong_key": dict(body=body, timestamp=str(now), signature=_sign(body, now, "other")),
        "stale": dict(body=body, timestamp=str(now - 301), signature=_sign(body, now - 301)),
        "no_ts": dict(body=body, timestamp=None, signature=_sign(body, now)),
        "no_sig": dict(body=body, timestamp=str(now), signature=None),
    }
    for name, kw in cases.items():
        assert sb.authorize_request(now=now, **kw)[0] is False, name
    monkeypatch.setattr(sb, "WEBHOOK_SECRET", "legacy")
    assert sb.authorize_request(body=body, timestamp=str(now), signature=_sign(body, now), now=now,
                                query_secret="nope") == (False, "bad_webhook_secret")
    assert sb.authorize_request(body=body, timestamp=str(now), signature=_sign(body, now), now=now,
                                query_secret="legacy") == (True, "ok")


@pytest.fixture
def server(tmp_path, monkeypatch):
    from ops_dashboard import ceo_channels, db
    from ops_dashboard.server import DashboardHandler
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "s.db")
    db.init_db()
    routed: list = []
    monkeypatch.setattr(ceo_channels, "route_inbound", lambda *a: routed.append(a) or {"ok": True, "routed": 1})

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/api/slack/webhook", routed
    finally:
        srv.shutdown()
        srv.server_close()


def _post(url, body: bytes, headers: dict):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


EVENT = json.dumps({"type": "event_callback",
                    "event": {"type": "message", "channel": "C1", "user": "U1", "text": "hola"}}).encode()


def test_http_rejects_all_without_secret_even_from_loopback(server, no_secret):
    url, routed = server
    ts = int(time.time())
    st, body = _post(url, EVENT, {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": _sign(EVENT, ts)})
    assert st == 503 and body["error"] == "signing_secret_not_configured" and routed == []
    st2, _ = _post(url, EVENT, {})
    assert st2 == 503 and routed == []


def test_http_accepts_only_valid_signature(server, with_secret):
    url, routed = server
    ts = int(time.time())
    st, body = _post(url, EVENT, {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": "v0=" + "0" * 64})
    assert st == 401 and body["error"] == "bad_signature" and routed == []
    st, body = _post(url, EVENT, {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": _sign(EVENT, ts)})
    assert st == 200 and routed == [("slack", "C1", "hola", "U1")]
    chal = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    st, body = _post(url, chal, {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": _sign(chal, ts)})
    assert st == 200 and body == {"challenge": "abc"}
