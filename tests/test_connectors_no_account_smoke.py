"""P1-4: connectors that need no account, end to end through the HTTP API.

Runs ``scripts/smoke-connectors.py`` (the same script used against the
packaged app) against the real request handler on 127.0.0.1:

* MCP stdio: the fake stdio server as a real child process.
* MCP Streamable HTTP: the fake HTTP server on a real loopback socket (real
  httpx, SSE parsing, session header and bearer auth; no MockTransport).
* RSS: through /api/connectors/token/rss/*, with recorded feed responses and
  offline DNS (no network).
"""
from __future__ import annotations

import importlib.util
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from token_fake import Recorded, token_env

pytest.importorskip("cryptography")

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("smoke_connectors", ROOT / "scripts" / "smoke-connectors.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)  # type: ignore[union-attr]

PUBLIC = {"blog.example.org": ["93.184.216.34"], "news.example.net": ["93.184.216.40"]}


@pytest.fixture
def base(tmp_path):
    from ops_dashboard import connector_registry as registry
    from ops_dashboard.connectors_token import base as tbase
    from ops_dashboard.connectors_token import rss
    from ops_dashboard.server import DashboardHandler

    with token_env(tmp_path, Recorded.load("rss/recorded.json"), resolver=lambda h: PUBLIC[h]):
        tbase.register_connector(rss.CONNECTOR)

        class Q(DashboardHandler):
            def log_message(self, *_a):
                pass
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = ThreadingHTTPServer(("127.0.0.1", port), Q)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            srv.shutdown()
            srv.server_close()
            for sp in registry.all_specs():
                if sp.id.startswith("mcp-"):
                    registry.unregister(sp.id)


def test_mcp_stdio_smoke(base, tmp_path):
    smoke.run_stdio(base, smoke.sys.executable, tmp_path)


def test_mcp_http_smoke_real_socket_with_bearer(base, tmp_path):
    smoke.run_http(base, smoke.sys.executable, tmp_path, token="smoke-bearer-0001")


def test_mcp_http_wrong_token_is_reported_not_connected(base, tmp_path):
    proc = smoke.subprocess.Popen([smoke.sys.executable, str(smoke.FIXTURES / "fake_mcp_http_server.py"),
                                   "--token", "right"], stdout=smoke.subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline().split()[1])
        st, _ = smoke._req(base, "/api/connectors/mcp/servers", {
            "name": "smoke-bad", "transport": "http", "url": f"http://127.0.0.1:{port}/mcp", "token": "wrong"})
        assert st == 200
        st, b = smoke._req(base, "/api/connectors/mcp/servers/smoke-bad/refresh", {})
        assert b.get("server", {}).get("status") != "connected"
        assert "wrong" not in smoke._text(b)
    finally:
        smoke._req(base, "/api/connectors/mcp/servers/smoke-bad/remove", {})
        proc.terminate()
        proc.wait(timeout=10)


def test_rss_smoke_through_routes(base):
    smoke.run_rss(base, "https://blog.example.org/feed")


def test_cli_main_exit_code(base):
    assert smoke.main(["--base", base, "--skip-http"]) == 0
