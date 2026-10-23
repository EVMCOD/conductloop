"""/api/connectors/mcp/* over the real server on 127.0.0.1 with the local
stdio fake MCP server. No network."""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from mcp_fake import STDIO_CMD

from ops_dashboard import connector_tokens as tokens


@pytest.fixture
def server(tmp_path, monkeypatch):
    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "r.db")
    db.init_db()
    kc = tokens.MemoryBackend()
    prev = tokens.use_backend(kc)

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
        yield f"http://127.0.0.1:{port}", kc, tmp_path
    finally:
        srv.shutdown()
        srv.server_close()
        tokens.use_backend(prev)
        from ops_dashboard import connector_registry as registry
        for sp in registry.all_specs():
            if sp.id.startswith("mcp-"):
                registry.unregister(sp.id)


def _req(url, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data,
                                 method=method or ("POST" if data is not None else "GET"),
                                 headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_mcp_routes_end_to_end(server):
    base, kc, tmp = server
    out = tmp / "side.txt"
    st, b = _req(base + "/api/connectors/mcp/servers", {
        "name": "notes", "transport": "stdio", "command": STDIO_CMD, "env": {"FAKE_MCP_OUT": str(out)},
        "secret_env": {"FAKE_API_TOKEN": "child-only-secret"}})
    assert st == 200 and b["server"]["secret_vars"] == [{"name": "FAKE_API_TOKEN", "set": True}]
    assert "child-only-secret" not in json.dumps(b)
    st, b = _req(base + "/api/connectors/mcp/servers", {"name": "x", "transport": "stdio", "command": "a",
                                                        "env": {"OPENAI_API_KEY": "sk-x"}})
    assert st == 400 and b["error"] == "secret_in_env"

    # The user trusts this server: until they do, its tools are all writes
    # and need the approval gate (2026-10-23).
    assert _req(base + "/api/mcp/servers/notes", {"trust_readonly": True}, method="PUT")[0] == 200
    st, b = _req(base + "/api/connectors/mcp/servers/notes/refresh", {})
    assert st == 200 and b["server"]["status"] == "connected"
    listing = _req(base + "/api/connectors/mcp")[1]
    assert listing["keychain"] is True and listing["servers"][0]["name"] == "notes"
    assert "child-only-secret" not in json.dumps(listing)
    assert all(c["auth"] != "mcp" for c in _req(base + "/api/connectors")[1]["connectors"])

    st, b = _req(base + "/api/connectors/mcp/servers/notes/tools/search_notes/call", {"arguments": {"q": "Pro"}})
    assert st == 200 and b["result"]["untrusted"] is True
    st, b = _req(base + "/api/connectors/mcp/servers/notes/tools/create_note/call", {"arguments": {"title": "t"}})
    assert st == 403 and b["error"] == "write_not_permitted"
    assert _req(base + "/api/connectors/mcp/servers/notes/permission", {"permission": "write"})[0] == 200
    st, q = _req(base + "/api/connectors/mcp/servers/notes/tools/create_note/call", {"arguments": {"title": "t"}})
    assert q["status"] == "confirmation_required" and not out.exists()
    pend = _req(base + "/api/connectors/actions")[1]["actions"]
    assert pend[0]["connector_id"] == "mcp-notes" and pend[0]["action"] == "tool:create_note"
    cross = _req(base + f"/api/connectors/actions/{q['action_id']}/confirm", {"payload_hash": q["payload_hash"]},
                 {"Origin": "https://evil.example"})
    assert cross[0] == 403 and not out.exists()
    st, b = _req(base + f"/api/connectors/actions/{q['action_id']}/confirm", {"payload_hash": q["payload_hash"]})
    assert st == 200 and out.exists()

    st, b = _req(base + "/api/connectors/mcp/servers/notes/tools/inject/mode", {"mode": "disabled"})
    assert st == 200 and next(t for t in b["server"]["tools"] if t["name"] == "inject")["kind"] is None
    events = [e["event"] for e in _req(base + "/api/connectors/audit")[1]["events"]]
    for ev in ("mcp_added", "mcp_secret_saved", "mcp_connected", "write_requested", "write_done", "mcp_tool_mode"):
        assert ev in events
    assert _req(base + "/api/connectors/mcp/servers/notes/remove", {})[0] == 200
    assert kc.items == {} and _req(base + "/api/connectors/mcp")[1]["servers"] == []


def test_mcp_routes_guard(server):
    base, _kc, _tmp = server
    st, b = _req(base + "/api/connectors/mcp/servers", {"name": "x", "transport": "stdio", "command": "a"},
                 {"Origin": "https://evil.example"})
    assert st == 403
    st, b = _req(base + "/api/connectors/mcp", headers={"Host": "evil.example"})
    assert st == 421
    assert _req(base + "/api/connectors/mcp/servers/nope/refresh", {})[0] == 404


def test_legacy_delete_goes_through_the_hub(server):
    # The old dashboard view still deletes via DELETE /api/mcp/servers/<name>:
    # that must also remove keychain secrets, not only the row (Codex on #46).
    from mcp_fake import STDIO_CMD

    from ops_dashboard import mcp_hub
    base, kc, _tmp = server
    mcp_hub.save_server("legacy", "stdio", command=STDIO_CMD, secret_env={"FAKE_API_TOKEN": "s"})
    assert kc.items
    req = urllib.request.Request(base + "/api/mcp/servers/legacy", method="DELETE")
    with urllib.request.urlopen(req, timeout=15) as r:
        assert r.status == 200
    assert kc.items == {} and mcp_hub.list_servers() == []
    req = urllib.request.Request(base + "/api/mcp/servers/legacy", method="DELETE")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=15)
    assert e.value.code == 404
