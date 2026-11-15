"""The organisation can be walked with the keyboard, and found by name.

The panel of agents (the tree of the 1.2.25-1.2.27) had ``role="tree"``
and ``role="treeitem"``, but nothing answered the keyboard: the canvas
could not be traversed without a mouse, and with 43 people in a real
install there was no way to jump to one either. Two more things were
missing: a dropped member could not be given a boss from the path above
the tree (so nobody could be taken out of a branch), and "New agent"
asked for a team id as a NUMBER (2026-11-15, prompt-minimax-2026-11-15
P1-1, P1-4).

This test drives a real browser -- Chrome and WebKit -- because focus
moving is the whole of the defect, and a fake DOM cannot show it. It only
talks to its own server on 127.0.0.1.
"""
from __future__ import annotations

import os
import shutil
import socket
import threading

import pytest

REQUIRED = "1" in (os.environ.get("CONNECTORS_BROWSER_REQUIRED"), os.environ.get("LEARNING_BROWSER_REQUIRED"))
CHROME = (os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
          or shutil.which("chromium") or shutil.which("chromium-browser")
          or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None  # type: ignore[assignment]
if not REQUIRED:
    pytestmark = pytest.mark.skipif(sync_playwright is None or not CHROME, reason="no Playwright/Chrome")


def _app_server(address, handler):
    """The app's own server class. The stdlib default queues 5 connections and
    a page loads more files than that, so the extra ones were dropped."""
    from ops_dashboard.server import _LocalHTTPServer
    return _LocalHTTPServer(address, handler)


@pytest.fixture
def org(tmp_path, monkeypatch):
    """A server with a small organisation, and the ids of its people."""
    assert sync_playwright is not None and CHROME, "Playwright and Chrome are required"
    dbp = tmp_path / "c.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    from ops_dashboard import db_teams
    db_teams.seed_default_teams()
    team_id = db_teams.list_teams()[0]["id"]
    # Invented names (Mosaic, Tidepool, Acme): no real company, no real person.
    ceo = db_teams.add_member(team_id=team_id, name="Ada Mosaic", type="ai_agent", level="ceo")
    lead = db_teams.add_member(team_id=team_id, name="Bo Tidepool", type="ai_agent", level="vp", manager_id=ceo)
    worker = db_teams.add_member(team_id=team_id, name="Cy Acme", type="ai_agent", level="agent", manager_id=lead)
    peer = db_teams.add_member(team_id=team_id, name="Dee Mosaic", type="human", level="agent", manager_id=ceo)
    # The dashboard sends a fresh install to /welcome. One indexed document is
    # enough to say "this is not a first run".
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO documents (source, path, title, content, indexed_at) VALUES (?,?,?,?,?)",
            ("test", "/tmp/mosaic.md", "Mosaic", "Mosaic", 0),
        )

    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = _app_server(("127.0.0.1", port), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "ceo": ceo, "lead": lead, "worker": worker, "peer": peer,
        }
    finally:
        srv.shutdown()
        srv.server_close()


def _open(page, org):
    """The dashboard, on the panel of agents, with the diagram at rest.

    The panel is rebuilt when the data arrives, and the diagram that was
    there is replaced with it: anything that drives the tree waits for the
    drawing to settle (the canvas carries the number of the drawing).
    """
    page.goto(org["url"], wait_until="domcontentloaded")
    page.evaluate("setView('agents')")
    page.wait_for_selector("#org-canvas", timeout=20000)
    page.wait_for_selector(".org-node", timeout=20000)
    _settled(page)
    return page


def _settled(page):
    """Wait until the diagram is not being redrawn."""
    page.wait_for_function(
        "() => { const c = document.getElementById('org-canvas');"
        " if (!c) return false;"
        " const now = c.getAttribute('data-render');"
        " if (window.__lastRender === now) return true;"
        " window.__lastRender = now; return false; }",
        timeout=20000,
    )


# Where is the keyboard? These are the questions the test asks the page, and
# they have to survive the canvas not being there: the panel is rebuilt when
# the data arrives, and for a moment there is no canvas at all. Asking a
# missing canvas for its attribute throws inside the page, and Playwright
# reports that as a page error -- which the test then reads as a bug of the
# app. It was the test's own question that broke (once in eight runs, on a
# loaded machine), not the app.
_ACTIVE = ("() => { const c = document.getElementById('org-canvas');"
           " return c ? c.getAttribute('aria-activedescendant') : null; }")
_ACTIVE_MOVED = ("(prev) => { const c = document.getElementById('org-canvas');"
                 " return c ? c.getAttribute('aria-activedescendant') !== prev : false; }")
_ACTIVE_BACK = ("(prev) => { const c = document.getElementById('org-canvas');"
                " return c ? c.getAttribute('aria-activedescendant') === prev : false; }")


def _press_until(page, key, condition, tries=3, arg=None):
    """Press a key on the tree until the tree answers.

    The panel is rebuilt when the data arrives, and on a loaded machine the
    rebuild can land between the press and the answer: the key reaches a
    canvas that is no longer there. The tree is asked again.

    ``condition`` is a function of ``arg``, so a step can say "the focus is
    not where it was".
    """
    for _ in range(tries):
        page.locator("#org-canvas").press(key)
        try:
            page.wait_for_function(condition, arg=arg, timeout=4000)
            return True
        except Exception:
            continue
    return False


