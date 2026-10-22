"""End-to-end check of /sessions in headless Chrome against the REAL local
server, with scripted MockProviders (no network, no real keys).

Walks the whole flow through the UI in es-ES and en-US: mark repo, set price,
create session, add task, run, needs_review, diff, accept (no merge), budget
pause + resume, stalled resume with explicit confirmation, claims release,
paginated events/messages, sending a message, provider_unavailable rendering,
and that hostile model output (<script>, <img onerror>) is shown as text and
never executes. Screenshots go to $SESSIONS_SCREENS (default: pytest tmp dir).

Skipped when Playwright or a Chrome/Chromium binary is not available (CI).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from ops_dashboard import brain, routes_sessions  # noqa: E402
from ops_dashboard.sessions import providers  # noqa: E402
from ops_dashboard.sessions import worktrees as W  # noqa: E402

CHROME = (os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
          or shutil.which("chromium-browser"))
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome/Chromium binary")

FAKE = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE0123456789"
XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


def J(tool=None, final=None, **args):
    return json.dumps({"final": final} if final is not None else {"tool": tool, "args": args})


class Mock(brain.MockProvider):
    def __init__(self, replies, *, usage=None):
        self.model = "m"
        self.replies, self.usage = list(replies), usage

    def ask(self, system, messages, **kw):
        self.last_usage = self.usage
        return self.replies.pop(0) if self.replies else J(final="done")


def scripts(role):
    if role.startswith("backend"):
        return Mock([J("claim", patterns=["feature.py"]),
                     J("write_file", path="feature.py",
                       content=f"def f():\n    return 42\n# {XSS}\n"),
                     J(final=f"added f() {XSS}")],
                    usage={"input_tokens": 1000, "output_tokens": 2000, "source": "input_output"})
    if role.startswith("budget"):
        # 40 cents per turn at 10000 c/Mtok out → pauses on a 0.50 EUR budget.
        return Mock([J("list_dir")] * 3 + [J(final="finished after resume")],
                    usage={"input_tokens": 0, "output_tokens": 4000, "source": "input_output"})
    raise providers.ProviderUnavailable("anthropic", "cooldown", "anthropic is cooling down", 30)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "ui.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    monkeypatch.setenv(W.WORKTREES_ENV, str(tmp_path / "wt"))
    monkeypatch.setenv("OPENAI_API_KEY", FAKE)
    from ops_dashboard import db, secrets_store
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: FAKE)
    db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("V = 1\n")
    g(repo, "add", ".")
    g(repo, "commit", "-q", "-m", "init")
    cache: dict[int, Mock] = {}

    def factory(s):
        if s["id"] not in cache:
            cache[s["id"]] = scripts(s["role"])
        return cache[s["id"]]

    providers.reset_cooldowns()
    routes_sessions.configure(provider_factory=factory, tools_kwargs={"python": sys.executable})
    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shots = Path(os.environ.get("SESSIONS_SCREENS") or tmp_path / "screens")
    shots.mkdir(parents=True, exist_ok=True)
    yield {"base": f"http://127.0.0.1:{port}", "repo": repo, "shots": shots,
           "store": routes_sessions.supervisor().store}
    sup = routes_sessions.supervisor()
    for sid in list(sup.running()):
        sup.stop(sid)
    routes_sessions.configure()
    server.shutdown()
    providers.reset_cooldowns()


def _wait(fn, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("timed out")


def wait_js(page, expr, timeout=25.0):
    """Poll an expression (page.wait_for_function would need 'unsafe-eval',
    which the page's CSP forbids — on purpose)."""
    _wait(lambda: page.evaluate(expr), timeout)


@pytest.mark.parametrize("locale", ["es-ES", "en-US"])
def test_sessions_ui_full_flow(env, locale):
    es = locale.startswith("es")
    tag = "es" if es else "en"
    shots, store = env["shots"], env["store"]
    errors: list[str] = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(locale=locale, viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        bodies: list[str] = []

        def keep(resp):
            if "/api/sessions" in resp.url:
                try:
                    bodies.append(resp.text())
                except Exception:
                    pass
        page.on("response", keep)
        resp = page.goto(env["base"] + "/sessions")
        assert resp is not None and "script-src 'self'" in resp.headers["content-security-policy"]
        page.wait_for_selector("body[data-ready='1']")
        assert page.evaluate("document.documentElement.lang") == tag
        assert page.text_content("h1") == ("Sesiones de agentes" if es else "Agent sessions")

        # repo + price
        page.fill("#repo-path", str(env["repo"] / "nope"))
        page.click("#repo-form button")
        page.wait_for_selector("#repo-error[role=alert]")
        page.fill("#repo-path", str(env["repo"]))
        page.click("#repo-form button")
        page.wait_for_selector("#repos code.path")
        page.select_option("#price-provider", "openai")
        page.fill("#price-model", "m")
        page.fill("#price-in", "0")
        page.fill("#price-out", "100")
        page.click("#price-form button")
        wait_js(page, "document.querySelector('#prices').textContent.includes('100')")

        # create session: validation error first (missing key_ref for openai)
        page.fill("#s-role", "backend-" + tag)
        page.select_option("#s-provider", "openai")
        page.fill("#s-model", "m")
        page.fill("#s-budget", "5")
        page.click("#session-form button[type=submit]")
        page.wait_for_selector("#session-error[data-error-code='validation_error']")
        page.fill("#s-keyref", "byok.openai.work")
        page.click("#session-form button[type=submit]")
        page.wait_for_selector("#detail:not([hidden])")
        sid = int(page.get_attribute(".session.selected", "data-sid"))

        # task, start, needs_review
        page.fill("#t-title", "feature " + XSS)
        page.fill("#t-prompt", "add f()")
        page.fill("#t-paths", "feature.py")
        page.click("#task-form button")
        page.wait_for_selector("#tasks li.task[data-status='queued']")
        page.click(f".session[data-sid='{sid}'] button[data-act='start']")
        page.wait_for_selector("#tasks li.task[data-status='needs_review']", timeout=30000)
        # model message the user sees in the channel (hostile on purpose)
        store.post_message(kind="info", body="model says " + XSS, from_session=sid, to_session=None)
        page.click("#tasks li.task[data-status='needs_review'] button[data-act='review']")
        page.wait_for_selector("#diff .ln.add")
        diff_text = page.text_content("#diff")
        assert "+    return 42" in diff_text and XSS in diff_text
        assert page.is_visible("#review .warn")
        assert ("NO fusiona" if es else "does NOT merge") in page.text_content("#review .warn")
        page.wait_for_selector("#inbox li.msg")
        page.screenshot(path=str(shots / f"sessions-{tag}-review-diff.png"), full_page=True)

        # accept (dialog) → done, nothing merged
        page.click("#review-accept")
        page.wait_for_selector("#confirm-dialog[open]")
        assert ("NO fusiona" if es else "does NOT merge") in page.text_content("#confirm-dialog p")
        page.click("#confirm-yes")
        page.wait_for_selector("#tasks li.task[data-status='done']")
        assert "merged: false" in page.text_content("#detail-error")
        assert g(env["repo"], "rev-list", "--count", "main").strip() == "1"

        # events pagination: plant 70 events, the feed pages by 50
        for i in range(70):
            store.record_event(sid, "test.filler", {"i": i, "note": XSS})
        page.click(f".session[data-sid='{sid}'] button[data-act='open']")
        wait_js(page, "document.querySelectorAll('#events li.ev').length >= 50")
        _wait(lambda: page.is_visible("#events-more"))
        n1 = page.eval_on_selector_all("#events li.ev", "els => els.length")
        page.click("#events-more")
        wait_js(page, f"document.querySelectorAll('#events li.ev').length > {n1}")

        # message to the session (hostile text stays text)
        page.fill("#m-body", "user says " + XSS)
        page.click("#message-form button")
        wait_js(page, "[...document.querySelectorAll('#messages li .body')]"
                               ".some(e => e.textContent.startsWith('user says <img'))")

        # budget pause + resume with a higher budget
        page.fill("#s-role", "budget-" + tag)
        page.fill("#s-keyref", "byok.openai.work")
        page.fill("#s-budget", "0.5")
        page.fill("#s-iters", "")
        page.click("#session-form button[type=submit]")
        wait_js(page, f"document.querySelector('.session.selected')?.dataset.sid != '{sid}'")
        sid2 = int(page.get_attribute(".session.selected", "data-sid"))
        page.fill("#t-title", "explore")
        page.click("#task-form button")
        page.wait_for_selector("#tasks li.task")
        page.click(f".session[data-sid='{sid2}'] button[data-act='start']")
        page.wait_for_selector(f".session[data-sid='{sid2}'][data-status='paused']", timeout=30000)
        page.click(f".session[data-sid='{sid2}'] button[data-act='resume']")
        page.wait_for_selector("#resume-form:not([hidden])")
        page.screenshot(path=str(shots / f"sessions-{tag}-budget-paused.png"), full_page=True)
        page.fill("#r-budget", "0.6")           # still not enough? server decides
        page.fill("#r-budget", "20")
        page.click("#resume-form button[type=submit]")
        page.wait_for_selector("#tasks li.task[data-status='needs_review']", timeout=30000)
        assert page.is_hidden("#resume-form")

        # claims: plant one and release it from the UI
        store.claim_paths(sid2, ["docs/*.md"])
        page.click(f".session[data-sid='{sid2}'] button[data-act='open']")
        page.wait_for_selector("#claims li.claim")
        page.click("#claims li.claim button")
        wait_js(page, "!document.querySelector('#claims li.claim')")

        # stalled: needs the checkbox AND the dialog
        store.set_session_status(sid2, "stalled", last_error="no heartbeat")
        page.wait_for_selector(f".session[data-sid='{sid2}'][data-status='stalled']")
        page.click(f".session[data-sid='{sid2}'] button[data-act='resume']")
        page.wait_for_selector("#r-stalled-wrap:not([hidden])")
        page.click("#resume-form button[type=submit]")
        page.wait_for_selector("#detail-error[data-error-code='confirm_required']")
        page.check("#r-stalled")
        page.click("#resume-form button[type=submit]")
        page.wait_for_selector("#confirm-dialog[open]")
        page.click("#confirm-yes")
        wait_js(page, f"document.querySelector(\".session[data-sid='{sid2}']\")"
                               ".dataset.status !== 'stalled'")

        # provider_unavailable: same rendering as provider-errors.js
        page.fill("#s-role", "down-" + tag)
        page.select_option("#s-provider", "anthropic")
        page.fill("#s-keyref", "byok.anthropic.main")
        page.fill("#s-budget", "1")
        page.click("#session-form button[type=submit]")
        wait_js(page, f"!['{sid}','{sid2}'].includes(document.querySelector('.session.selected')?.dataset.sid)")
        sid3 = int(page.get_attribute(".session.selected", "data-sid"))
        page.click(f".session[data-sid='{sid3}'] button[data-act='start']")
        page.wait_for_selector("#action-error[data-provider-unavailable='anthropic']")
        assert ("El proveedor anthropic no está disponible" if es else
                "Provider anthropic is unavailable") in page.text_content("#action-error")
        page.wait_for_timeout(3500)          # survives a polling refresh
        assert page.is_visible("#action-error[data-provider-unavailable='anthropic']")
        page.screenshot(path=str(shots / f"sessions-{tag}-provider-error.png"), full_page=True)

        # close one session through the dialog
        page.click(f".session[data-sid='{sid3}'] button[data-act='close']")
        page.click("#confirm-yes")
        page.wait_for_selector(f".session[data-sid='{sid3}'][data-status='closed']")

        # hostile content: rendered as text, never executed, no injected nodes
        page.click(f".session[data-sid='{sid}'] button[data-act='open']")
        page.wait_for_selector("#tasks li.task[data-status='done']")
        page.click("#diff-load")
        page.wait_for_selector("#diff .ln.add")
        page.wait_for_timeout(500)
        assert page.evaluate("window.__xss") is None
        assert page.eval_on_selector_all("main img, main script", "els => els.length") == 0
        assert XSS in page.text_content("#tasks")
        assert XSS in page.text_content("#inbox")
        assert "<script>window.__xss=2</script>" in page.text_content("#events")  # JSON-escaped quotes
        page.screenshot(path=str(shots / f"sessions-{tag}-xss-as-text.png"), full_page=True)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(shots / f"sessions-{tag}-overview.png"))

        # key values never reach the page
        assert FAKE not in page.content()
        assert not any(FAKE in b for b in bodies)
        assert errors == [], errors
        browser.close()
