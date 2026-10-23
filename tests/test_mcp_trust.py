"""The MCP trust model: a tool is read-only by default, but a server that
changes its mind stops being believed.

The product promises that a tool is read-only by default ("Las herramientas
son de solo lectura por defecto"), so a server the user chose to run is
trusted to label its own tools. What it is not trusted to do is change its
mind: when a tool's annotations change, the trust is revoked and the tool
goes back through the approval gate until the user has looked at it.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "mcp.db"))
    import ops_dashboard.db as _db

    _db.DB_PATH = tmp_path / "mcp.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import mcp_hub

    return _db, mcp_hub


def _tool(name: str, *, read_only: bool = False, destructive: bool = False) -> dict:
    return {
        "name": name,
        "description": f"{name} does something",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive},
        "desc_hash": f"hash-{name}-{read_only}-{destructive}",
    }


# ── the promise ────────────────────────────────────────────────────────────

def test_a_read_only_tool_is_read(env):
    _, hub = env
    assert hub.effective_kind(_tool("search_notes", read_only=True), "auto") == "read"


def test_a_tool_that_does_not_say_is_a_write(env):
    _, hub = env
    assert hub.effective_kind(_tool("create_note"), "auto") == "write"


def test_a_destructive_tool_is_a_write_even_if_it_claims_otherwise(env):
    _, hub = env
    tool = _tool("drop_everything", read_only=True, destructive=True)
    assert hub.effective_kind(tool, "auto") == "write"


def test_what_the_user_decided_wins_over_anything(env):
    _, hub = env
    assert hub.effective_kind(_tool("search_notes", read_only=True), "write") == "write"
    assert hub.effective_kind(_tool("create_note"), "read") == "read"
    assert hub.effective_kind(_tool("create_note"), "disabled") is None


def test_a_server_the_user_does_not_trust_sends_everything_to_the_gate(env):
    _, hub = env
    assert hub.effective_kind(_tool("search_notes", read_only=True), "auto",
                              trust_readonly=False) == "write"


# ── where the trust comes from ─────────────────────────────────────────────

def test_installing_a_server_is_the_moment_the_trust_is_given(env):
    db, hub = env
    db.upsert_mcp_server("notes", "stdio", command=["echo"])
    row = hub._row("notes")
    assert row is not None
    assert row["trust_readonly"] == 1
    assert hub.effective_kind(_tool("search_notes", read_only=True), "auto",
                              bool(row["trust_readonly"])) == "read"


def test_a_server_that_changes_its_annotations_loses_the_trust(env):
    db, hub = env
    db.upsert_mcp_server("notes", "stdio", command=["echo"])
    first = _tool("search_notes", read_only=True)
    assert hub._sync_policies("notes", [first]) == []       # first sight
    assert hub._row("notes")["trust_readonly"] == 1

    # The server now describes the same tool differently.
    second = _tool("search_notes", read_only=True)
    second["desc_hash"] = "hash-different"
    second["annotations"] = {"readOnlyHint": False}
    changed = hub._sync_policies("notes", [second])
    assert changed == ["search_notes"]
    assert hub._row("notes")["trust_readonly"] == 0        # trust revoked
    assert hub.effective_kind(second, "auto",
                              bool(hub._row("notes")["trust_readonly"])) == "write"


def test_the_user_can_give_the_trust_back(env):
    db, hub = env
    db.upsert_mcp_server("notes", "stdio", command=["echo"])
    hub.set_server_trust("notes", False)
    assert hub._row("notes")["trust_readonly"] == 0
    hub.set_server_trust("notes", True)
    assert hub._row("notes")["trust_readonly"] == 1
