"""The catalogue's "+" has to open the connection the app already knows how to do.

This test exists because the catalogue nearly shipped a button that could not
work: it opened a personal-token form for every connector, including the six
that use a plain token (``auth="token"``) and whose token goes to a different
endpoint. The form appeared, the user pasted a token, and the request was
rejected with "cannot be connected with a token". A button that looks like it
connects and does not is worse than no button.

So the catalogue has to reuse the connection flow each connector already has:

- a token connector opens the token form the settings card uses,
- an OAuth connector with a client id starts the consent flow,
- an OAuth connector without one says so and opens the settings.

And the catalogue has to be a catalogue: a tile per app, with its name and
the line that says what it does.

End-to-end in headless Chrome against the real local server. No network.
"""
from __future__ import annotations

import os
import shutil
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest

REQUIRED = "1" in (os.environ.get("CONNECTORS_BROWSER_REQUIRED"), os.environ.get("LEARNING_BROWSER_REQUIRED"))
CHROME = (os.environ.get("CHROME_BIN") or shutil.which("google-chrome")
          or shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chromium-browser"))
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None  # type: ignore[assignment]
if not REQUIRED:
    pytestmark = pytest.mark.skipif(sync_playwright is None or not CHROME, reason="no Playwright/Chrome")


@pytest.fixture
def site(tmp_path, monkeypatch):
    assert sync_playwright is not None and CHROME, "Playwright and Chrome are required"
    real_connect = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, addr, *a, **k)
        raise AssertionError(f"network used: {addr}")

    monkeypatch.setattr(socket.socket, "connect", guarded)

    from ops_dashboard import connector_registry as registry
    from ops_dashboard import connector_tokens as tokens
    from ops_dashboard import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "c.db")
    db.init_db()
    prev_b = tokens.use_backend(tokens.MemoryBackend())

    # A token connector lives in two registries: the spec catalogue (the
    # page draws the tile from it) and the token registry (the form comes
    # from it). A test that registers only one of them is testing a
    # connector that does not exist.
    from ops_dashboard.connectors_token import base as token_base

    spec = registry.ConnectorSpec(
        id="faketok", name="FakeToken", auth="token",
        allowed_hosts=("api.faketok.test",),
        description="Issues and pull requests",
        description_es="Incidencias y pull requests",
        category="work", brand="#123456",
        actions={"sync": registry.ActionSpec(
            "sync", "read", "Import things", lambda c, p: {"items": []},
            input_schema={"type": "object", "properties": {}, "required": []})})
    token_base.register_connector(token_base.TokenConnector(
        spec=spec, token_mode="keychain",
        whoami=lambda c: "fake-account",
        sync=lambda c, cfg: []))
    registry.register(spec)

    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        registry.unregister(spec.id)
        token_base._NATIVE.pop(spec.id, None)
        tokens.use_backend(prev_b)


def test_the_catalogue_tile_connects_the_way_the_app_connects(site):
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            ctx = browser.new_context(locale="es-ES")
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            pg.on("console", lambda m: errors.append(f"console: {m.text}")
                  if "Content Security Policy" in m.text
                  or (m.type == "error" and "Failed to load resource" not in m.text)
                  else None)
            pg.on("response", lambda r: errors.append(f"http {r.status} {r.url}")
                  if r.status >= 400 and not r.url.endswith("/favicon.ico") else None)

            pg.goto(site + "/connectors")
            tile = '#catalog-list [data-connector="faketok"]'
            pg.wait_for_selector(tile)

            # It is a catalogue: the name, and the line that says what it does.
            assert "FakeToken" in (pg.text_content(tile) or "")
            assert "Incidencias y pull requests" in (pg.text_content(tile) or "")

            # The "+" opens the token form — the one this connector uses.
            pg.click(f'{tile} [data-testid="catalog-connect"]')
            pg.wait_for_selector(f'{tile} [data-testid="token-input"]')

            # And the form posts to the endpoint that works for a token
            # connector. If it posted to the personal-token route, the
            # provider would answer "cannot be connected with a token".
            pg.fill(f'{tile} [data-testid="token-input"]', "not-a-real-token")
            pg.click(f'{tile} [data-testid="token-submit"]')
            pg.wait_for_timeout(1200)
            body = pg.text_content("body") or ""
            assert "cannot be connected with a token" not in body, body[:600]

            assert not errors, errors
        finally:
            browser.close()


def test_the_search_finds_the_app(site):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            ctx = browser.new_context(locale="es-ES")
            pg = ctx.new_page()
            pg.goto(site + "/connectors")
            pg.wait_for_selector('#catalog-list .app-card')
            before = pg.locator('#catalog-list .app-card').count()
            pg.fill("#catalog-search", "faketoken")
            pg.wait_for_timeout(400)
            after = pg.locator('#catalog-list .app-card').count()
            assert after < before, (before, after)
        finally:
            browser.close()
