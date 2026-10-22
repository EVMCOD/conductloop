"""Fake MCP servers for tests — no network.

* stdio: ``tests/fixtures/mcp/fake_mcp_server.py`` run with this interpreter.
* Streamable HTTP: ``FakeHttpMCP`` on ``httpx.MockTransport`` (session id,
  protocol-version header, JSON and SSE responses, bearer auth, 202 for
  notifications); raises on any other host.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

SERVER = Path(__file__).parent / "fixtures" / "mcp" / "fake_mcp_server.py"
STDIO_CMD = [sys.executable, str(SERVER)]
HTTP_URL = "https://mcp.fake.test/mcp"
TOKEN = "fake-mcp-bearer-0001"


class FakeHttpMCP:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.sessions: set[str] = set()
        self.requests: list[dict] = []
        self.created: list[dict] = []
        self.deleted: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.host != "mcp.fake.test":
            raise AssertionError(f"network used: {req.url}")
        if req.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if req.method == "DELETE":
            self.deleted.append(req.headers.get("Mcp-Session-Id", ""))
            return httpx.Response(200)
        assert "text/event-stream" in req.headers.get("Accept", "")
        msg = json.loads(req.content)
        self.requests.append({"msg": msg, "headers": dict(req.headers)})
        if msg.get("method") == "initialize":
            self.sessions.add("sess-1")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-http-mcp", "version": "2.0"}}}, headers={"Mcp-Session-Id": "sess-1"})
        if req.headers.get("Mcp-Session-Id") not in self.sessions:
            return httpx.Response(404)
        assert req.headers.get("MCP-Protocol-Version") == "2025-06-18"
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "tools/list":
            tools = [{"name": "list_issues", "description": "List issues", "inputSchema": {"type": "object"},
                      "annotations": {"readOnlyHint": True}},
                     {"name": "create_issue", "description": "Create an issue", "inputSchema": {"type": "object"}}]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}})
        if msg["method"] == "tools/call":
            name = msg["params"]["name"]
            if name == "create_issue":
                self.created.append(msg["params"]["arguments"])
            body = {"jsonrpc": "2.0", "id": msg["id"],
                    "result": {"content": [{"type": "text", "text": f"{name} ok"}]}}
            # SSE response with a progress notification first, like a real server.
            sse = ("event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress",
                                                          "params": {"progress": 1}}) + "\n\n"
                   "event: message\ndata: " + json.dumps(body) + "\n\n")
            return httpx.Response(200, content=sse.encode(), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601,
                                                                                       "message": "nope"}})


def run_eval_case(params: dict, tmp_dir: Path) -> str:
    """Run one docs/evals/mcp-tools.json case and return the JSON text the
    eval checks inspect. Needs a fresh DB (caller's job)."""
    import os

    from ops_dashboard import connector_actions as actions
    from ops_dashboard import connector_tokens as tokens
    from ops_dashboard import mcp_hub as hub

    out_file = tmp_dir / "mcp-side-effects.txt"
    prev = tokens.use_backend(tokens.MemoryBackend())
    os.environ["CONDUCTLOOP_EVAL_PARENT_SECRET"] = "sk-ant-" + "x" * 30   # must NOT reach the child
    try:
        hub.save_server("fake", "stdio", command=STDIO_CMD, env={"FAKE_MCP_OUT": str(out_file)},
                        secret_env={"FAKE_API_TOKEN": "child-only-secret"})
        hub.refresh("fake")
        if params.get("permission") == "write":
            hub.set_permission("fake", "write")
        try:
            res = hub.invoke("fake", params["tool"], params.get("arguments") or {})
        except actions.ActionError as e:
            res = {"ok": False, "error": e.code}
        if res.get("status") == "done" and isinstance(res.get("result"), dict):
            res["fenced"] = hub.fence_for_llm(res["result"])
        res["side_effects"] = out_file.read_text().count("\n") if out_file.exists() else 0
        return json.dumps(res, ensure_ascii=False, sort_keys=True)
    finally:
        os.environ.pop("CONDUCTLOOP_EVAL_PARENT_SECRET", None)
        try:
            hub.remove_server("fake")
        finally:
            tokens.use_backend(prev)
