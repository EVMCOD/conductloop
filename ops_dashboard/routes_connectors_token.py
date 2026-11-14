"""Routes for token / no-auth connectors (``connectors_token``).

All routes go through the #40 connectors guard (loopback, same-origin, JSON).
Status, disconnect and the confirmation queue reuse the #40 routes; this
module only adds what a personal token needs:

  GET  /api/connectors/token                         per-connector extras
  POST /api/connectors/token/<connector_id>/token    {token} | {connection_id}
  POST /api/connectors/token/<connector_id>/permission {permission}
  POST /api/connectors/token/<connector_id>/config   {config}
  POST /api/connectors/token/<connector_id>/sync     read-only ingest
  POST /api/connectors/token/<connector_id>/forget   delete ingested docs
"""
from __future__ import annotations

from typing import Any

from . import connector_actions as actions
from . import connectors_token
from .connectors_token import base
from .routes_connectors import _body, _guarded, _send_json, connector_view
from .routing import router

connectors_token.load_all()

def _payload(connector_id: str) -> dict:
    from . import connector_registry as registry
    spec = registry.get(connector_id)
    out = base.view(connector_id)
    if spec is not None:
        out["connector"] = connector_view(spec)
    return out


@router.get("/api/connectors/token")
@_guarded
def token_list(handler: Any) -> None:
    _send_json(handler, 200, {"ok": True, "connectors": {nc.id: base.view(nc.id) for nc in base.all_connectors()}})


@router.post("/api/connectors/token/<connector_id>/token")
@_guarded
def token_token(handler: Any, connector_id: str) -> None:
    data = _body(handler)
    token, cid = data.get("token"), data.get("connection_id")
    if token is not None and not isinstance(token, str):
        raise actions.ActionError("bad_token", "token must be a string")
    if cid is not None and (isinstance(cid, bool) or not isinstance(cid, int)):
        raise actions.ActionError("bad_connection", "connection_id must be an integer")
    base.connect(connector_id, token=token, connection_id=cid)
    _send_json(handler, 200, {"ok": True, **_payload(connector_id)})


@router.post("/api/connectors/token/<connector_id>/permission")
@_guarded
def token_permission(handler: Any, connector_id: str) -> None:
    base.set_permission(connector_id, str(_body(handler).get("permission") or ""))
    _send_json(handler, 200, {"ok": True, **_payload(connector_id)})


@router.post("/api/connectors/token/<connector_id>/config")
@_guarded
def token_config(handler: Any, connector_id: str) -> None:
    base.save_config(connector_id, _body(handler).get("config"))
    _send_json(handler, 200, {"ok": True, **_payload(connector_id)})


@router.post("/api/connectors/token/<connector_id>/sync")
@_guarded
def token_sync(handler: Any, connector_id: str) -> None:
    _body(handler)
    res = actions.invoke(connector_id, "sync", {})
    _send_json(handler, 200, {"ok": True, "sync": res.get("result"), **_payload(connector_id)})


@router.post("/api/connectors/token/<connector_id>/forget")
@_guarded
def token_forget(handler: Any, connector_id: str) -> None:
    _body(handler)
    n = base.forget(connector_id)
    _send_json(handler, 200, {"ok": True, "deleted": n, **_payload(connector_id)})
