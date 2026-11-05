"""Fake MCP server over real Streamable HTTP on 127.0.0.1 (stdlib only).

Unlike ``tests/mcp_fake.FakeHttpMCP`` (an ``httpx.MockTransport``), this one
listens on a real loopback socket, so the whole client stack (httpx, SSE
parsing, session header, bearer auth) runs exactly as against a real server.
Used by ``scripts/smoke-connectors.py`` (packaged-app verification) and by
``tests/test_connectors_no_account_smoke.py``.

Run: ``python fake_mcp_http_server.py [--port N] [--token T] [--out FILE]``.
Prints ``PORT <n>`` on stdout once listening. Tools:
  search_notes  readOnlyHint   → JSON response
  echo_sse      readOnlyHint   → answered as an SSE stream (with a log event first)
  create_note   (no hints)     → write; appends its arguments to --out
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RO = {"readOnlyHint": True}
TOOLS = [
    {"name": "search_notes", "description": "Search the notes vault.",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}, "annotations": RO},
    {"name": "echo_sse", "description": "Echo text back over SSE.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}, "annotations": RO},
    {"name": "create_note", "description": "Create a note.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}},
]


def make_server(port: int = 0, token: str | None = None, out: str | None = None) -> ThreadingHTTPServer:
    sessions: set[str] = set()
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _auth_ok(self) -> bool:
            return token is None or self.headers.get("Authorization") == f"Bearer {token}"

        def _json(self, status: int, body: object, extra: dict | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_DELETE(self):  # noqa: N802
            with lock:
                sessions.discard(self.headers.get("Mcp-Session-Id") or "")
            self._empty(200)

        def do_POST(self):  # noqa: N802
            if self.path != "/mcp":
                return self._empty(404)
            if not self._auth_ok():
                return self._empty(401)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                msg = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._empty(400)
            m, i, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
            if m == "initialize":
                sid = secrets.token_hex(8)
                with lock:
                    sessions.add(sid)
                return self._json(200, {"jsonrpc": "2.0", "id": i, "result": {
                    "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp-http", "version": "1.0"}}}, {"Mcp-Session-Id": sid})
            with lock:
                known = (self.headers.get("Mcp-Session-Id") or "") in sessions
            if not known:
                return self._empty(404)
            if i is None:                       # notification
                return self._empty(202)
            if m == "tools/list":
                return self._json(200, {"jsonrpc": "2.0", "id": i, "result": {"tools": TOOLS}})
            if m == "tools/call":
                name, args = p.get("name"), p.get("arguments") or {}
                if name == "search_notes":
                    return self._json(200, {"jsonrpc": "2.0", "id": i, "result": {
                        "content": [{"type": "text", "text": f"HTTP note for {args.get('q')!s}"}]}})
                if name == "echo_sse":
                    events = [{"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "log"}},
                              {"jsonrpc": "2.0", "id": i, "result": {
                                  "content": [{"type": "text", "text": f"sse:{args.get('text')!s}"}]}}]
                    body = "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return None
                if name == "create_note":
                    if out:
                        with open(out, "a", encoding="utf-8") as fh:
                            fh.write(json.dumps(args) + "\n")
                    return self._json(200, {"jsonrpc": "2.0", "id": i, "result": {
                        "content": [{"type": "text", "text": "created"}]}})
                return self._json(200, {"jsonrpc": "2.0", "id": i,
                                        "error": {"code": -32602, "message": f"unknown tool {name}"}})
            return self._json(200, {"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "not found"}})

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--token")
    ap.add_argument("--out")
    a = ap.parse_args()
    srv = make_server(a.port, a.token, a.out)
    print(f"PORT {srv.server_address[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
