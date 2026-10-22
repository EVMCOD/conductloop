"""MCP server — exposes ConductLoop's second brain to other AI agents.

Any MCP client (Claude Desktop, an agent, etc.) can search your knowledge,
recall memory, drop a capture, read decisions, or pull a project status. The
tool dispatch (``call_tool``) is plain Python and fully testable; the stdio
transport (``serve_stdio``) uses the optional ``mcp`` package.

    ops mcp        # run the server over stdio (add to your MCP client config)
"""
from __future__ import annotations

import logging

from . import db

log = logging.getLogger("conductloop.mcp")

# Tool catalog — name, description, and JSON-schema input (MCP-shaped).
TOOLS: list[dict] = [
    {
        "name": "search",
        "description": "Search the second brain (Obsidian, repos, memory, URLs) and return matching documents.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
            "required": ["query"],
        },
    },
    {
        "name": "recall_memory",
        "description": "Recall the most relevant persistent memories for a query.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "add_capture",
        "description": "Drop a note/idea/task into the quick-capture inbox to triage later.",
        "inputSchema": {
            "type": "object",
            "properties": {"content": {"type": "string"}, "kind": {"type": "string", "default": "note"}},
            "required": ["content"],
        },
    },
    {
        "name": "list_decisions",
        "description": "List recent decisions from the decision log.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "project_status",
        "description": "Roll up everything known about a project (docs, repos, URL health).",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
]


def client_config() -> dict:
    """The MCP client config snippet (Claude Desktop's claude_desktop_config.json,
    Cursor, etc.) to register this server. Uses the installed `ops` console script."""
    import shutil
    return {
        "mcpServers": {
            "conductloop": {
                "command": shutil.which("ops") or "ops",
                "args": ["mcp"],
            }
        }
    }


def call_tool(name: str, arguments: dict | None = None) -> dict:
    """Dispatch an MCP tool call. Returns a JSON-serializable result dict."""
    args = arguments or {}
    if name == "search":
        rows = db.search(args["query"], limit=int(args.get("limit", 10)))
        return {"results": [dict(r) for r in rows]}
    if name == "recall_memory":
        from . import memory_ai
        return {"memories": memory_ai.recall(args["query"])}
    if name == "add_capture":
        from . import capture
        return {"id": capture.add(args["content"], kind=args.get("kind", "note"), source="mcp")}
    if name == "list_decisions":
        return {"decisions": db.list_decisions(limit=int(args.get("limit", 20)))}
    if name == "project_status":
        from . import projects
        return {"status": projects.rollup(args["project"])}
    raise ValueError(f"unknown tool: {name}")


def serve_stdio() -> None:  # pragma: no cover - requires the mcp package + a client
    """Run the MCP server over stdio. Needs `pip install mcp` (requirements-optional)."""
    import asyncio
    import json

    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    db.init_db()
    server = Server("conductloop")

    @server.list_tools()
    async def _list():
        return [Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"]) for t in TOOLS]

    @server.call_tool()
    async def _call(name: str, arguments: dict):
        result = call_tool(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    async def _run():
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    asyncio.run(_run())
