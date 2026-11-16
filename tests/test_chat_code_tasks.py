"""A coding task pasted in the chat runs in a code session.

On 16-nov-2026 a task to change the repository was pasted in the team chat:
the CEO handed it to agents who cannot see the repository, they answered that
they could not, and the turn ended twice in "(max tool iterations reached)".
The chat now recognises such a request and opens a code session for it; the
person pushes the branch with one click when it is done.
"""
from __future__ import annotations

import subprocess

import pytest
from test_sessions_api import GIT_ENV, J, Mock, _task, _until, api  # noqa: F401

from ops_dashboard import brain, code_delegate, routes_sessions, team_chat

TASK = ("Fix the README: the Linux and Windows rows link to an old release. "
        "Add a test in tests/test_readme_links.py and commit.")


# ── recognising a coding task ───────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    TASK,
    "fix the bug in ops_dashboard/indexers/anomalies.py",
    "refactor the code and add unit tests",
    "arregla el código de la función y añade tests",
])
def test_the_filter_sees_a_coding_task(text):
    assert code_delegate.looks_like_code(text)


@pytest.mark.parametrize("text", [
    "How do we launch?", "what was our traffic yesterday", "hola, ¿qué tal?",
    "write a tweet about the release",
])
def test_the_filter_leaves_a_conversation_alone(text):
    assert not code_delegate.looks_like_code(text)


class _Answers(brain.Provider):
    name = "fake"

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.asked = answer, error, 0

    def ask(self, system, messages, **kw):
        self.asked += 1
        if self.error:
            raise self.error
        return self.answer


def test_the_model_decides_only_after_the_filter():
    p = _Answers("CODE")
    assert code_delegate.is_code_task(p, TASK) and p.asked == 1
    assert not code_delegate.is_code_task(p, "How do we launch?") and p.asked == 1   # not even asked
    assert not code_delegate.is_code_task(_Answers("CHAT"), TASK)
    assert code_delegate.is_code_task(_Answers("Code."), TASK)
    # A model that cannot answer leaves the message in the chat.
    assert not code_delegate.is_code_task(_Answers(error=RuntimeError("down")), TASK)
    assert not code_delegate.is_code_task(brain.MockProvider(), TASK)


def test_the_repository_is_the_marked_one(monkeypatch):
    repos = [{"repo_path": "/w/mosaic", "enabled": 1, "updated_at": 5},
             {"repo_path": "/w/tidepool", "enabled": 1, "updated_at": 9}]
    monkeypatch.setattr(code_delegate, "_repos", lambda: repos)
    assert code_delegate.pick_repo("fix the bug in mosaic")["repo_path"] == "/w/mosaic"
    assert code_delegate.pick_repo("fix the bug")["repo_path"] == "/w/tidepool"   # most recent
    monkeypatch.setattr(code_delegate, "_repos", lambda: repos[:1])
    assert code_delegate.pick_repo("anything")["repo_path"] == "/w/mosaic"
    monkeypatch.setattr(code_delegate, "_repos", lambda: [])
    assert code_delegate.pick_repo("anything") is None


def test_the_pull_request_page_is_github_compare():
    url = routes_sessions.compare_url("git@github.com:acme/mosaic.git", "main", "cl/engineer/7")
    assert url == "https://github.com/acme/mosaic/compare/main...cl/engineer/7?expand=1"
    assert routes_sessions.compare_url("https://github.com/acme/mosaic", "main", "cl/x/1").startswith(
        "https://github.com/acme/mosaic/compare/")
    assert routes_sessions.compare_url("/srv/git/mosaic.git", "main", "cl/x/1") is None


# ── the chat hands the task over ────────────────────────────────────────────

@pytest.fixture()
def chat(tmp_path, monkeypatch):
    import ops_dashboard.db as _db

    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "chat.db")
    _db.init_db()
    monkeypatch.setattr(team_chat.brain, "resolve_provider", lambda *a, **k: _Answers("CODE"))
    monkeypatch.setattr(team_chat.brain, "_retrieve", lambda *a, **k: [])
    return monkeypatch


