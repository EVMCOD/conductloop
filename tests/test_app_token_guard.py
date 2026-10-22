"""Per-launch app token for loopback browser requests (browser design PR 2).

Real server on an ephemeral port. Covers: guard off without the env var; boot
nonce single use; cookie flags; browser-looking requests (Origin / Sec-Fetch-*)
need the cookie on every method incl. GET; CLI-style requests keep loopback
trust; exempt public paths; remote requests untouched; one parse_request.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

TOKEN = "a" * 64
BOOT = "b" * 64


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    from ops_dashboard import app_token_guard, db
    from ops_dashboard.indexers import live_urls
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    live_urls.ensure_schema()
    app_token_guard._reset_for_tests()
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    def call(method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": f"localhost:{port}"}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        hdrs["_set_cookie_all"] = r.msg.get_all("Set-Cookie") or []
        c.close()
        return r.status, data, hdrs

    yield call
    httpd.shutdown()
    app_token_guard._reset_for_tests()


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_APP_TOKEN", TOKEN)
    monkeypatch.setenv("CONDUCTLOOP_APP_BOOT", BOOT)


def _code(data: bytes) -> str:
    return json.loads(data)["code"]


BROWSER = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
COOKIE = {"Cookie": f"cl_app={TOKEN}"}


def test_disabled_without_env(srv, monkeypatch):
    monkeypatch.delenv("CONDUCTLOOP_APP_TOKEN", raising=False)
    st, _, _ = srv("GET", "/api/projects", BROWSER)
    assert st == 200
    st, _, h = srv("GET", "/__cl/boot?n=whatever")
    assert st == 302 and h["location"] == "/"


def test_short_token_counts_as_disabled(srv, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_APP_TOKEN", "short")
    st, _, _ = srv("GET", "/api/projects", BROWSER)
    assert st == 200


def test_boot_sets_cookie_once(srv, on):
    st, _, h = srv("GET", f"/__cl/boot?n={BOOT}", BROWSER)
    assert st == 302 and h["location"] == "/"
    cookie = next(c for c in h["_set_cookie_all"] if c.startswith("cl_app="))
    assert cookie.startswith(f"cl_app={TOKEN};")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Path=/" in cookie
    assert "Max-Age" not in cookie and "Expires" not in cookie  # session cookie
    # burnt
    st, data, _ = srv("GET", f"/__cl/boot?n={BOOT}", BROWSER)
    assert st == 403 and _code(data) == "app_boot_invalid"


@pytest.mark.parametrize("q", ["", "?n=", "?n=" + "c" * 64, "?n=" + BOOT[:-1]])
def test_boot_rejects_bad_nonce(srv, on, q):
    st, data, _ = srv("GET", "/__cl/boot" + q, BROWSER)
    assert st == 403 and _code(data) == "app_boot_invalid"
    # a bad attempt does not burn the real nonce
    st, _, _ = srv("GET", f"/__cl/boot?n={BOOT}", BROWSER)
    assert st == 302


def test_boot_only_get(srv, on):
    st, data, _ = srv("POST", f"/__cl/boot?n={BOOT}", {"Origin": "http://localhost:7777"}, b"{}")
    assert st == 403 and _code(data) == "app_boot_invalid"


@pytest.mark.parametrize("headers", [
    BROWSER,                                              # typed URL / app navigate
    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},  # link from a page
    {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"},     # tab that loaded the UI
    {"Origin": "http://localhost:7777"},                  # allowlisted origin, no cookie
    {"Origin": "null"},
    {"sec-fetch-dest": "image"},                          # <img> (case-insensitive)
])
def test_browser_requests_without_cookie_refused(srv, on, headers):
    for method, path in [("GET", "/"), ("GET", "/api/projects"), ("GET", "/brain")]:
        st, data, h = srv(method, path, headers)
        assert st == 403, (method, path, headers)
        assert _code(data) == "app_token_required"
        assert h.get("connection", "").lower() == "close"


def test_browser_writes_without_cookie_never_reach_handler(srv, on):
    st, data, _ = srv("POST", "/api/agents/create",
                      {"Origin": "http://localhost:7777", "Content-Type": "application/json"},
                      json.dumps({"name": "pwned"}).encode())
    assert st == 403 and _code(data) == "app_token_required"
    from ops_dashboard import db
    with db.get_connection() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM team_members").fetchall()]
    assert "pwned" not in names


def test_wrong_cookie_refused(srv, on):
    for bad in ["cl_app=" + "a" * 63, "cl_app=" + "z" * 64, "cl_app=", "other=" + TOKEN]:
        st, _, _ = srv("GET", "/api/projects", {**BROWSER, "Cookie": bad})
        assert st == 403, bad


def test_browser_with_cookie_allowed(srv, on):
    st, _, _ = srv("GET", "/api/projects", {**BROWSER, **COOKIE})
    assert st == 200
    st, _, _ = srv("GET", "/api/projects", {**BROWSER, "Cookie": f"ops_csrf=x; cl_app={TOKEN}; y=z"})
    assert st == 200


def test_cli_style_requests_keep_loopback_trust(srv, on):
    st, _, _ = srv("GET", "/api/projects")
    assert st == 200
    st, data, _ = srv("POST", "/api/agents/create", {"Content-Type": "application/json"}, b"{}")
    assert st != 403  # reaches the handler (e.g. 400 name required)


@pytest.mark.parametrize("path", ["/api/health", "/health", "/api/pricing-tier"])
def test_public_paths_exempt(srv, on, path):
    st, _, _ = srv("GET", path, {"Origin": "https://conductloop.com", "Sec-Fetch-Site": "cross-site"})
    assert st != 403


def test_remote_requests_untouched(srv, on):
    # X-Forwarded-For => not local for _is_local(); the normal auth gate applies.
    st, data, _ = srv("GET", "/api/projects", {**BROWSER, "X-Forwarded-For": "203.0.113.9"})
    assert not (st == 403 and b"app_token_required" in data)


def test_html_gets_minimal_csp(srv):
    st, _, h = srv("GET", "/")
    if h.get("content-type", "").startswith("text/html"):
        csp = h.get("content-security-policy", "")
        for d in ["object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'", "form-action 'self'"]:
            assert d in csp


def test_single_parse_request_hook():
    src = (Path(__file__).resolve().parent.parent / "ops_dashboard" / "server.py").read_text()
    assert len(re.findall(r"^\s+def parse_request\(", src, re.M)) == 1
    assert "app_token_guard.check(self)" in src


def test_tauri_wrapper_passes_token_and_boots():
    lib = (Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "src" / "lib.rs").read_text()
    assert '"CONDUCTLOOP_APP_TOKEN"' in lib and '"CONDUCTLOOP_APP_BOOT"' in lib
    assert "/__cl/boot?n=" in lib
