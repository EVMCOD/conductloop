"""Static + server checks for the sessions page (PR 8) that run in CI (the
headless-Chrome walk-through lives in test_sessions_ui_browser.py)."""
from __future__ import annotations

import json
import re
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
JS = (WEB / "sessions.js").read_text(encoding="utf-8")
HTML = (WEB / "sessions.html").read_text(encoding="utf-8")
CODE = re.sub(r"^\s*//.*$", "", JS, flags=re.M)       # the header comment names the sinks


@pytest.mark.parametrize("pattern", [
    r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write",
    r"\beval\s*\(", r"new\s+Function\b", r"setTimeout\s*\(\s*['\"]", r"setInterval\s*\(\s*['\"]",
    r"\.href\s*=", r"\.src\s*=", r"window\.confirm|[^.\w]confirm\s*\(", r"api_key|apiKey",
])
def test_sessions_js_has_no_html_sinks_or_key_values(pattern):
    assert not re.search(pattern, CODE), pattern


def test_html_has_no_inline_script_handlers_or_styles():
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", HTML)      # only external scripts
    assert not re.search(r"\son[a-z]+\s*=", HTML, re.I)              # no inline handlers
    assert not re.search(r"\sstyle\s*=", HTML, re.I)                 # CSP style-src 'self'
    assert '<script src="/provider-errors.js"' in HTML               # reuses #15 renderer


def test_both_languages_have_the_same_keys():
    en = re.search(r"\n    en: \{(.*?)\n    \},\n    es: \{", JS, re.S)
    es = re.search(r"\n    es: \{(.*?)\n    \}\n  \};", JS, re.S)
    assert en and es
    keys = [set(re.findall(r"(?:^|[\s,{])([a-z_]+):\s*'", b.group(1))) for b in (en, es)]
    assert keys[0] == keys[1] and len(keys[0]) > 100
    used = set(re.findall(r"\bt\('([a-z_]+)'(?! \+)", JS)) | set(re.findall(r'data-i18n(?:-ph)?="([a-z_]+)"', HTML))
    assert used <= keys[0], used - keys[0]


def test_index_links_to_the_page():
    idx = (WEB / "index.html").read_text(encoding="utf-8")
    assert idx.count('href="/sessions"') == 1


@pytest.fixture()
def server(tmp_path, monkeypatch):
    dbp = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _get(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10) as r:
        return r.status, r.headers, r.read()


def test_page_and_assets_are_served_with_a_strict_csp(server):
    st, h, body = _get(server + "/sessions")
    assert st == 200 and h["Content-Type"].startswith("text/html")
    csp = h["Content-Security-Policy"]
    for d in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "object-src 'none'",
              "frame-ancestors 'none'"):
        assert d in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert b'id="sessions"' in body
    st, h, _ = _get(server + "/sessions.css")
    assert st == 200 and h["Content-Type"].startswith("text/css")
    for js in ("/sessions.js", "/provider-errors.js"):
        st, h, body = _get(server + js)
        assert st == 200 and h["Content-Type"].startswith("application/javascript") and body


def test_remote_page_needs_auth(server):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/sessions", {"X-Forwarded-For": "10.0.0.2"})
    assert e.value.code == 401


def test_session_list_says_whether_totals_include_estimates(server, tmp_path):
    from ops_dashboard import routes_sessions
    st = routes_sessions.supervisor().store
    import subprocess
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    st.mark_repo(str(repo))
    a = st.create_session(name="a", role="a", provider="mock", model="m", repo_path=str(repo),
                          base_ref="main", budget_cost_cents=100)
    b = st.create_session(name="b", role="b", provider="mock", model="m", repo_path=str(repo),
                          base_ref="main", budget_cost_cents=100)
    st.record_event(a, "llm.turn", {"estimated": True})
    st.record_event(b, "llm.turn", {"estimated": False})
    _, _, body = _get(server + "/api/sessions")
    items = {s["id"]: s for s in json.loads(body)["items"]}
    assert items[a]["usage_includes_estimates"] is True
    assert items[b]["usage_includes_estimates"] is False
    routes_sessions.configure()
