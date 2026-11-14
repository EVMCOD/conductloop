"""The "Now" view in a real browser, in the language the person chose.

The panel is where the day begins, so the view has to hold up in the browser:
it opens on what needs the person, it says it in their language, and every
item takes them to the place it came from.

Skipped when Playwright or Chrome is missing, unless one of the variables the
CI browser step sets is on: the step runs every tests/test_*ui_browser.py, so
this file has to join it (and not sit out the run in silence).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import urllib.request
from datetime import datetime

import pytest

REQUIRED = "1" in (os.environ.get("NOW_BROWSER_REQUIRED"),
                   os.environ.get("LEARNING_BROWSER_REQUIRED"),
                   os.environ.get("CONNECTORS_BROWSER_REQUIRED"))
CHROME = (os.environ.get("CHROME_BIN") or shutil.which("google-chrome")
          or shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chromium-browser"))
try:
    from playwright.sync_api import TimeoutError as BrowserTimeout
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None  # type: ignore[assignment]
    BrowserTimeout = None  # type: ignore[assignment, misc]
if not REQUIRED:
    pytestmark = pytest.mark.skipif(sync_playwright is None or not CHROME,
                                    reason="no Playwright/Chrome")

# The panel has loaded its data: the app is up.
PAINTED = "() => typeof _allData !== 'undefined' && !!_allData.stats"
# The "Now" view paints "Cargando…" and then its answer: the items are what
# is in the DOM when it is done.
NOW_ITEM = "#main-content .now-item"
# Chrome sometimes sits on a navigation for a long time without sending
# anything at all: the machine is busy, not the panel. The panel answers in
# milliseconds, so a stalled navigation gets a second page instead of a
# failed test — and only a panel that stays blank is a real failure.
NAV_ATTEMPTS = 3
NAV_TIMEOUT = 20_000
PAINT_TIMEOUT = 60_000


@pytest.fixture(scope="module")
def browser():
    """One Chrome for the whole file.

    Starting a browser is slow, and every start is another chance for the
    browser to misbehave. The contexts are per test, so each one starts from
    a clean slate."""
    assert sync_playwright is not None and CHROME, "Playwright and Chrome are required"
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def site(tmp_path, monkeypatch):
    """The panel, with the database a returning person would have."""
    assert sync_playwright is not None and CHROME, "Playwright and Chrome are required"
    # The panel talks to nothing but this machine.
    real_connect = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, addr, *a, **k)
        raise AssertionError(f"network used: {addr}")

    monkeypatch.setattr(socket.socket, "connect", guarded)

    from ops_dashboard import brain, db, migrations
    from ops_dashboard.indexers import live_urls

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "now.db")
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    db.init_db(backup_before_upgrade=False)
    with db.get_connection() as conn:
        migrations.run_migrations(conn)
    live_urls.ensure_schema()

    from ops_dashboard.server import DashboardHandler, _LocalHTTPServer

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    # The app's own server: it is the one that has to survive a browser
    # opening its connections all at once.
    srv = _LocalHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _choose_language(base: str, locale: str) -> None:
    req = urllib.request.Request(
        base + "/api/locale",
        data=json.dumps({"locale": locale}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        assert json.loads(r.read())["locale"] == locale


def _watch(pg, errors: list[str]) -> None:
    """Collect what the page got wrong.

    A console error about a resource that failed to load is not counted here:
    the response listener below catches those with their URL. The browser
    asks for /favicon.ico on its own — the panel has never had one."""
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errors.append(f"console: {m.text}")
          if m.type == "error" and "Failed to load resource" not in m.text else None)
    pg.on("response", lambda r: errors.append(f"http {r.status} {r.url}")
          if r.status >= 400 and not r.url.endswith("/favicon.ico") else None)


def _today() -> int:
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _returning(site: str) -> None:
    """Somebody who already installed the app: the panel, not the welcome page."""
    from ops_dashboard import onboarding

    onboarding.mark("completed")
    assert onboarding.state()["fresh"] is False


def _open(browser, site: str, errors: list[str]):
    """Open the panel in a page that navigates.

    A browser that does not navigate is not the panel failing: the panel
    answers in milliseconds. So a navigation that never starts gets a new
    page, and only a panel that stays blank is a failure."""
    for attempt in range(1, NAV_ATTEMPTS + 1):
        pg = browser.new_context(locale="es-ES").new_page()
        _watch(pg, errors)
        try:
            pg.goto(site, timeout=NAV_TIMEOUT, wait_until="commit")
            return pg
        except Exception as e:
            if BrowserTimeout is None or not isinstance(e, BrowserTimeout):
                raise
            # A stalled browser says nothing about the panel: start over.
            pg.close()
            errors.clear()
            if attempt == NAV_ATTEMPTS:
                raise
    raise AssertionError("unreachable")


def _painted(pg) -> None:
    """Wait until the panel has loaded its data."""
    pg.wait_for_function(PAINTED, timeout=PAINT_TIMEOUT)


def _now_painted(pg) -> None:
    """Wait until the "Now" view has painted its answer."""
    _painted(pg)
    pg.wait_for_selector(NOW_ITEM, timeout=PAINT_TIMEOUT)


def test_the_server_waits_for_a_whole_browser_burst(site):
    """Chrome opens one connection per resource and six of them at once.

    The queue of connections waiting to be accepted has to be deeper than the
    burst: when it was not (the default is five), the panel loaded half of
    its scripts, one request died on the way and the person was left looking
    at "cargando..." for ever."""
    from ops_dashboard.server import _LocalHTTPServer

    assert _LocalHTTPServer.request_queue_size >= 64


def test_the_panel_opens_on_what_needs_the_person(browser, site):
    """Something is waiting, so the app opens on it and says what it is."""
    from ops_dashboard import db

    db.add_approval(1, "publish_release", {"note": "ship it"}, reason="publish 1.2.16")
    db.add_user_todo("renew the domain", due_date=_today())
    _returning(site)
    errors: list[str] = []

    pg = _open(browser, site, errors)
    try:
        _now_painted(pg)
        assert pg.evaluate("() => _view") == "now"
        body = pg.inner_text("#main-content")
        assert "publish 1.2.16" in body
        assert "renew the domain" in body
        assert errors == []
    finally:
        pg.close()


def test_the_view_speaks_the_language_the_person_chose(browser, site):
    from ops_dashboard import db

    db.add_user_todo("renew the domain", due_date=_today())
    _returning(site)
    errors: list[str] = []

    _choose_language(site, "es")
    pg = _open(browser, site, errors)
    try:
        _now_painted(pg)
        body = pg.inner_text("#main-content").lower()
        # The words of the view are the ones of the app, not the browser.
        assert "para hoy" in body
        assert "renovar el dominio" not in body   # the todo is the person's own
        assert errors == []
    finally:
        pg.close()


def test_an_item_takes_the_person_to_where_it_lives(browser, site):
    from ops_dashboard import db

    db.add_approval(1, "publish_release", {"note": "ship it"}, reason="publish 1.2.16")
    _returning(site)
    errors: list[str] = []

    pg = _open(browser, site, errors)
    try:
        _now_painted(pg)
        pg.wait_for_selector('[data-now-view="approvals"]', timeout=PAINT_TIMEOUT)
        pg.click('[data-now-view="approvals"]')
        # The approvals view is the one that can approve a proposal.
        pg.wait_for_selector("[data-approval-approve]", timeout=PAINT_TIMEOUT)
        assert errors == []
    finally:
        pg.close()


def test_nothing_waiting_says_so(browser, site):
    """An empty day is a real answer, not a blank page."""
    _returning(site)
    errors: list[str] = []

    _choose_language(site, "es")
    pg = _open(browser, site, errors)
    try:
        _painted(pg)
        # Nothing waits: the panel stays where it was, and the badge is gone.
        assert pg.evaluate("() => _view") == "files"
        badge = pg.evaluate("() => { const b = document.getElementById('now-badge');"
                            " return b ? getComputedStyle(b).display : 'none'; }")
        assert badge == "none"
        assert errors == []
    finally:
        pg.close()


def test_a_signal_alone_is_enough_to_open_the_view(browser, site, monkeypatch):
    """Nothing blocks, but something is wrong: the panel still opens on the
    answer, because that is the news."""
    from ops_dashboard.indexers import anomalies

    monkeypatch.setattr(
        anomalies, "detect_recent_anomalies",
        lambda: {"anomalies": [{"kind": "revenue_total", "severity": "high",
                                "message": "revenue down 30% vs last week"}]})
    _returning(site)
    errors: list[str] = []

    pg = _open(browser, site, errors)
    try:
        _now_painted(pg)
        body = pg.inner_text("#main-content")
        assert "revenue down 30% vs last week" in body
        assert pg.evaluate("() => _view") == "now"
        assert errors == []
    finally:
        pg.close()
