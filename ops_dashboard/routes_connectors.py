"""Conectores page + API (connectors framework). Registered on the shared route
registry (imported from routes_meta), so server.py does not grow.

Every route is **local-only and same-origin**. On ``main`` the server reflects
any ``Origin`` with credentials and loopback POSTs skip CSRF (fixed by #24/#26,
still open), so this module carries its own guard instead of relying on them:

* loopback peer, no ``X-Forwarded-For`` / ``X-Relay`` (``handler._is_local``);
* ``Host`` must be a loopback name (DNS-rebinding);
* ``Origin`` (when sent) must equal ``http://<Host>``; ``Sec-Fetch-Site`` (when
  sent) must be ``same-origin`` or ``none``;
* POST bodies must be ``application/json`` (no simple cross-site form posts).

Confirming a queued write additionally needs the payload hash shown on the
confirmation card, so a stale or tampered request cannot run another payload.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import connector_actions as actions
from . import connector_oauth as oauth
from . import connector_registry as registry
from . import connector_store as store
from . import connector_tokens as tokens
from .routing import router

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")
MAX_BODY = 128 * 1024
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_CLIENT_ID_RE = re.compile(r"^[\x21-\x7e]{1,256}$")


def _send_json(handler: Any, status: int, obj: dict) -> None:
    handler._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                  "application/json; charset=utf-8")


def _err(handler: Any, status: int, code: str, message: str = "") -> None:
    _send_json(handler, status, {"ok": False, "error": code, "message": message or code})


def _host_ok(host: str) -> bool:
    host = (host or "").strip().lower()
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in _LOOPBACK_HOSTS


def guard_failure(handler: Any) -> tuple[int, str] | None:
    """Return (status, code) if the request must be refused, else None."""
    if not handler._is_local():
        return 403, "local_only"
    host = handler.headers.get("Host") or ""
    if not _host_ok(host):
        return 421, "invalid_host"
    origin = handler.headers.get("Origin")
    if origin is not None and origin != f"http://{host}":
        return 403, "cross_origin"
    site = handler.headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        return 403, "cross_site"
    if handler.command == "POST":
        ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return 415, "json_required"
    return None


def _guarded(fn: Callable) -> Callable:
    def wrapper(handler: Any, **kw: Any) -> None:
        bad = guard_failure(handler)
        if bad:
            _err(handler, bad[0], bad[1], "connectors are only reachable from this Mac's own pages")
            return
        try:
            fn(handler, **kw)
        except (oauth.OAuthError, actions.ActionError) as e:
            _err(handler, e.status, e.code, str(e))
        except tokens.TokenStoreUnavailable as e:
            _err(handler, 503, "keychain_unavailable", str(e))
        except registry.RegistryError as e:
            _err(handler, 400, "bad_request", str(e))
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _body(handler: Any) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except ValueError:
        length = -1
    if length < 0 or length > MAX_BODY:
        raise actions.ActionError("body_too_large", f"body must be <= {MAX_BODY} bytes", 413)
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as e:
        raise actions.ActionError("bad_json", "body must be a JSON object") from e
    if not isinstance(data, dict):
        raise actions.ActionError("bad_json", "body must be a JSON object")
    return data


def _spec(connector_id: str) -> registry.ConnectorSpec:
    spec = registry.get(connector_id)
    if spec is None:
        raise actions.ActionError("unknown_connector", connector_id, 404)
    return spec


def connector_view(spec: registry.ConnectorSpec) -> dict[str, Any]:
    acc = store.get_account(spec.id)
    return {**spec.public(), "status": acc["status"], "permission": acc["permission"],
            "granted_scopes": acc["granted_scopes"], "account_label": acc["account_label"],
            "client_id": acc["client_id"] or "", "client_id_set": bool(acc["client_id"] or spec.default_client_id),
            "last_error": acc["last_error"], "connected_at": acc["connected_at"]}


# ---------- page ----------

def _serve_web(name: str, ctype: str, headers: dict | None = None) -> Callable:
    @_guarded
    def serve(handler: Any) -> None:
        with open(os.path.join(_WEB_DIR, name), "rb") as fh:
            body = fh.read()
        handler._send(200, body, ctype, headers or None)
    serve.__name__ = f"serve_{name.replace('.', '_').replace('-', '_')}"
    return serve


router.add("GET", "/connectors", _serve_web("connectors.html", "text/html; charset=utf-8",
                                            {"Content-Security-Policy": PAGE_CSP}))
router.add("GET", "/connectors.css", _serve_web("connectors.css", "text/css; charset=utf-8"))


# ---------- API ----------

@router.get("/api/connectors")
@_guarded
def connectors_list(handler: Any) -> None:
    from . import db
    legacy = 0
    try:
        legacy = len(db.list_connections())
    except Exception:
        legacy = 0
    _send_json(handler, 200, {"ok": True, "keychain": tokens.available(),
                              "connectors": [connector_view(s) for s in registry.all_specs() if s.auth != "mcp"],
                              "pending": len(actions.list_actions("pending")),
                              "legacy_token_connections": legacy})


@router.post("/api/connectors/<connector_id>/client")
@_guarded
def connector_client(handler: Any, connector_id: str) -> None:
    """BYOK: save (or clear with "") the user's own OAuth client id, and the
    client secret for providers that require one (keychain only)."""
    spec = _spec(connector_id)
    data = _body(handler)
    cid = data.get("client_id")
    if not isinstance(cid, str) or (cid and not _CLIENT_ID_RE.match(cid.strip())):
        raise actions.ActionError("bad_client_id", "client_id must be printable ASCII (<= 256)")
    cid = cid.strip()
    secret = data.get("client_secret")
    if cid and secret:
        if not isinstance(secret, str) or not _CLIENT_ID_RE.match(secret.strip()):
            raise actions.ActionError("bad_client_secret", "client_secret must be printable ASCII")
        tokens.save_client_secret(spec.id, secret.strip())
    if not cid:
        tokens.delete_client_secret(spec.id)
    store.upsert_account(spec.id, client_id=cid or None)
    store.audit(spec.id, "client_saved" if cid else "client_cleared",
                {"client_secret_set": bool(cid and secret)})
    _send_json(handler, 200, {"ok": True, "connector": connector_view(spec)})


@router.post("/api/connectors/<connector_id>/connect")
@_guarded
def connector_connect(handler: Any, connector_id: str) -> None:
    spec = _spec(connector_id)
    if spec.auth == "token":
        # Token connectors connect through the token kit, never through OAuth.
        raise actions.ActionError("token_connector",
                                  f"{connector_id} uses a token: POST /api/connectors/token/{connector_id}/token")
    data = _body(handler)
    permission = data.get("permission", "read")
    if permission not in registry.PERMISSIONS:
        raise actions.ActionError("bad_permission", "permission must be read or write")
    _send_json(handler, 200, {"ok": True, **oauth.start(connector_id, permission)})


@router.get("/api/connectors/flows/<state>")
@_guarded
def connector_flow(handler: Any, state: str) -> None:
    st = oauth.flow_status(state)
    if st is None:
        raise actions.ActionError("unknown_state", "unknown flow", 404)
    _send_json(handler, 200, {"ok": True, **st})


@router.post("/api/connectors/<connector_id>/disconnect")
@_guarded
def connector_disconnect(handler: Any, connector_id: str) -> None:
    spec = _spec(connector_id)
    _body(handler)
    oauth.disconnect(spec.id)
    _send_json(handler, 200, {"ok": True, "connector": connector_view(spec)})


@router.post("/api/connectors/<connector_id>/permission")
@_guarded
def connector_permission(handler: Any, connector_id: str) -> None:
    """Downgrade to read-only locally (pending writes expire). Upgrading to
    write needs a new consent: POST /connect with permission=write."""
    spec = _spec(connector_id)
    data = _body(handler)
    if data.get("permission") != "read":
        raise actions.ActionError("reconnect_required", "granting write needs a new consent (connect)", 409)
    store.upsert_account(spec.id, permission="read")
    expired = actions.expire_for_connector(spec.id)
    store.audit(spec.id, "permission_changed", {"permission": "read", "pending_expired": expired})
    _send_json(handler, 200, {"ok": True, "connector": connector_view(spec)})


@router.get("/api/connectors/actions")
@_guarded
def connector_actions_list(handler: Any) -> None:
    qs = parse_qs(urlparse(handler.path).query)
    status = (qs.get("status") or ["pending"])[0]
    if status not in ("pending", "done", "failed", "rejected", "expired", "all"):
        raise actions.ActionError("bad_status", status)
    _send_json(handler, 200, {"ok": True, "actions": actions.list_actions(None if status == "all" else status)})


@router.post("/api/connectors/actions/<action_id>/confirm")
@_guarded
def connector_action_confirm(handler: Any, action_id: str) -> None:
    data = _body(handler)
    if not action_id.isdigit():
        raise actions.ActionError("unknown_action_id", action_id, 404)
    _send_json(handler, 200, actions.confirm(int(action_id), str(data.get("payload_hash") or "")))


@router.post("/api/connectors/actions/<action_id>/reject")
@_guarded
def connector_action_reject(handler: Any, action_id: str) -> None:
    _body(handler)
    if not action_id.isdigit():
        raise actions.ActionError("unknown_action_id", action_id, 404)
    _send_json(handler, 200, actions.reject(int(action_id)))


@router.get("/api/connectors/audit")
@_guarded
def connector_audit(handler: Any) -> None:
    qs = parse_qs(urlparse(handler.path).query)
    try:
        limit = int((qs.get("limit") or ["100"])[0])
    except ValueError:
        limit = 100
    _send_json(handler, 200, {"ok": True, "events": store.list_audit(limit)})


# MCP servers section (registers /api/connectors/mcp/*).
# Native connectors (Google, Slack, Notion, Microsoft…): each one registers itself
# from its own module in ops_dashboard/connectors_native/. Appended at EOF on
# purpose so it never collides with other edits to this file.
from . import (
    connectors_native,  # noqa: E402,F401
    routes_mcp,  # noqa: E402,F401
)
