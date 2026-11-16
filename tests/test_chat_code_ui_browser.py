"""The chat page follows a code session it started, in a real browser.

A coding task sent in the chat opens a code session; the page shows a card
that follows it (steps, state, report), shows its changes, and pushes its
branch when the person clicks — here to a local bare repository standing in
for GitHub.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from test_sessions_ui_browser import CHROME, GIT_ENV, J, Mock, _app_server, g, wait_js  # noqa: E402

from ops_dashboard import brain, routes_sessions, team_chat  # noqa: E402
from ops_dashboard.sessions import providers  # noqa: E402
from ops_dashboard.sessions import worktrees as W  # noqa: E402

pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome/Chromium binary")

TASK = "Add a notes.txt file that says hello, with a test in tests/test_notes.py, and commit."


class _Chat(brain.Provider):
    name = "fake"
    model = "m"

    def ask(self, system, messages, **kw):
        return "CODE"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "ui.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    monkeypatch.setenv(W.WORKTREES_ENV, str(tmp_path / "wt"))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    cfg = {"default_provider": "minimax", "minimax_base_url": brain.MINIMAX_CODING_PLAN_URL}
    monkeypatch.setattr(brain, "_load_config", lambda: dict(cfg))
    monkeypatch.setattr(team_chat.brain, "resolve_provider", lambda *a, **k: _Chat())
    monkeypatch.setattr(team_chat.brain, "_retrieve", lambda *a, **k: [])
    repo = tmp_path / "mosaic"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("V = 1\n")
    g(repo, "add", ".")
    g(repo, "commit", "-q", "-m", "init")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True,
                   env={**os.environ, **GIT_ENV})
    g(repo, "remote", "add", "origin", str(remote))

    cache: dict[int, Mock] = {}

    def factory(s):
        # One script per session: the runner asks the factory on every turn.
        if s["id"] not in cache:
            cache[s["id"]] = Mock([J("claim", patterns=["notes.txt"]),
                                   J("write_file", path="notes.txt", content="hello\n"),
                                   J(final="Added **notes.txt** with a greeting.")])
        return cache[s["id"]]

    providers.reset_cooldowns()
    routes_sessions.configure(provider_factory=factory, tools_kwargs={"python": sys.executable})
    routes_sessions._store().mark_repo(str(repo))
    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = _app_server(("127.0.0.1", port), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{port}", "remote": remote, "shots": tmp_path}
    sup = routes_sessions.supervisor()
    for sid in list(sup.running()):
        sup.stop(sid)
    routes_sessions.configure()
    server.shutdown()
    providers.reset_cooldowns()


@pytest.mark.parametrize("locale", ["en-US", "es-ES"])
def test_the_chat_follows_the_session_and_pushes_its_branch(env, locale, monkeypatch):
    es = locale.startswith("es")
    from ops_dashboard import i18n
    i18n.set_request_language(None)
    i18n.set_locale("es" if es else "en")
    events = list(team_chat.ChatTurn("brain", TASK).events())
    card = next(e for e in events if e["type"] == "code_session")
    thread_id = events[0]["thread_id"]
    errors: list[str] = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
        page = browser.new_context(locale=locale, viewport={"width": 1280, "height": 1000}).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{env['base']}/chat?agent=brain&thread={thread_id}")
        page.wait_for_selector(".code-card")
        assert page.text_content(".code-title").startswith(
            ("Sesión de código #" if es else "Code session #") + str(card["session_id"]))
        wait_js(page, "!document.querySelector('.code-actions').hidden", timeout=40)
        state = page.text_content(".code-state")
        assert state == ("Terminada · espera tu revisión" if es else "Finished · waiting for your review")
        assert "notes.txt" in page.text_content(".code-report")
        assert "notes.txt" in page.text_content(".code-steps")
        page.click(".code-diff-btn")
        wait_js(page, "!document.querySelector('.code-diff').hidden")
        assert "+hello" in page.text_content(".code-diff")
        page.screenshot(path=str(env["shots"] / f"chat-code-{locale}.png"), full_page=True)
        page.click(".code-publish-btn")
        wait_js(page, "document.querySelector('.code-publish-btn').disabled && "
                      "/notes|subidos|Pushed/.test(document.querySelector('.code-publish-btn').textContent)")
        assert page.is_hidden(".code-error")
        browser.close()
    assert not errors, errors
    branch = card["branch"]
    out = subprocess.run(["git", "--git-dir", str(env["remote"]), "rev-parse", "--verify", branch],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
