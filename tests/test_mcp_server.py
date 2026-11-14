"""Guards the MCP tool dispatch (#1) — the part that exposes the brain to agents.
The stdio transport needs the optional `mcp` package + a client, so it's not
unit-tested here; the dispatch is."""
from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "mcp.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "mcp.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import mcp_server
    return _db, mcp_server


def test_tool_catalog_shape(env):
    _, mcp = env
    names = {t["name"] for t in mcp.TOOLS}
    assert {"search", "recall_memory", "add_capture", "list_decisions", "project_status"} <= names
    for t in mcp.TOOLS:
        assert set(t) >= {"name", "description", "inputSchema"}
        assert t["inputSchema"]["type"] == "object"


def test_call_add_capture_and_list_decisions(env):
    db, mcp = env
    r = mcp.call_tool("add_capture", {"content": "from an agent", "kind": "idea"})
    assert r["id"] > 0
    db.add_decision("A decision", "did a thing")
    d = mcp.call_tool("list_decisions", {"limit": 5})
    assert any(x["title"] == "A decision" for x in d["decisions"])


def test_call_recall_memory(env):
    db, mcp = env
    db.memory_set("stack", "uses SQLite + FTS5")
    r = mcp.call_tool("recall_memory", {"query": "what database"})
    assert "memories" in r


def test_unknown_tool_raises(env):
    _, mcp = env
    with pytest.raises(ValueError):
        mcp.call_tool("nope", {})


def test_client_config_shape(env):
    _, mcp = env
    cfg = mcp.client_config()
    assert "conductloop" in cfg["mcpServers"]
    server = cfg["mcpServers"]["conductloop"]
    assert server["args"] == ["mcp"] and server["command"]
