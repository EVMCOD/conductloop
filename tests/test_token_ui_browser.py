"""Headless Chrome check of native token connectors on /connectors with a
fake connector and recorded responses (no network): paste a token → connected
(read-only) → sync into the vault → grant writes → an agent's write waits in
"Pendiente de confirmar" → forget imported data → disconnect. A
connections-mode connector offers the existing "Conexiones" rows. No CSP
violations or JS errors. Skipped without Playwright/Chrome unless
CONNECTORS_BROWSER_REQUIRED=1 (the CI browser step)."""
from __future__ import annotations

import os
import shutil
import socket
import threading

import pytest
from token_fake import FAKE_TOKEN, fake_recorded, make_fake, token_env

REQUIRED = os.environ.get("CONNECTORS_BROWSER_REQUIRED") == "1"
CHROME = (os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
          or shutil.which("chromium") or shutil.which("chromium-browser"))
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]
if not REQUIRED:
    pytestmark = pytest.mark.skipif(sync_playwright is None or not CHROME, reason="no Playwright/Chrome")
pytest.importorskip("cryptography")


def _app_server(address, handler):
    """The app's own server class. The stdlib default queues 5 connections;
    Chrome opens six at once and a page now loads more files (appearance.js,
    app-nav.js), so the extra ones were dropped, a script never arrived and
    the test timed out once in six runs. The app serves with a queue of 128
    (server._LocalHTTPServer), and so must its tests."""
    from ops_dashboard.server import _LocalHTTPServer
    return _LocalHTTPServer(address, handler)


@pytest.fixture
def site(tmp_path, monkeypatch):
    assert sync_playwright is not None and CHROME, "Playwright and Chrome are required"
    real_connect = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, addr, *a, **k)
        raise AssertionError(f"network used: {addr}")
    monkeypatch.setattr(socket.socket, "connect", guarded)
    from ops_dashboard.connectors_token import base
    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass
    rec = fake_recorded()
    with token_env(tmp_path, rec) as env:
        make_fake("keychain")
        make_fake("connections", cid="fakegh", provider="github")
        from ops_dashboard import db
        db.add_connection("github", "personal", {"token": FAKE_TOKEN})
        srv = _app_server(("127.0.0.1", 0), Q)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{srv.server_address[1]}", env
        finally:
            srv.shutdown()
            srv.server_close()
            base.unregister_connector("fakenotes")
            base.unregister_connector("fakegh")


def test_native_connector_cards(site):
    base_url, env = site
    from ops_dashboard import connector_actions as actions
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            pg = browser.new_context(locale="es-ES").new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            pg.on("console", lambda m: errors.append(f"console: {m.text}")
                  if "Content Security Policy" in m.text or (m.type == "error" and "Failed to load resource" not in m.text)
                  else None)

            # A native dialog must never be raised: the question is asked in
            # the page. See the note in test_connectors_ui_browser.py.
            def native_dialog(d):
                errors.append(f"native dialog: {d.type} {d.message}")
                d.dismiss()
            pg.on("dialog", native_dialog)
            pg.goto(base_url + "/connectors")
            # The connection settings live below the catalogue, folded.
            pg.eval_on_selector("#settings-details", "e => { e.open = true; }")
            card = '[data-connector="fakenotes"]'
            pg.wait_for_selector(f"{card} [data-testid=token-details] input[type=password]")
            assert pg.text_content(f"{card} [data-testid=status]") == "desconectado"
            assert "Crea un token de solo lectura" in (pg.text_content(card) or "")

            pg.fill(f"{card} input[type=password]", "wrong-token")
            pg.click(f"{card} button:text-is('Conectar (solo lectura)')")
            pg.wait_for_selector("#global-error:not([hidden])")
            # The error path re-renders the page (H.load); with MCP + sync cards
            # that reload takes longer, so wait for it before typing again.
            pg.wait_for_load_state("networkidle")
            assert env["kc"].items == {}
            pg.fill(f"{card} input[type=password]", FAKE_TOKEN)
            pg.click(f"{card} button:text-is('Conectar (solo lectura)')")
            pg.wait_for_selector(f"{card} [data-testid=status]:text-is('conectado')")
            assert pg.text_content(f"{card} [data-testid=permission]") == "solo lectura"
            assert FAKE_TOKEN not in (pg.content() or "")
            pg.wait_for_selector(f"{card} [data-testid=token-source]:text-is('Token en el llavero del sistema.')")

            pg.fill(f'{card} textarea[data-field="folders"]', "work\nhome")
            pg.click(f"{card} button:text-is('Guardar configuración')")
            pg.wait_for_function("document.querySelector('[data-connector=\"fakenotes\"] textarea') && "
                                 "document.querySelector('[data-connector=\"fakenotes\"] textarea').value === 'work\\nhome'")
            pg.click(f"{card} button:text-is('Sincronizar ahora')")
            pg.wait_for_selector(f"{card} :text('2 documentos en el vault')")
            assert "2 elementos" in (pg.text_content(f"{card} [data-testid=token-last-sync]") or "")
            assert pg.query_selector(f"{card} script") is None

            pg.click(f"{card} button:text-is('Permitir escrituras (con confirmación)')")
            pg.wait_for_selector(f"{card} [data-testid=permission]:text-is('lectura y escritura')")
            q = actions.invoke("fakenotes", "create_note", {"title": "desde el agente"})
            pg.reload()
            item = f'#pending-list li[data-action-id="{q["action_id"]}"]'
            pg.wait_for_selector(item)
            assert env["rec"].writes() == []
            pg.click(f"{item} button:text-is('Confirmar y ejecutar')")
            pg.wait_for_selector("#pending-empty:not([hidden])")
            assert env["rec"].writes()[0]["body"] == {"title": "desde el agente"}

            # "Olvidar" asks in the page: the documents are still there while
            # the dialog is open, and only "Confirmar" forgets them.
            pg.click(f"{card} button:text-is('Olvidar datos importados')")
            pg.wait_for_selector("#confirm-dialog[open]")
            assert "2 documentos en el vault" in (pg.text_content(card) or "")
            pg.click("#confirm-yes")
            pg.wait_for_selector(f"{card} :text('0 documentos en el vault')")
            pg.click(f"{card} button:text-is('Desconectar')")
            pg.wait_for_selector("#confirm-dialog[open]")
            pg.click("#confirm-yes")
            pg.wait_for_selector(f"{card} [data-testid=status]:text-is('desconectado')")

            gh = '[data-connector="fakegh"]'
            pg.wait_for_selector(f"{gh} select")
            assert pg.text_content(f"{gh} select option") == "personal"
            pg.click(f"{gh} button:text-is('Usar esta conexión')")
            pg.wait_for_selector(f"{gh} [data-testid=status]:text-is('conectado')")
            pg.wait_for_selector(f"{gh} [data-testid=token-source]")
            assert "personal" in (pg.text_content(f"{gh} [data-testid=token-source]") or "")

            en = browser.new_context(locale="en-US").new_page()
            en.goto(base_url + "/connectors")
            # The connection settings live below the catalogue, folded.
            en.eval_on_selector("#settings-details", "e => { e.open = true; }")
            en.wait_for_selector(f"{gh} [data-testid=token-source]")
            assert "Connections" in (en.text_content(f"{gh} [data-testid=token-source]") or "")
            assert en.is_visible(f"{gh} button:text-is('Sync now')")
        finally:
            browser.close()
    assert errors == []
