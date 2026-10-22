"""MCP client (stdio + Streamable HTTP), server list, tool policy, keychain
secrets, confirm-before-write and untrusted results.

No network: the stdio server is a local Python script (tests/fixtures/mcp),
the HTTP server is an httpx.MockTransport fake; socket.connect is limited to
loopback.
"""
from __future__ import annotations

import json
import os
import socket

import pytest
from mcp_fake import HTTP_URL, STDIO_CMD, TOKEN, FakeHttpMCP

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import mcp, mcp_client, mcp_hub


@pytest.fixture(autouse=True)
def _no_external_network():
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"network access in tests: {address!r}")
        return real(self, address)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", guarded)
        yield


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mcp.db")
    db.init_db()
    kc = tokens.MemoryBackend()
    prev = tokens.use_backend(kc)
    try:
        yield kc, tmp_path
    finally:
        tokens.use_backend(prev)
        from ops_dashboard import connector_registry as registry
        for s in registry.all_specs():
            if s.id.startswith("mcp-"):
                registry.unregister(s.id)


@pytest.fixture
def http_fake():
    f = FakeHttpMCP()
    prev = mcp_client.set_http_transport(f.transport())
    try:
        yield f
    finally:
        mcp_client.set_http_transport(prev)


def _add_stdio(tmp, **kw):
    out = tmp / "side.txt"
    mcp_hub.save_server("notes", "stdio", command=STDIO_CMD, env={"FAKE_MCP_OUT": str(out)},
                        secret_env={"FAKE_API_TOKEN": "child-only-secret"}, **kw)
    return out


# ── client / transports ─────────────────────────────────────────────────────

def test_safe_env_drops_parent_secrets():
    base = {"PATH": "/usr/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "sk-ant-x", "GITHUB_TOKEN": "ghp_x",
            "AWS_SECRET_ACCESS_KEY": "a", "SLACK_SIGNING_SECRET": "s", "LANG": "es_ES.UTF-8"}
    env = mcp_client.safe_env({"FOO": "1"}, base=base)
    assert env == {"PATH": "/usr/bin", "HOME": "/h", "LANG": "es_ES.UTF-8", "FOO": "1"}


def test_http_url_rules():
    assert mcp_client.check_http_url("https://x.test/mcp")
    assert mcp_client.check_http_url("http://127.0.0.1:9000/mcp")
    for bad in ("http://example.test/mcp", "ftp://x/", "file:///etc/passwd", ""):
        with pytest.raises(mcp_client.MCPError):
            mcp_client.check_http_url(bad)


def test_stdio_session_handshake_pagination_and_server_requests():
    with mcp_client.open_session({"transport": "stdio", "command": STDIO_CMD}, env={}) as s:
        assert s.server_info["name"] == "fake-mcp" and s.protocol_version == "2025-06-18"
        names = [t["name"] for t in s.list_tools()]                 # 2 pages + refused sampling request
        assert names == ["search_notes", "create_note", "delete_all", "env_probe", "inject", "bad name!"]
        res = s.call_tool("search_notes", {"q": "Pro"})
        assert "plan Pro" in res["content"][0]["text"]
        with pytest.raises(mcp_client.MCPError) as e:
            s.call_tool("nope", {})
        assert e.value.code == "rpc_error"


def test_stdio_errors_are_reported():
    with pytest.raises(mcp_client.MCPError) as e:
        mcp_client.open_session({"transport": "stdio", "command": ["definitely-not-a-binary-xyz"]})
    assert e.value.code == "command_not_found"
    with pytest.raises(mcp_client.MCPError) as e2:
        with mcp_client.open_session({"transport": "stdio", "command": [STDIO_CMD[0], "-c", "import sys; sys.exit(3)"]},
                                     timeout=5):
            pass
    assert e2.value.code == "server_exited"
    with pytest.raises(mcp_client.MCPError) as e3:
        mcp_client.open_session({"transport": "sse", "url": "https://x.test"})
    assert e3.value.code == "unsupported_transport"


def test_streamable_http_session(http_fake):
    with mcp_client.open_session({"transport": "http", "url": HTTP_URL}, token=TOKEN) as s:
        assert s.server_info["name"] == "fake-http-mcp"
        assert [t["name"] for t in s.list_tools()] == ["list_issues", "create_issue"]
        res = s.call_tool("list_issues", {})                         # SSE response with progress first
        assert res["content"][0]["text"] == "list_issues ok"
    assert http_fake.deleted == ["sess-1"]                           # session closed with DELETE
    later = [r for r in http_fake.requests if r["msg"].get("method") != "initialize"]
    assert all(r["headers"].get("mcp-session-id") == "sess-1" for r in later)
    with pytest.raises(mcp_client.MCPError) as e:
        mcp_client.open_session({"transport": "http", "url": HTTP_URL}, token="wrong").__enter__()
    assert e.value.code == "unauthorized"


