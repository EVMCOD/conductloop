"""Back-compat shim for the old minimal MCP client.

The implementation now lives in ``mcp_client`` (transports + JSON-RPC) and
``mcp_hub`` (server list, policies, keychain secrets, confirmation). This
module keeps the two functions the legacy ``/api/mcp/servers/<name>/discover``
route and older callers use, with the same return shape ``(value, error)``,
but with the new client's guarantees:

* stdio children get an allowlisted environment (no inherited secrets);
* request ids are matched (the old stdio discovery never matched and always
  timed out);
* the legacy ``sse`` transport is reported as unsupported (use Streamable HTTP);
* ``call_tool`` goes through ``mcp_hub.invoke``: read-only tools run, anything
  that may write is queued for confirmation in the Conectores page and this
  returns ``(None, "confirmation_required:<action_id>")``.
"""
from __future__ import annotations

from typing import Any

from . import mcp_client


def discover_tools(name: str, transport: str, *, command: list[str] | None = None,
                   url: str | None = None, env: dict | None = None) -> tuple[list[dict] | None, str | None]:
    from . import connector_actions, mcp_hub
    row = mcp_hub._row(name) if mcp_hub.NAME_RE.match(name or "") else None
    if row is not None and not row.get("legacy_env") and isinstance(env, dict) and "vars" in env:
        # Managed by the Conectores page: use the hub (keychain secrets, policies, log).
        try:
            view = mcp_hub.refresh(name)
        except connector_actions.ActionError as e:
            return None, e.code
        return [{"name": t["name"], "description": t["description"]} for t in view["tools"]], None
    server = {"name": name, "transport": transport, "command": command, "url": url}
    try:
        with mcp_client.open_session(server, env={k: str(v) for k, v in (env or {}).items()}) as s:
            tools, _rejected = mcp_hub.sanitize_tools(s.list_tools())
        return [{k: t[k] for k in ("name", "description", "inputSchema", "annotations")} for t in tools], None
    except mcp_client.MCPError as e:
        return None, f"{e.code}: {mcp_hub.clean_text(str(e), 200)}"


def call_tool(server: dict, tool_name: str, arguments: dict, timeout: float = 30.0) -> tuple[Any | None, str | None]:
    from . import connector_actions, mcp_hub
    try:
        out = mcp_hub.invoke(str(server.get("name") or ""), tool_name, arguments or {})
    except connector_actions.ActionError as e:
        return None, e.code
    if out.get("status") == "confirmation_required":
        return None, f"confirmation_required:{out['action_id']}"
    return out.get("result"), None
