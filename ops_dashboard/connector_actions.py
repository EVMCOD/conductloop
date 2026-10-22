"""Connector actions: reads run, **writes wait for the user**.

``invoke()`` is the only entry point for agents and UI code:

* ``read`` actions run immediately if the connector is connected.
* ``write`` actions (send an email, post a message, create an event…) never
  run from ``invoke()``. They need permission ``write`` on the account and are
  queued as a *pending action* with the SHA-256 of the canonical payload. Only
  ``confirm(action_id, payload_hash)`` — called from the Conectores page after
  the user clicks "Confirmar" — executes them, once, before ``CONFIRM_TTL``.
  There is deliberately no ``confirmed=True`` shortcut.

Every step is written to the local audit log (``connector_store.audit``) with a
redacted detail; nothing is sent anywhere else.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from . import connector_oauth as oauth
from . import connector_registry as registry
from . import connector_store as store
from . import db as _dbmod

CONFIRM_TTL = 15 * 60
MAX_PAYLOAD = 64 * 1024


class ActionError(Exception):
    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


class ConnectorClient:
    """HTTP client handed to action handlers: bearer token injected, https
    only, and only to the connector's ``allowed_hosts``."""

    def __init__(self, spec: registry.ConnectorSpec, token: str) -> None:
        self.spec = spec
        self._token = token

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if not self.spec.host_allowed(url):
            raise ActionError("host_not_allowed", f"{self.spec.id} may not contact {url}", 400)
        headers = dict(kw.pop("headers", {}) or {})
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        with oauth._client() as c:
            return c.request(method, url, headers=headers, **kw)

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)


def _resolve(connector_id: str, action: str) -> tuple[registry.ConnectorSpec, registry.ActionSpec]:
    spec = registry.get(connector_id)
    if spec is None:
        raise ActionError("unknown_connector", connector_id, 404)
    a = spec.actions.get(action)
    if a is None:
        raise ActionError("unknown_action", f"{connector_id}.{action}", 404)
    return spec, a


def _client_for(spec: registry.ConnectorSpec) -> ConnectorClient:
    if spec.auth != "oauth_pkce":
        # token / mcp connectors: the handler brings its own credentials; the
        # framework still requires the account to be connected.
        if store.get_account(spec.id)["status"] != "connected":
            raise ActionError("not_connected", spec.id, 409)
        return ConnectorClient(spec, "")
    try:
        return ConnectorClient(spec, oauth.access_token(spec.id))
    except oauth.OAuthError as e:
        raise ActionError(e.code, str(e), e.status) from e


def _check_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ActionError("bad_payload", "payload must be an object")
    if len(canonical(payload).encode("utf-8")) > MAX_PAYLOAD:
        raise ActionError("payload_too_large", f"payload must be <= {MAX_PAYLOAD} bytes", 413)
    return payload


def invoke(connector_id: str, action: str, payload: dict[str, Any] | None = None, *,
           now: float | None = None) -> dict[str, Any]:
    spec, a = _resolve(connector_id, action)
    payload = _check_payload(payload or {})
    if a.kind == "read":
        client = _client_for(spec)
        try:
            result = a.handler(client, payload)
        except Exception as e:
            code = e.code if isinstance(e, ActionError) else type(e).__name__
            store.audit(connector_id, "read_failed", {"action": action, "error": code})
            raise ActionError("action_failed", code, 502) from e
        store.audit(connector_id, "read", {"action": action})
        return {"ok": True, "status": "done", "result": result}
    return request_write(connector_id, action, payload, now=now)


def request_write(connector_id: str, action: str, payload: dict[str, Any], *,
                  now: float | None = None) -> dict[str, Any]:
    spec, a = _resolve(connector_id, action)
    if a.kind != "write":
        raise ActionError("not_a_write", action)
    payload = _check_payload(payload)
    acc = store.get_account(connector_id)
    if acc["status"] != "connected":
        raise ActionError("not_connected", connector_id, 409)
    if acc["permission"] != "write":
        store.audit(connector_id, "write_blocked", {"action": action, "reason": "read_only"})
        raise ActionError("write_not_permitted", "connector is read-only; grant write first", 403)
    now = time.time() if now is None else now
    h = payload_hash(payload)
    summary = (a.summarize(payload) if a.summarize else f"{spec.name}: {a.description}")[:500]
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO connector_actions (connector_id, action, payload, payload_hash, summary, status, "
            "created_at, expires_at) VALUES (?,?,?,?,?,'pending',?,?)",
            (connector_id, action, canonical(payload), h, summary, int(now), int(now + CONFIRM_TTL)))
        action_id = int(cur.lastrowid or 0)
    store.audit(connector_id, "write_requested", {"action_id": action_id, "action": action, "payload_hash": h})
    return {"ok": True, "status": "confirmation_required", "action_id": action_id, "payload_hash": h,
            "summary": summary, "expires_at": int(now + CONFIRM_TTL)}