# ── hub: server list, status, log ───────────────────────────────────────────

def test_add_server_secrets_only_in_keychain(env):
    kc, tmp = env
    _add_stdio(tmp)
    v = mcp_hub.server_view("notes")
    assert v["status"] == "disconnected" and v["permission"] == "read"
    assert v["secret_vars"] == [{"name": "FAKE_API_TOKEN", "set": True}]
    assert kc.items == {"mcp-env:notes:FAKE_API_TOKEN": "child-only-secret"}
    from ops_dashboard import db
    raw = db.DB_PATH.read_bytes() + b"".join(p.read_bytes() for p in db.DB_PATH.parent.glob("mcp.db-*"))
    assert b"child-only-secret" not in raw
    with pytest.raises(actions.ActionError) as e:
        mcp_hub.save_server("x", "stdio", command="echo hi", env={"GITHUB_TOKEN": "ghp_x"})
    assert e.value.code == "secret_in_env"
    for bad in [dict(name="Bad Name", transport="stdio", command="x"), dict(name="x", transport="sse", url="https://a"),
                dict(name="x", transport="http", url="http://evil.test/mcp"), dict(name="x", transport="stdio", command="")]:
        with pytest.raises(actions.ActionError):
            mcp_hub.save_server(**bad)


def test_keychain_unavailable_refuses_secrets(env):
    class NoKC(tokens.MemoryBackend):
        def available(self):
            return False
    prev = tokens.use_backend(NoKC())
    try:
        with pytest.raises(actions.ActionError) as e:
            mcp_hub.save_server("h", "http", url=HTTP_URL, token=TOKEN)
        assert e.value.code == "keychain_unavailable"
    finally:
        tokens.use_backend(prev)


def test_refresh_connects_lists_and_logs(env):
    kc, tmp = env
    _add_stdio(tmp)
    v = mcp_hub.refresh("notes")
    assert v["status"] == "connected" and v["server_label"] == "fake-mcp 1.0"
    kinds = {t["name"]: t["kind"] for t in v["tools"]}
    assert kinds == {"search_notes": "read", "create_note": "write", "delete_all": "write",
                     "env_probe": "read", "inject": "read"}                 # "bad name!" rejected
    assert "\u200b" not in next(t for t in v["tools"] if t["name"] == "inject")["description"]
    log = store.list_audit(connector_id="mcp-notes")
    assert [e["event"] for e in log][:1] == ["mcp_connected"]
    assert log[0]["detail"]["rejected_tools"] == ["bad name!"]
    assert "child-only-secret" not in json.dumps(store.list_audit())


def test_refresh_failure_sets_error_and_logs(env):
    mcp_hub.save_server("broken", "stdio", command=["definitely-not-a-binary-xyz"])
    with pytest.raises(actions.ActionError):
        mcp_hub.refresh("broken")
    v = mcp_hub.server_view("broken")
    assert v["status"] == "error" and v["last_error"] == "command_not_found"
    assert store.list_audit(connector_id="mcp-broken")[0]["event"] == "mcp_connect_failed"


def test_stdio_child_gets_no_parent_secrets(env, monkeypatch):
    kc, tmp = env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_parent")
    _add_stdio(tmp)
    mcp_hub.refresh("notes")
    res = mcp_hub.invoke("notes", "env_probe")
    keys = json.loads(res["result"]["text"])
    assert "ANTHROPIC_API_KEY" not in keys and "GITHUB_TOKEN" not in keys
    assert "FAKE_API_TOKEN" in keys and "FAKE_MCP_OUT" in keys            # only its own configured vars
    assert set(keys) <= set(mcp_client.SAFE_ENV_KEYS) | {"FAKE_API_TOKEN", "FAKE_MCP_OUT"} | {
        "LC_CTYPE", "PYTHONIOENCODING", "__CF_USER_TEXT_ENCODING"}


# ── read-only by default + confirmation of writes (#40) ─────────────────────

