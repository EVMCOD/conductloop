"""End-to-end check of /connectors ("Conectores") in headless Chrome against
the real local server. No network: the OAuth provider is the in-process fake
(httpx.MockTransport); the browser never opens the provider URL — the test
plays the consent screen in Python and the browser only visits the loopback
callback, like a real redirect would.

Flow (es-ES): BYOK client id → connect with write → callback on 127.0.0.1 →
"conectado" → an agent queues a write → it shows in "Pendiente de confirmar"
with its payload rendered as text (hostile HTML must not execute) → nothing is
sent until "Confirmar" → downgrade to read-only → disconnect. Then en-US
strings. Strict CSP, no CSP violations or JS errors.

Skipped when Playwright or Chrome is missing, unless
CONNECTORS_BROWSER_REQUIRED=1 (or LEARNING_BROWSER_REQUIRED=1, set by the CI
browser step on the learning stack).
"""
from __future__ import annotations

import os
import shlex
import shutil
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest
from connectors_fake import FakeProvider, fake_spec

REQUIRED = "1" in (os.environ.get("CONNECTORS_BROWSER_REQUIRED"), os.environ.get("LEARNING_BROWSER_REQUIRED"))
CHROME = (os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
          or shutil.which("chromium") or shutil.which("chromium-browser"))
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None  # type: ignore[assignment]
if not REQUIRED:
    pytestmark = pytest.mark.skipif(sync_playwright is None or not CHROME, reason="no Playwright/Chrome")

XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'


@pytest.fixture
def site(tmp_path, monkeypatch):
    assert sync_playwright is not None and CHROME, "Playwright and Chrome are required"
    real_connect = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, addr, *a, **k)
        raise AssertionError(f"network used: {addr}")
    monkeypatch.setattr(socket.socket, "connect", guarded)

    from ops_dashboard import connector_oauth as oauth
    from ops_dashboard import connector_registry as registry
    from ops_dashboard import connector_tokens as tokens
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "c.db")
    db.init_db()
    prev_b = tokens.use_backend(tokens.MemoryBackend())
    provider = FakeProvider()
    prev_t = oauth.set_transport(provider.transport())
    spec = registry.register(fake_spec())

    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", provider
    finally:
        srv.shutdown()
        srv.server_close()
        registry.unregister(spec.id)
        oauth.set_transport(prev_t)
        tokens.use_backend(prev_b)


def test_connectors_page_full_flow(site):
    base, provider = site
    from ops_dashboard import connector_actions as actions
    from ops_dashboard import connector_store as store
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            ctx = browser.new_context(locale="es-ES")
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            pg.on("console", lambda m: errors.append(f"console: {m.text}")
                  if "Content Security Policy" in m.text or (m.type == "error" and "Failed to load resource" not in m.text)
                  else None)
            pg.on("response", lambda r: errors.append(f"http {r.status} {r.url}")
                  if r.status >= 400 and not r.url.endswith("/favicon.ico") else None)
            pg.on("dialog", lambda d: d.accept())

            resp = pg.goto(base + "/connectors")
            policies = [h["value"] for h in resp.headers_array() if h["name"].lower() == "content-security-policy"]
            assert any("script-src 'self'" in c and "object-src 'none'" in c for c in policies), policies
            assert not any("unsafe-inline" in c for c in policies), policies
            card = '[data-connector="fakechat"]'
            pg.wait_for_selector(card)
            assert pg.title().startswith("Conectores")
            assert pg.text_content(f"{card} [data-testid=status]") == "desconectado"
            assert pg.is_disabled(f"{card} button:text-is('Conectar (solo lectura)')")
            assert not pg.is_visible("#keychain-warning")

            # BYOK client id → connect with write
            pg.fill(f"{card} input[type=text]", "my-client-id")
            pg.click(f"{card} button:text-is('Guardar')")
            pg.wait_for_selector(f"{card} button:text-is('Conectar con escritura'):not([disabled])")
            pg.click(f"{card} button:text-is('Conectar con escritura')")
            link = pg.wait_for_selector(f"{card} [data-testid=consent-link]")
            href = link.get_attribute("href") or ""
            assert href.startswith("https://auth.fake.test/authorize?") and link.get_attribute("rel") == "noopener noreferrer"
            cb = provider.authorize(href)                    # the user clicks "Allow" at the provider
            tab = ctx.new_page()                             # provider redirects the browser to loopback
            tab.goto(f"{cb['redirect_uri']}?state={cb['state']}&code={cb['code']}")
            assert "Conectado" in (tab.text_content("body") or "")
            tab.close()
            pg.wait_for_selector(f"{card} [data-testid=status]:text-is('conectado')")
            assert pg.text_content(f"{card} [data-testid=permission]") == "lectura y escritura"

            # An agent queues a write: nothing is sent until the user confirms.
            q = actions.invoke("fakechat", "post_message", {"channel": "ops", "text": f"hola {XSS}"})
            assert q["status"] == "confirmation_required"
            pg.reload()
            item = f'#pending-list li[data-action-id="{q["action_id"]}"]'
            pg.wait_for_selector(item)
            assert pg.text_content("#pending-count") == "1"
            assert XSS in (pg.text_content(item) or "")       # rendered as text
            assert pg.query_selector(f"{item} img") is None and pg.query_selector(f"{item} script") is None
            assert provider.messages == []
            pg.click(f"{item} button:text-is('Confirmar y ejecutar')")
            pg.wait_for_selector("#pending-empty:not([hidden])")
            assert provider.messages == [{"channel": "ops", "text": f"hola {XSS}"}]

            # Downgrade, then disconnect (confirm dialog accepted).
            pg.click(f"{card} button:text-is('Pasar a solo lectura')")
            pg.wait_for_selector(f"{card} [data-testid=permission]:text-is('solo lectura')")
            pg.click(f"{card} button:text-is('Desconectar')")
            pg.wait_for_selector(f"{card} [data-testid=status]:text-is('desconectado')")
            assert store.get_account("fakechat")["status"] == "disconnected"
            pg.click("#audit-refresh")
            pg.wait_for_selector("#audit-body tr")
            audit = pg.text_content("#audit-body") or ""
            for ev in ("write_requested", "write_confirmed", "write_done", "disconnected"):
                assert ev in audit
            assert "fake-access" not in audit and "hola" not in audit

            # English
            en = browser.new_context(locale="en-US").new_page()
            en.goto(base + "/connectors")
            en.wait_for_selector(card)
            assert en.title().startswith("Connectors")
            assert en.text_content(f"{card} [data-testid=status]") == "disconnected"
            assert en.text_content("#pending-empty") == "No pending writes."
        finally:
            browser.close()
    assert errors == []


