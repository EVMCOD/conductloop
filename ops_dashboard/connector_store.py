"""Connector state + local audit log (SQLite, migration 14).

Only *state* lives here — status, permission, granted scopes, a display label
and the (public) BYOK client id. Tokens and client secrets are in the OS
keychain (``connector_tokens``). The audit log is local-only: nothing in this
module, or anywhere in the framework, sends events off the machine.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from . import db as _dbmod

_SENSITIVE_KEY = re.compile(r"token|secret|password|passwd|verifier|authorization|cookie|^code$|api_?key",
                            re.IGNORECASE)
_MAX_STR = 300
AUDIT_EVENTS = (
    "client_saved", "client_cleared", "connect_started", "connected", "connect_failed",
    "disconnected", "disconnect_failed", "permission_changed", "token_refreshed", "refresh_failed",
    "read", "read_failed", "write_requested", "write_confirmed", "write_rejected",
    "write_expired", "write_done", "write_failed", "write_blocked",
    # MCP servers (mcp_hub): connection log + configuration changes.
    "mcp_added", "mcp_updated", "mcp_removed", "mcp_connected", "mcp_connect_failed",
    "mcp_tools_changed", "mcp_tool_mode", "mcp_secret_saved", "mcp_server_trust",
)


def redact(obj: Any, _depth: int = 0) -> Any:
    """Drop secret-looking keys and cap sizes before anything is audited."""
    if _depth > 4:
        return "…"
    if isinstance(obj, dict):
        return {str(k): ("[redacted]" if _SENSITIVE_KEY.search(str(k)) else redact(v, _depth + 1))
                for k, v in list(obj.items())[:40]}
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in list(obj)[:40]]
    if isinstance(obj, str):
        return obj if len(obj) <= _MAX_STR else obj[:_MAX_STR] + "…"
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj
    return str(obj)[:_MAX_STR]


def audit(connector_id: str, event: str, detail: dict | None = None, *, now: float | None = None) -> None:
    if event not in AUDIT_EVENTS:
        raise ValueError(f"unknown audit event {event!r}")
    with _dbmod.db() as conn:
        conn.execute("INSERT INTO connector_audit (ts, connector_id, event, detail) VALUES (?,?,?,?)",
                     (int(now if now is not None else time.time()), connector_id, event,
                      json.dumps(redact(detail or {}), ensure_ascii=False, sort_keys=True)))


def list_audit(limit: int = 100, connector_id: str | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    sql = "SELECT id, ts, connector_id, event, detail FROM connector_audit"
    args: list[Any] = []
    if connector_id:
        sql += " WHERE connector_id = ?"
        args.append(connector_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _dbmod.db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{"id": r[0], "ts": r[1], "connector_id": r[2], "event": r[3],
             "detail": json.loads(r[4] or "{}")} for r in rows]


_DEFAULT: dict[str, Any] = {"status": "disconnected", "permission": "read", "granted_scopes": [],
            "account_label": None, "client_id": None, "last_error": None, "connected_at": None,
            "updated_at": None}


def get_account(connector_id: str) -> dict:
    with _dbmod.db() as conn:
        r = conn.execute("SELECT status, permission, granted_scopes, account_label, client_id, last_error, "
                         "connected_at, updated_at FROM connector_accounts WHERE connector_id = ?",
                         (connector_id,)).fetchone()
    if not r:
        return {"connector_id": connector_id, **_DEFAULT, "granted_scopes": []}
    return {"connector_id": connector_id, "status": r[0], "permission": r[1],
            "granted_scopes": json.loads(r[2] or "[]"), "account_label": r[3], "client_id": r[4],
            "last_error": r[5], "connected_at": r[6], "updated_at": r[7]}


_FIELDS = ("status", "permission", "granted_scopes", "account_label", "client_id", "last_error", "connected_at")


def upsert_account(connector_id: str, **fields: Any) -> dict:
    bad = set(fields) - set(_FIELDS)
    if bad:
        raise ValueError(f"unknown fields {sorted(bad)}")
    cur = get_account(connector_id)
    cur.update(fields)
    now = int(time.time())
    with _dbmod.db() as conn:
        conn.execute(
            "INSERT INTO connector_accounts (connector_id, status, permission, granted_scopes, account_label, "
            "client_id, last_error, connected_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(connector_id) DO UPDATE SET status=excluded.status, permission=excluded.permission, "
            "granted_scopes=excluded.granted_scopes, account_label=excluded.account_label, "
            "client_id=excluded.client_id, last_error=excluded.last_error, "
            "connected_at=excluded.connected_at, updated_at=excluded.updated_at",
            (connector_id, cur["status"], cur["permission"], json.dumps(list(cur["granted_scopes"] or [])),
             cur["account_label"], cur["client_id"], cur["last_error"], cur["connected_at"], now))
    return get_account(connector_id)