def test_a_coding_task_in_the_chat_opens_a_code_session(chat):
    started = {"session_id": 7, "task_id": 3, "repo_name": "mosaic", "branch": "cl/engineer/7",
               "base_ref": "main", "provider": "minimax", "model": "MiniMax-M3", "flat_rate": True}
    chat.setattr(code_delegate, "start", lambda message: {"ok": True, **started})
    turn = team_chat.ChatTurn("brain", TASK)
    events = list(turn.events())
    kinds = [e["type"] for e in events]
    assert kinds == ["start", "code_session", "delta", "done"]          # no plan, no agents
    assert events[1]["session_id"] == 7 and events[1]["branch"] == "cl/engineer/7"
    assert "mosaic" in events[2]["content"] and "cl/engineer/7" in events[2]["content"]
    thread = team_chat.get_thread(events[0]["thread_id"])
    assert [m["role"] for m in thread["messages"]] == ["user", "code_session", "assistant"]


def test_no_marked_repository_says_how_to_mark_one(chat):
    chat.setattr(code_delegate, "start", lambda message: {"ok": False, "error": "no_repo", "message": "x"})
    events = list(team_chat.ChatTurn("brain", TASK).events())
    text = "".join(e["content"] for e in events if e["type"] == "delta")
    assert "Sessions" in text and "Repositories" in text


def test_a_conversation_still_goes_to_the_team(chat):
    chat.setattr(code_delegate, "start", lambda message: pytest.fail("opened a session for a chat"))
    events = list(team_chat.ChatTurn("brain", "How do we launch?").events())
    assert "code_session" not in [e["type"] for e in events]


# ── end to end: the chat's message runs, commits, and is pushed on a click ───

def test_from_the_chat_message_to_a_pushed_branch(api, tmp_path, monkeypatch):  # noqa: F811
    cfg = {"default_provider": "minimax", "minimax_base_url": brain.MINIMAX_CODING_PLAN_URL}
    monkeypatch.setattr(brain, "_load_config", lambda: dict(cfg))
    call, repo = api["call"], api["repo"]
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env={**GIT_ENV, "PATH": "/usr/bin:/bin"})
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    call("POST", "/api/sessions/repos", {"path": str(repo)})
    api["default"]["p"] = Mock([
        J("claim", patterns=["notes.txt"]),
        J("write_file", path="notes.txt", content="from the chat\n"),
        J(final="wrote notes.txt"),
    ])

    started = code_delegate.start("add a notes.txt file and commit")
    assert started["ok"], started
    assert started["provider"] == "minimax" and started["flat_rate"] is True
    sid, tid = started["session_id"], started["task_id"]
    assert _task(api, tid)["prompt"] == "add a notes.txt file and commit"
    _until(lambda: _task(api, tid)["status"] == "needs_review")

    st, r = call("POST", f"/api/sessions/{sid}/publish")
    assert st == 200, r
    assert r["branch"] == started["branch"] and r["commits"] >= 1 and r["compare_url"] is None
    pushed = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", r["branch"]],
                            capture_output=True, text=True)
    assert pushed.returncode == 0
    events = [e["kind"] for e in call("GET", f"/api/sessions/{sid}/events?limit=500")[1]["items"]]
    assert "session.published" in events


def test_nothing_to_publish_is_said(api, tmp_path, monkeypatch):  # noqa: F811
    cfg = {"default_provider": "minimax", "minimax_base_url": brain.MINIMAX_CODING_PLAN_URL}
    monkeypatch.setattr(brain, "_load_config", lambda: dict(cfg))
    call = api["call"]
    call("POST", "/api/sessions/repos", {"path": str(api["repo"])})
    api["default"]["p"] = Mock([J(final="nothing to change")])
    started = code_delegate.start("look at app.py and fix nothing")
    _until(lambda: _task(api, started["task_id"])["status"] == "needs_review")
    st, r = call("POST", f"/api/sessions/{started['session_id']}/publish")
    assert st == 409 and r["error"] == "nothing_to_publish"


# ── the page ────────────────────────────────────────────────────────────────

def test_the_chat_page_follows_the_session():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "ops_dashboard/web/chat.js").read_text()
    assert "case 'code_session': showCodeSession(block, evt)" in js
    assert "m.role === 'code_session'" in js
    assert "'/publish'" in js and "X-CSRF-Token" in js
