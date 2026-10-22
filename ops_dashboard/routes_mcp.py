"""MCP servers section of the Conectores page: /api/connectors/mcp/*.

Same guard as the rest of the connectors API (local-only, same-origin, JSON
POSTs — see ``routes_connectors``). Secret values (bearer token, secret env
vars) are accepted on write and never returned.
"""
from __future__ import annotations

from typing import Any

from . import connector_tokens as tokens
from . import mcp_hub as hub
from .routes_connectors import _body, _guarded, _send_json
from .routing import router


@router.get("/api/connectors/mcp")
@_guarded
def mcp_list(handler: Any) -> None:
    hub.ensure_registered()
    _send_json(handler, 200, {"ok": True, "keychain": tokens.available(), "servers": hub.list_servers()})


@router.post("/api/connectors/mcp/servers")
@_guarded
def mcp_save(handler: Any) -> None:
    d = _body(handler)
    for k in ("env", "secret_env"):
        if d.get(k) is not None and not isinstance(d.get(k), dict):
            raise hub.HubError("bad_env", f"{k} must be an object")
    token = d.get("token")
    if token is not None and not isinstance(token, str):
        raise hub.HubError("bad_token", "token must be a string")
    view = hub.save_server(str(d.get("name") or ""), str(d.get("transport") or ""), command=d.get("command"),
                           url=d.get("url"), env=d.get("env"), secret_env=d.get("secret_env"), token=token)
    _send_json(handler, 200, {"ok": True, "server": view})


@router.post("/api/connectors/mcp/servers/<name>/refresh")
@_guarded
def mcp_refresh(handler: Any, name: str) -> None:
    _body(handler)
    _send_json(handler, 200, {"ok": True, "server": hub.refresh(name)})


@router.post("/api/connectors/mcp/servers/<name>/permission")
@_guarded
def mcp_permission(handler: Any, name: str) -> None:
    d = _body(handler)
    _send_json(handler, 200, {"ok": True, "server": hub.set_permission(name, str(d.get("permission") or ""))})


@router.post("/api/connectors/mcp/servers/<name>/tools/<tool>/mode")
@_guarded
def mcp_tool_mode(handler: Any, name: str, tool: str) -> None:
    d = _body(handler)
    _send_json(handler, 200, {"ok": True, "server": hub.set_tool_mode(name, tool, str(d.get("mode") or ""))})


@router.post("/api/connectors/mcp/servers/<name>/tools/<tool>/call")
@_guarded
def mcp_tool_call(handler: Any, name: str, tool: str) -> None:
    """Read tools run now (result is untrusted data); write tools are queued
    and must be confirmed via /api/connectors/actions/<id>/confirm."""
    d = _body(handler)
    args = d.get("arguments") or {}
    if not isinstance(args, dict):
        raise hub.HubError("bad_arguments", "arguments must be an object")
    _send_json(handler, 200, hub.invoke(name, tool, args))


@router.post("/api/connectors/mcp/servers/<name>/remove")
@_guarded
def mcp_remove(handler: Any, name: str) -> None:
    _body(handler)
    hub.remove_server(name)
    _send_json(handler, 200, {"ok": True})
