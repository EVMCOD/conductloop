"""End-to-end tests through the real HTTP server — the new feature endpoints
exercised over the wire (not just 'no 500'). Boots the in-process handler
against a throwaway DB + tmp vault and drives capture / recall / analytics /
setup / obsidian / voice with real requests.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    import os
    os.environ["CONDUCTLOOP_DB_PATH"] = str(tmp / "e2e.db")
    os.environ["OPS_VAULT_ROOT"] = str(tmp / "vault")
    os.environ["OPS_DISABLE_KEYCHAIN"] = "1"
    os.environ["OPS_SECRETS_DIR"] = str(tmp / "secrets")
    (tmp / "vault").mkdir()

    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler
    db.DB_PATH = tmp / "e2e.db"  # type: ignore[attr-defined]
    db.init_db()

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()


def _get(url):
    with urllib.request.urlopen(url, timeout=8) as r:
        return r.status, json.loads(r.read())


def _post(url, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_capture_roundtrip_over_http(base):
    status, body = _post(base + "/api/capture", {"content": "buy milk", "kind": "task"})
    assert status == 200 and body["ok"] and body["id"] > 0
    cid = body["id"]
    status, listing = _get(base + "/api/capture")
    assert any(c["id"] == cid for c in listing["captures"])
    status, done = _post(base + f"/api/capture/{cid}/process", {})
    assert done["ok"] is True
    _, after = _get(base + "/api/capture")
    assert not any(c["id"] == cid for c in after["captures"])


def test_analytics_and_setup_over_http(base):
    status, a = _get(base + "/api/brain/analytics")
    assert status == 200 and "llm" in a and "spend" in a
    status, chk = _get(base + "/api/setup/checklist")
    assert status == 200 and chk["total"] == 9


def test_memory_recall_over_http(base):
    from ops_dashboard import db
    db.memory_set("db_fact", "the engine uses sqlite for storage")
    status, r = _get(base + "/api/memory/recall?q=engine%20uses%20sqlite")
    assert status == 200
    assert any(m["key"] == "db_fact" for m in r["results"])


def test_obsidian_export_over_http(base):
    from ops_dashboard import db
    db.memory_set("k", "v")
    status, r = _post(base + "/api/obsidian/export", {})
    assert status == 200 and r["ok"] and r["written"] >= 1


def test_voice_autofile_over_http(base):
    status, r = _post(base + "/api/voice/autofile", {"transcript": "remember that the api is local-first"})
    assert status == 200 and r["ok"] and r["kind"] == "memory"