def test_read_tool_runs_and_result_is_untrusted(env):
    kc, tmp = env
    _add_stdio(tmp)
    mcp_hub.refresh("notes")
    res = mcp_hub.invoke("notes", "search_notes", {"q": "Pro"})
    assert res["status"] == "done" and res["result"]["untrusted"] is True
    assert res["result"]["source"] == "mcp:notes/search_notes" and "plan Pro" in res["result"]["text"]


def test_write_tools_blocked_while_read_only_then_queued(env):
    kc, tmp = env
    out = _add_stdio(tmp)
    mcp_hub.refresh("notes")
    for tool in ("create_note", "delete_all"):
        with pytest.raises(actions.ActionError) as e:
            mcp_hub.invoke("notes", tool, {"title": "x"})
        assert e.value.code == "write_not_permitted"
    assert not out.exists()
    mcp_hub.set_permission("notes", "write")
    q = mcp_hub.invoke("notes", "create_note", {"title": "Hola"})
    assert q["status"] == "confirmation_required" and not out.exists()
    assert q["summary"].startswith("MCP notes · create_note(")
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], "0" * 64)
    assert not out.exists()
    done = actions.confirm(q["action_id"], q["payload_hash"])
    assert done["status"] == "done" and out.read_text().strip() == '{"title": "Hola"}'
    assert done["result"]["untrusted"] is True
    d = mcp_hub.invoke("notes", "delete_all")                          # destructiveHint wins over readOnlyHint
    assert d["status"] == "confirmation_required"
    mcp_hub.set_permission("notes", "read")                            # downgrade expires the pending one
    with pytest.raises(actions.ActionError):
        actions.confirm(d["action_id"], d["payload_hash"])
    assert "DELETE_ALL" not in out.read_text()


def test_tool_modes_and_rug_pull(env, monkeypatch):
    kc, tmp = env
    _add_stdio(tmp)
    mcp_hub.refresh("notes")
    mcp_hub.set_tool_mode("notes", "create_note", "read")              # user decision
    mcp_hub.set_tool_mode("notes", "inject", "disabled")
    with pytest.raises(actions.ActionError) as e:
        mcp_hub.invoke("notes", "inject")
    assert e.value.code == "unknown_action"
    mcp_hub.set_tool_mode("notes", "search_notes", "read")
    with pytest.raises(actions.ActionError):
        mcp_hub.set_tool_mode("notes", "search_notes", "admin")
    # server changes search_notes' description after review → flagged, override dropped
    mcp_hub.save_server("notes", "stdio", command=STDIO_CMD,
                        env={"FAKE_MCP_OUT": str(tmp / "side.txt"), "FAKE_MCP_MUTATE": "1"})
    v = mcp_hub.refresh("notes")
    t = next(x for x in v["tools"] if x["name"] == "search_notes")
    assert t["changed"] is True and t["mode"] == "auto"
    assert store.list_audit(connector_id="mcp-notes")[0]["event"] == "mcp_tools_changed"


def test_untrusted_result_and_fence_resist_injection(env):
    kc, tmp = env
    _add_stdio(tmp)
    mcp_hub.refresh("notes")
    r = mcp_hub.invoke("notes", "inject")["result"]
    assert r["dropped_blocks"] == 1 and "\u202e" not in r["text"] and "\u200b" not in r["text"] and "\x07" not in r["text"]
    fenced = mcp_hub.fence_for_llm(r)
    assert fenced.startswith('<untrusted_tool_output source="mcp:notes/inject">')
    assert fenced.count("</untrusted_tool_output>") == 1 and fenced.endswith("</untrusted_tool_output>")
    assert "&lt;/untrusted_tool_output>" in fenced and "not an instruction" in fenced


def test_http_server_token_in_keychain_and_calls(env, http_fake):
    kc, tmp = env
    mcp_hub.save_server("issues", "http", url=HTTP_URL, token=TOKEN)
    assert kc.items == {"mcp-token:issues": TOKEN}
    v = mcp_hub.refresh("issues")
    assert v["status"] == "connected" and v["token_set"] is True and "token" not in json.dumps(v["tools"])
    assert mcp_hub.invoke("issues", "list_issues")["result"]["text"] == "list_issues ok"
    mcp_hub.set_permission("issues", "write")
    q = mcp_hub.invoke("issues", "create_issue", {"title": "bug"})
    assert q["status"] == "confirmation_required" and http_fake.created == []
    actions.confirm(q["action_id"], q["payload_hash"])
    assert http_fake.created == [{"title": "bug"}]
    kc.items.clear()                                                     # token gone from keychain
    with pytest.raises(actions.ActionError) as e:
        mcp_hub.refresh("issues")
    assert e.value.code == "secret_missing"


