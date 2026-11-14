"""MCP server registry.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import json
import time

from ops_dashboard import db as _dbmod

# ============================================================
# MCP servers
# ============================================================

def upsert_mcp_server(name: str, transport: str, *, command: list | None = None,
                       url: str | None = None, env: dict | None = None) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        existing = conn.execute("SELECT id FROM mcp_servers WHERE name = ?", (name,)).fetchone()
        if existing:
            # Adding or re-adding a server is the user choosing to run it:
            # that is the moment the trust is given.
            conn.execute(
                """UPDATE mcp_servers SET transport=?, command=?, url=?, env=?,
                   trust_readonly=1, updated_at=? WHERE name=?""",
                (transport,
                 json.dumps(command) if command else None,
                 url, json.dumps(env or {}), now, name),
            )
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO mcp_servers (name, transport, command, url, env, enabled,
                   trust_readonly, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)""",
            (name, transport,
             json.dumps(command) if command else None,
             url, json.dumps(env or {}), now, now),
        )
        return int(cur.lastrowid or 0)


def set_mcp_server_trust(name: str, trust_readonly: bool) -> bool:
    """Record whether the user trusts this server's own read-only annotations.

    Off by default: a server must not be able to grant itself a no-approval
    write by labelling its tool ``readOnlyHint`` (2026-10-23).
    """
    with _dbmod.db() as conn:
        cur = conn.execute(
            "UPDATE mcp_servers SET trust_readonly=?, updated_at=? WHERE name=?",
            (1 if trust_readonly else 0, int(time.time()), name),
        )
        return cur.rowcount > 0


def list_mcp_servers(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM mcp_servers"
    if enabled_only: sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_mcp_server(name: str) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM mcp_servers WHERE name = ?", (name,)).fetchone()
    if not row: return None
    d = dict(row)
    try: d["command"] = json.loads(d.get("command") or "null")
    except Exception: d["command"] = None
    try: d["env"] = json.loads(d.get("env") or "{}")
    except Exception: d["env"] = {}
    try: d["tools_cache"] = json.loads(d.get("tools_cache") or "[]")
    except Exception: d["tools_cache"] = []
    return d


def cache_mcp_tools(name: str, tools: list[dict]) -> None:
    with _dbmod.db() as conn:
        conn.execute(
            "UPDATE mcp_servers SET tools_cache=?, tools_cached_at=?, last_error=NULL, updated_at=? WHERE name=?",
            (json.dumps(tools), int(time.time()), int(time.time()), name),
        )


def set_mcp_server_error(name: str, error: str) -> None:
    with _dbmod.db() as conn:
        conn.execute(
            "UPDATE mcp_servers SET last_error=?, updated_at=? WHERE name=?",
            (error, int(time.time()), name),
        )


def delete_mcp_server(name: str) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
        return cur.rowcount > 0


def bind_agent_tool(agent_id: int, server_name: str, tool_name: str) -> None:
    now = int(time.time())
    with _dbmod.db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO agent_mcp_tools
               (agent_id, server_name, tool_name, enabled, created_at)
               VALUES (?, ?, ?, 1, ?)""",
            (agent_id, server_name, tool_name, now),
        )


def unbind_agent_tool(agent_id: int, server_name: str, tool_name: str) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute(
            "DELETE FROM agent_mcp_tools WHERE agent_id = ? AND server_name = ? AND tool_name = ?",
            (agent_id, server_name, tool_name),
        )
        return cur.rowcount > 0


def list_agent_tools(agent_id: int) -> list[dict]:
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM agent_mcp_tools WHERE agent_id = ? AND enabled = 1",
            (agent_id,),
        ).fetchall()]
