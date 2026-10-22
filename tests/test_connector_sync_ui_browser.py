"""Headless-Chrome check of the "Sincronizar ahora" add-on (connectors-sync.js)
with the Google Calendar connector against recorded responses. No network.
Skipped without Playwright/Chrome (CI's browser step only runs the #40 file;
run this one locally: CONNECTORS_BROWSER_REQUIRED=1 pytest tests/test_connector_sync_ui_browser.py)."""
from __future__ import annotations

import os
import re
import shutil
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
REQUIRED = os.environ.get("CONNECTORS_BROWSER_REQUIRED") == "1"
CHROME = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]


def test_sync_js_static_rules():
    js = re.sub(r"(?m)^\s*//.*$", "", (WEB / "connectors-sync.js").read_text(encoding="utf-8"))
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned
    assert "credentials: 'same-origin'" in js and "/api/connectors-sync" in js
    assert "/api/connectors-client-group/" in js
    assert '<script src="/connectors-sync.js" defer></script>' in (WEB / "connectors.html").read_text(encoding="utf-8")


@pytest.mark.skipif(not REQUIRED and (sync_playwright is None or not CHROME), reason="no Playwright/Chrome")
def test_sync_button_in_browser(tmp_path, monkeypatch):
    import test_connector_google_calendar as tg
    from connectors_native_fake import ProviderFake, connect

    from ops_dashboard import connector_oauth as oauth
    from ops_dashboard import connector_tokens as tokens
    from ops_dashboard import db
    from ops_dashboard.connectors_native import google_calendar as gcal
    from ops_dashboard.server import DashboardHandler

    real = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real(self, addr, *a, **k)
        raise AssertionError(f"network used: {addr}")
    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ui.db")
    monkeypatch.setattr(gcal.time, "time", lambda: 1792000000.0)
    db.init_db()
    fake = ProviderFake(token_url="https://oauth2.googleapis.com/token",
                        hosts=("oauth2.googleapis.com", "www.googleapis.com"), client_secret=tg.SECRET,
                        routes={("GET", f"{tg.API}/users/me/calendarList"): tg.REC["calendarList"],
                                ("GET", f"{tg.API}/calendars/ana%40example.com/events"): tg._events,
                                ("GET", f"{tg.API}/calendars/es.spain%23holiday%40group.v.calendar.google.com/events"):
                                    tg._events})
    prev_t, prev_b = oauth.set_transport(fake.transport()), tokens.use_backend(tokens.MemoryBackend())
    srv = None
    try:
        connect("google_calendar", fake, client_secret=tg.SECRET)

        class _Q(DashboardHandler):
            def log_message(self, *_a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Q)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        errors: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROME)
            pg = browser.new_context(locale="es-ES").new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
            pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/connectors")
            card = '[data-connector="google_calendar"]'
            pg.wait_for_selector(f"{card} [data-testid=sync]")
            pg.click(f"{card} [data-testid=sync]")
            pg.wait_for_selector(f"{card} [data-testid=sync-result]:has-text('Importado')")
            assert "events=3" in (pg.text_content(f"{card} [data-testid=sync-result]") or "")
            assert len(pg.query_selector_all(f"{card} [data-testid=sync]")) == 1
            browser.close()
        assert [e for e in errors if "404" not in e] == []
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)


@pytest.mark.skipif(not REQUIRED and (sync_playwright is None or not CHROME), reason="no Playwright/Chrome")
def test_share_google_app_in_browser(tmp_path, monkeypatch):
    """Paste the Google client once (Calendar), then "use the same app" on Gmail:
    Gmail gets the client ID/secret but no token — it still has to connect."""
    from ops_dashboard import connector_store as store
    from ops_dashboard import connector_tokens as tokens
    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    real = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real(self, addr, *a, **k)
        raise AssertionError(f"network used: {addr}")
    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "share.db")
    db.init_db()
    prev_b = tokens.use_backend(tokens.MemoryBackend())
    srv = None
    try:
        store.upsert_account("google_calendar", client_id="one-google-app")
        tokens.save_client_secret("google_calendar", "GOCSPX-fake")

        class _Q(DashboardHandler):
            def log_message(self, *_a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Q)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        errors: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROME)
            pg = browser.new_context(locale="es-ES").new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/connectors")
            card = '[data-connector="gmail"]'
            pg.wait_for_selector(f"{card} [data-testid=share-client]")
            assert "Google Calendar" in (pg.text_content(f"{card} [data-testid=share-client]") or "")
            assert pg.query_selector('[data-connector="google_calendar"] [data-testid=share-client]') is None
            with pg.expect_navigation():
                pg.click(f"{card} [data-testid=share-client]")
            pg.wait_for_selector('[data-connector="google_drive"]')
            pg.wait_for_timeout(300)
            assert pg.query_selector_all("[data-testid=share-client]") == []
            browser.close()
        assert errors == []
        assert store.get_account("gmail")["client_id"] == "one-google-app"
        assert tokens.load_client_secret("google_drive") == "GOCSPX-fake"
        assert tokens.load_token("gmail") is None
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()
        tokens.use_backend(prev_b)