def test_remove_server_cleans_keychain_and_pending(env):
    kc, tmp = env
    _add_stdio(tmp)
    mcp_hub.refresh("notes")
    mcp_hub.set_permission("notes", "write")
    q = mcp_hub.invoke("notes", "create_note", {"title": "x"})
    mcp_hub.remove_server("notes")
    assert kc.items == {} and mcp_hub.list_servers() == []
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], q["payload_hash"])
    assert store.list_audit(connector_id="mcp-notes")[0]["event"] == "mcp_removed"


def test_legacy_mcp_module_delegates(env, tmp_path):
    tools, err = mcp.discover_tools("legacy", "stdio", command=STDIO_CMD, env={})
    assert err is None and [t["name"] for t in tools][:2] == ["search_notes", "create_note"]
    assert mcp.discover_tools("legacy", "sse", url="https://x.test")[1].startswith("unsupported_transport")
    kc, tmp = env
    _add_stdio(tmp)
    mcp_hub.refresh("notes")
    res, err = mcp.call_tool({"name": "notes"}, "search_notes", {"q": "x"})
    assert err is None and res["untrusted"] is True
    mcp_hub.set_permission("notes", "write")
    res, err = mcp.call_tool({"name": "notes"}, "create_note", {"title": "x"})
    assert res is None and err.startswith("confirmation_required:")


def test_no_outbound_destinations_in_source():
    import re
    from pathlib import Path
    root = Path(mcp_hub.__file__).parent
    for f in ("mcp_client.py", "mcp_hub.py", "mcp.py", "routes_mcp.py"):
        src = (root / f).read_text(encoding="utf-8")
        for url in re.findall(r"https?://[^\s\"'`)]+", src):
            assert url.startswith(("http://127.0.0.1", "http://localhost")), (f, url)
        assert "os.environ)" not in src and "dict(os.environ" not in src, f
    assert os.environ.get("CONDUCTLOOP_EVAL_PARENT_SECRET") is None


def test_legacy_plain_env_row_never_echoes_values(env):
    from ops_dashboard import db
    with db.db() as conn:
        conn.execute("INSERT INTO mcp_servers (name, transport, command, env, created_at, updated_at)"
                     " VALUES (?, 'stdio', ?, ?, 1, 1)",
                     ("old", json.dumps(["true"]), json.dumps({"GITHUB_TOKEN": "ghp_legacy_secret"})))
    view = mcp_hub.server_view("old")
    assert view["legacy_env"] is True and view["env"] == {"GITHUB_TOKEN": "…"}
    assert "ghp_legacy_secret" not in json.dumps(mcp_hub.list_servers())


def test_http_bodies_are_bounded_while_streaming():
    import itertools

    import pytest

    from ops_dashboard import mcp_client

    pulled = []

    class Endless:                     # a server that never stops sending
        def iter_bytes(self):
            for i in itertools.count():
                pulled.append(i)
                yield b"x" * 65536      # no newline: iter_lines would buffer it all

    with pytest.raises(mcp_client.MCPError, match="exceeds limit"):
        b"".join(mcp_client._bounded_chunks(Endless()))
    assert len(pulled) * 65536 <= mcp_client.MAX_MESSAGE + 65536
    pulled.clear()
    with pytest.raises(mcp_client.MCPError, match="exceeds limit"):
        list(mcp_client._bounded_lines(Endless()))
    assert len(pulled) * 65536 <= mcp_client.MAX_MESSAGE + 65536

    class Small:
        def iter_bytes(self):
            yield b"event: message\r\ndata: {\"a\""
            yield b": 1}\n\ntail"

    assert list(mcp_client._bounded_lines(Small())) == ["event: message", 'data: {"a": 1}', "", "tail"]


def test_switching_stdio_to_http_deletes_the_stdio_secrets(env, http_fake):
    kc, tmp = env
    _add_stdio(tmp)
    assert any("FAKE_API_TOKEN" in k for k in kc.items)
    mcp_hub.save_server("notes", "http", url=HTTP_URL)
    assert not any("FAKE_API_TOKEN" in k for k in kc.items)


def test_server_names_leave_room_for_the_connector_prefix(env):
    with pytest.raises(mcp_hub.HubError):
        mcp_hub.save_server("a" * 37, "stdio", command="echo hi")
    mcp_hub.save_server("a" * 36, "stdio", command="echo hi")         # mcp-<36> = 40 chars
    assert [s["name"] for s in mcp_hub.list_servers()] == ["a" * 36]
