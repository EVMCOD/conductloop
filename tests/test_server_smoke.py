"""In-process smoke test for the ops-dashboard (ConductLoop) HTTP server.

Boots the real ``DashboardHandler`` on an ephemeral port against a throwaway
demo database, then sweeps every GET route declared in ``server.py`` and asserts
that none 500s or hangs. This is the regression guard that would have caught the
``/api/health`` 404 (endpoint declared public but never wired to a handler).

No network, no live worker — runs in the .venv with just stdlib + the package:
    .venv/bin/pytest tests/test_server_smoke.py -v
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

# Routes that make an LLM call, stream forever, hit the network, or mutate state
# on GET — excluded from the "must not hang / must not 500" sweep.
_SKIP = re.compile(
    r"(stream|webhook|/recap|/brain/|transcribe|/sync|/scan|broadcast|"
    r"test_send|/run|tools/suggest|/digest)"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get_routes() -> list[str]:
    src = (Path(__file__).resolve().parent.parent / "ops_dashboard" / "server.py").read_text()
    routes = sorted(set(re.findall(r'"(/api/[a-zA-Z0-9_/]+)"', src)))
    return [r for r in routes if not _SKIP.search(r)]


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    # Throwaway DB + demo fixtures so the sweep has real data to serve.
    db_path = tmp_path_factory.mktemp("ops") / "smoke.db"
    os.environ["CONDUCTLOOP_DEMO"] = "1"
    os.environ["CONDUCTLOOP_DB_PATH"] = str(db_path)

    from ops_dashboard import db
    from ops_dashboard.indexers import demo as demo_mod
    from ops_dashboard.indexers import live_urls
    from ops_dashboard.server import DashboardHandler

    db.DB_PATH = db_path  # type: ignore[attr-defined]
    db.init_db()
    live_urls.ensure_schema()
    demo_mod.run()

    class _QuietHandler(DashboardHandler):
        def log_message(self, *_a):  # silence per-request stderr noise
            pass

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _QuietHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def _hit(url: str, timeout: float = 8.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_health_both_paths(base_url):
    """Regression guard: /api/health and /health must return 200 + ok:true."""
    for path in ("/api/health", "/health"):
        status, body = _hit(base_url + path)
        assert status == 200, f"{path} returned {status}"
        payload = json.loads(body)
        assert payload["ok"] is True, f"{path} -> {payload}"


def test_no_get_route_500s_or_hangs(base_url):
    """Sweep every declared GET route; none may 500 or hang (>8s)."""
    failures = []
    for route in _get_routes():
        try:
            status, _ = _hit(base_url + route)
        except Exception as e:  # timeout / connection = hang
            failures.append(f"{route}: {type(e).__name__}")
            continue
        if status >= 500:
            failures.append(f"{route}: {status}")
    assert not failures, "GET routes 500'd or hung:\n" + "\n".join(failures)


def test_500_handler_does_not_leak(base_url):
    """The top-level 500 handler must return a generic body, never raw
    exception text (which can leak paths/SQL/secrets)."""
    from ops_dashboard.server import DashboardHandler

    captured = {}

    class _Fake(DashboardHandler):
        def __init__(self):  # bypass BaseHTTPRequestHandler socket setup
            self.command = "GET"
            self.path = "/api/boom"

        def _send(self, status, body, content_type="text/html", headers=None):
            captured["status"] = status
            captured["body"] = body

    _Fake()._send_500(RuntimeError("secret path /Users/someone/.env leaked"))
    assert captured["status"] == 500
    text = captured["body"].decode()
    assert "internal server error" in text
    assert "secret path" not in text and ".env" not in text
    assert json.loads(text)["id"]  # stable error id for log correlation


def test_service_worker_is_retired():
    # The old cache-first worker kept serving the previous version's UI after
    # every update. The served sw.js must only clean up, and no page registers it.
    from pathlib import Path
    web = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
    sw = (web / "sw.js").read_text(encoding="utf-8")
    assert "caches.delete" in sw and "unregister()" in sw
    assert "respondWith" not in sw and "addAll" not in sw
    for page in web.glob("*.html"):
        assert "serviceWorker.register" not in page.read_text(encoding="utf-8"), page.name