def _row(action_id: int) -> dict[str, Any] | None:
    with _dbmod.db() as conn:
        r = conn.execute("SELECT id, connector_id, action, payload, payload_hash, summary, status, result, "
                         "created_at, expires_at, decided_at FROM connector_actions WHERE id = ?",
                         (int(action_id),)).fetchone()
    if not r:
        return None
    keys = ("id", "connector_id", "action", "payload", "payload_hash", "summary", "status", "result",
            "created_at", "expires_at", "decided_at")
    return dict(zip(keys, r))


def _set_status(action_id: int, status: str, result: Any = None, *, now: float,
                only_if: str = "pending") -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("UPDATE connector_actions SET status = ?, result = ?, decided_at = ? "
                           "WHERE id = ? AND status = ?",
                           (status, None if result is None else json.dumps(store.redact(result), ensure_ascii=False),
                            int(now), int(action_id), only_if))
        return cur.rowcount == 1


def confirm(action_id: int, confirm_hash: str, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    row = _row(action_id)
    if row is None:
        raise ActionError("unknown_action_id", str(action_id), 404)
    cid = row["connector_id"]
    if row["status"] != "pending":
        raise ActionError("not_pending", row["status"], 409)
    if now > row["expires_at"]:
        _set_status(action_id, "expired", now=now)
        store.audit(cid, "write_expired", {"action_id": action_id})
        raise ActionError("expired", "confirmation window elapsed; ask again", 410)
    if not isinstance(confirm_hash, str) or confirm_hash != row["payload_hash"]:
        raise ActionError("hash_mismatch", "the confirmed payload is not the queued one", 409)
    spec, a = _resolve(cid, row["action"])
    acc = store.get_account(cid)
    if acc["status"] != "connected" or acc["permission"] != "write":
        _set_status(action_id, "failed", {"error": "write_not_permitted"}, now=now)
        store.audit(cid, "write_blocked", {"action_id": action_id, "reason": "permission_changed"})
        raise ActionError("write_not_permitted", "connector is no longer writable", 403)
    # Claim the row first (pending -> done) so a double click can't run it twice.
    if not _set_status(action_id, "done", {"running": True}, now=now):
        raise ActionError("not_pending", "already decided", 409)
    store.audit(cid, "write_confirmed", {"action_id": action_id, "action": row["action"]})
    payload = json.loads(row["payload"])
    try:
        client = _client_for(spec)
        result = a.handler(client, payload)
    except Exception as e:
        code = e.code if isinstance(e, ActionError) else type(e).__name__
        _set_status(action_id, "failed", {"error": code}, now=now, only_if="done")
        store.audit(cid, "write_failed", {"action_id": action_id, "error": code})
        raise ActionError("action_failed", code, 502) from e
    _set_status(action_id, "done", result, now=now, only_if="done")
    store.audit(cid, "write_done", {"action_id": action_id})
    return {"ok": True, "status": "done", "action_id": action_id, "result": store.redact(result)}


def reject(action_id: int, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    row = _row(action_id)
    if row is None:
        raise ActionError("unknown_action_id", str(action_id), 404)
    if not _set_status(action_id, "rejected", now=now):
        raise ActionError("not_pending", row["status"], 409)
    store.audit(row["connector_id"], "write_rejected", {"action_id": action_id})
    return {"ok": True, "status": "rejected", "action_id": action_id}


def list_actions(status: str | None = "pending", limit: int = 50, *, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    _expire_due(now)
    sql = ("SELECT id, connector_id, action, payload, payload_hash, summary, status, created_at, expires_at, "
           "decided_at FROM connector_actions")
    args: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with _dbmod.db() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        out.append({"id": r[0], "connector_id": r[1], "action": r[2], "payload": json.loads(r[3]),
                    "payload_hash": r[4], "summary": r[5], "status": r[6], "created_at": r[7],
                    "expires_at": r[8], "decided_at": r[9]})
    return out


def _expire_due(now: float) -> int:
    with _dbmod.db() as conn:
        due = conn.execute("SELECT id, connector_id FROM connector_actions WHERE status='pending' AND expires_at < ?",
                           (int(now),)).fetchall()
        conn.execute("UPDATE connector_actions SET status='expired', decided_at=? "
                     "WHERE status='pending' AND expires_at < ?", (int(now), int(now)))
    for aid, cid in due:
        store.audit(cid, "write_expired", {"action_id": aid})
    return len(due)


def expire_for_connector(connector_id: str, *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    with _dbmod.db() as conn:
        cur = conn.execute("UPDATE connector_actions SET status='expired', decided_at=? "
                           "WHERE status='pending' AND connector_id = ?", (int(now), connector_id))
        return int(cur.rowcount or 0)
