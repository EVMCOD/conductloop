"""Headless-Chrome check of Google writes on /connectors: a Gmail send and a
Calendar event show up in «Pendiente de confirmar» with the recipients/time in
the card; Confirm sends exactly once, Reject sends nothing. Recorded responses,
no network. Skipped without Playwright/Chrome; run locally with
CONNECTORS_BROWSER_REQUIRED=1 (CI's browser step only runs #40's file)."""
from __future__ import annotations

import os
import shutil
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest

REQUIRED = os.environ.get("CONNECTORS_BROWSER_REQUIRED") == "1"
CHROME = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]


@pytest.mark.skipif(not REQUIRED and (sync_playwright is None or not CHROME), reason="no Playwright/Chrome")
def test_confirm_and_reject_google_writes(tmp_path, monkeypatch):
    import test_connector_gmail_write as tgw
    from connectors_native_fake import connect

    from ops_dashboard import connector_actions as actions
    from ops_dashboard import connector_oauth as oauth
    from ops_dashboard import connector_tokens as tokens
    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    real = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real(self, addr, *a, **k)
        raise AssertionError(f"network used: {addr}")
    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "w.db")
    db.init_db()
    fake = tgw.tgm.make_fake()
    fake.hosts.add("www.googleapis.com")
    fake.routes[("POST", f"{tgw.tgm.API}/messages/send")] = tgw._send
    fake.routes[("POST", "www.googleapis.com/calendar/v3/calendars/primary/events")] = tgw._send
    prev_t, prev_b = oauth.set_transport(fake.transport()), tokens.use_backend(tokens.MemoryBackend())
    srv = None
    try:
        connect("gmail", fake, client_secret=tgw.tgm.SECRET, permission="write")
        connect("google_calendar", fake, client_secret=tgw.tgm.SECRET, permission="write")
        mail = actions.invoke("gmail", "send_message", {"to": "luis@example.com", "subject": "<img src=x onerror=alert(1)>",
                                                       "body": "Visto."})
        ev = actions.invoke("google_calendar", "create_event", {"title": "Revisión", "start": "2026-10-22T10:00:00+02:00",
                                                                "end": "2026-10-22T11:00:00+02:00"})

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
            pg.on("dialog", lambda d: errors.append("dialog: " + d.message))
            pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/connectors")
            m_item = f'#pending-list li[data-action-id="{mail["action_id"]}"]'
            e_item = f'#pending-list li[data-action-id="{ev["action_id"]}"]'
            pg.wait_for_selector(m_item)
            assert pg.text_content("#pending-count") == "2"
            assert "ENVIAR a luis@example.com" in (pg.text_content(m_item) or "")
            assert pg.query_selector(f"{m_item} img") is None
            assert "sin invitaciones" in (pg.text_content(e_item) or "")
            pg.click(f"{e_item} button:text-is('Rechazar')")
            pg.wait_for_selector(e_item, state="detached")
            assert not [r for r in fake.requests if r.method == "POST" and r.url.host == "www.googleapis.com"]
            pg.click(f"{m_item} button:text-is('Confirmar y ejecutar')")
            pg.wait_for_selector("#pending-empty:not([hidden])")
            browser.close()
        sends = [r for r in fake.requests if r.method == "POST" and r.url.host != "oauth2.googleapis.com"]
        assert [r.url.host for r in sends] == ["gmail.googleapis.com"]
        assert errors == []
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)