def test_mcp_servers_section(site, tmp_path):
    """Add a stdio server (local fake) from the page, test the connection,
    see the tools with their read/write kind, switch a tool off, allow
    writes, queue a tool write from an agent → it appears in the same
    "Pendiente de confirmar" list → confirm → remove the server."""
    base, _provider = site
    from mcp_fake import STDIO_CMD

    from ops_dashboard import connector_registry as registry
    from ops_dashboard import mcp_hub
    out = tmp_path / "mcp-side.txt"
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            pg = browser.new_context(locale="es-ES").new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            pg.on("console", lambda m: errors.append(f"console: {m.text}")
                  if "Content Security Policy" in m.text or (m.type == "error" and "Failed to load resource" not in m.text)
                  else None)
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/connectors")
            pg.wait_for_selector("#mcp-list p.hint:text-is('No hay servidores MCP.')")
            pg.click("#mcp-add summary")
            assert pg.is_visible("#mcp-command") and not pg.is_visible("#mcp-url")
            pg.select_option("#mcp-transport", "http")
            assert pg.is_visible("#mcp-url") and pg.is_visible("#mcp-token") and not pg.is_visible("#mcp-command")
            pg.select_option("#mcp-transport", "stdio")
            pg.fill("#mcp-name", "notes")
            pg.fill("#mcp-command", shlex.join(STDIO_CMD))
            pg.fill("#mcp-env", f"FAKE_MCP_OUT={out}")
            pg.fill("#mcp-secret-name", "FAKE_API_TOKEN")
            pg.fill("#mcp-secret-value", "child-only-secret")
            pg.click("#mcp-save")
            srv = '[data-mcp="notes"]'
            pg.wait_for_selector(srv)
            assert pg.input_value("#mcp-secret-value") == ""
            assert "child-only-secret" not in (pg.content() or "")
            pg.click(f"{srv} button:text-is('Probar conexión')")
            pg.wait_for_selector(f"{srv} [data-testid=mcp-status]:text-is('conectado')")
            pg.wait_for_selector(f'{srv} tr[data-tool="search_notes"]')
            assert pg.text_content(f'{srv} tr[data-tool="search_notes"] .badge') == "lectura"
            assert pg.text_content(f'{srv} tr[data-tool="create_note"] .badge') == "escritura"
            assert pg.query_selector(f'{srv} tr[data-tool="inject"] img') is None
            pg.select_option(f'{srv} tr[data-tool="inject"] select', "disabled")
            pg.wait_for_selector(f'{srv} tr[data-tool="inject"] .badge:text-is("desactivada")')
            pg.click(f"{srv} button:text-is('Permitir escrituras (con confirmación)')")
            pg.wait_for_selector(f"{srv} [data-testid=mcp-permission]:text-is('lectura y escritura')")

            q = mcp_hub.invoke("notes", "create_note", {"title": "desde el agente"})
            pg.reload()
            item = f'#pending-list li[data-action-id="{q["action_id"]}"]'
            pg.wait_for_selector(item)
            assert not out.exists()
            pg.click(f"{item} button:text-is('Confirmar y ejecutar')")
            pg.wait_for_selector("#pending-empty:not([hidden])")
            assert out.exists()
            pg.click(f"{srv} button:text-is('Eliminar')")
            pg.wait_for_selector("#mcp-list p.hint:text-is('No hay servidores MCP.')")
        finally:
            browser.close()
            for sp in registry.all_specs():
                if sp.id.startswith("mcp-"):
                    registry.unregister(sp.id)
    assert errors == []