def _engine(name):
    """The browser engines this test runs on."""
    return ["chromium", "webkit"]


def _launch(p, engine):
    """The browser, or a skip when this engine is not installed here.

    Chrome is the one the CI runs on (the runner's preinstalled one);
    WebKit is Playwright's own, and it is what the desktop app uses on
    macOS. A machine without it skips that engine instead of failing.
    """
    if engine == "chromium":
        return p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    try:
        return p.webkit.launch()
    except Exception as e:  # pragma: no cover - depends on the machine
        pytest.skip(f"WebKit is not installed here: {e}")


@pytest.mark.parametrize("engine", _engine("chromium"))
def test_the_tree_is_walked_with_the_keyboard(org, engine):
    errors: list[str] = []
    with sync_playwright() as p:
        browser = _launch(p, engine)
        try:
            pg = browser.new_context(locale="es-ES").new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            _open(pg, org)

            # The tree takes the focus and the first arrow puts it on a node.
            # locator.press() focuses the element and presses in one go: the
            # panel is re-rendered when the data arrives, and the element the
            # focus was on is replaced with it.
            assert _press_until(pg, "ArrowDown", _ACTIVE), \
                "the tree did not answer the keyboard"
            first = pg.get_attribute("#org-canvas", "aria-activedescendant")
            assert first, "the tree did not answer the keyboard"

            # Down moves to the next node.
            assert _press_until(pg, "ArrowDown", _ACTIVE_MOVED, arg=first), \
                "the tree did not move down"
            second = pg.get_attribute("#org-canvas", "aria-activedescendant")
            assert second != first

            # Up goes back to the node the keyboard came from.
            assert _press_until(pg, "ArrowUp", _ACTIVE_BACK, arg=first), \
                "the tree did not go back up"
            assert pg.get_attribute("#org-canvas", "aria-activedescendant") == first

            # Enter opens the node the keyboard is on: its card appears and
            # the diagram shows its branch.
            assert _press_until(pg, "Enter", "() => !!document.getElementById('agent-detail-panel')"), \
                "Enter did not open the agent"
            assert pg.locator(".org-path-item").count() > 0, "the path above the branch is missing"

            # Escape closes the card and goes back to the whole organisation.
            assert _press_until(pg, "Escape", "() => !document.getElementById('agent-detail-panel')"), \
                "Escape did not close the card"
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize("engine", _engine("chromium"))
def test_an_agent_can_be_found_by_name(org, engine):
    errors: list[str] = []
    with sync_playwright() as p:
        browser = _launch(p, engine)
        try:
            pg = browser.new_context(locale="es-ES").new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            _open(pg, org)

            pg.fill("#agent-search", "Tidepool")
            # The names are read in one go: the panel is re-rendered when the
            # data arrives, and two queries can land on two different panels.
            names = pg.wait_for_function(
                "() => { const els = document.querySelectorAll('.org-search-item');"
                " return els.length ? Array.from(els).map(function(e) { return e.textContent; }) : null; }",
                timeout=20000,
            ).json_value()
            assert any("Tidepool" in n for n in names), names

            # Choosing one does what pressing Enter on it does.
            pg.click(".org-search-item")
            pg.wait_for_selector("#agent-detail-panel", timeout=20000)
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize("engine", _engine("chromium"))
def test_a_dropped_member_takes_the_boss_of_the_path(org, engine):
    """Inside a branch, nobody could be taken out of it.

    Dropping a member on a name of the path above the tree gives it that
    boss. The drop asks first (the same question as any other reparent),
    and the answer is "yes" here.
    """
    errors: list[str] = []
    with sync_playwright() as p:
        browser = _launch(p, engine)
        try:
            pg = browser.new_context(locale="es-ES").new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            _open(pg, org)

            # Edit mode: the members can be dragged.
            pg.click("#agent-edit-toggle")
            pg.wait_for_selector('.org-node[draggable="true"]', timeout=20000)

            # Open a branch, so the path above the tree appears.
            pg.click(f'.org-node[data-member-id="{org["worker"]}"]')
            pg.wait_for_selector(".org-path-item", timeout=20000)
            assert pg.locator(".org-path-item").count() > 0

            # The member is dragged onto the name of the boss in the path.
            source = f'.org-node[data-member-id="{org["worker"]}"]'
            target = f'.org-path-item[data-org-focus="{org["lead"]}"]'
            pg.drag_and_drop(source, target)
            pg.wait_for_selector("#confirm-dialog[open]", timeout=20000)
            pg.click("#confirm-yes")
            pg.wait_for_timeout(1500)

            m = pg.evaluate(
                "async (id) => (await fetch('/api/members/' + id)).json()",
                org["worker"],
            )
            assert m.get("manager_id") == org["lead"], m
            assert not errors, errors
        finally:
            browser.close()
