"""The agent must not forget what it has already done.

The window of the conversation keeps the last messages and throws the old
ones away. With them went everything the agent had read, searched and
changed, so it read the same file again and again: on 16-nov-2026 it spent 60
turns of a real task reading, and changed nothing.

The ledger is the part that does not get thrown away. It travels with every
turn, next to the task, and says what the agent read, what the searches
found, what it changed and how the commands ended.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from ops_dashboard.sessions import SessionStore, limits
from ops_dashboard.sessions import worktrees as W
from ops_dashboard.sessions.runner import KEEP_LAST_MESSAGES, Ledger, SessionRunner

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


def J(tool=None, final=None, **args):
    return json.dumps({"final": final} if final is not None else {"tool": tool, "args": args})


class Scripted:
    """Replies from a list; records what it was sent."""

    name = "openai"
    model = "m"
    last_usage = None

    def __init__(self, replies, default=None):
        self.replies, self.default = list(replies), default
        self.calls: list[list[dict]] = []

    def ask(self, system, messages, **kw):
        self.calls.append([dict(m) for m in messages])
        if self.replies:
            return self.replies.pop(0)
        return self.default or J(final="nothing left")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db

    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setenv(W.WORKTREES_ENV, str(tmp_path / "wt-root"))
    store = SessionStore()
    store.mark_repo(str(repo))
    store.set_model_price("openai", "m", in_cents_per_mtok=0, out_cents_per_mtok=10_000)
    provs: dict[int, Scripted] = {}

    def session(name="s", *, prov):
        sid = store.create_session(name=name, role=name, provider="openai", model="m",
                                   repo_path=str(repo), base_ref="main", budget_cost_cents=10_000,
                                   max_iterations=limits.DEFAULT_MAX_ITERATIONS)
        info = W.create(repo, "main", session_id=sid, role=name)
        store.set_session_worktree(sid, info.path, info.branch)
        provs[sid] = prov
        return sid

    yield {"store": store, "repo": repo, "session": session, "provs": provs}


# ── the ledger itself ──────────────────────────────────────────────────────

def test_the_ledger_says_what_was_read_found_changed_and_run():
    led = Ledger()
    led.note_read("src/app.py", {"path": "src/app.py", "text": "a\nb\nc\n"})
    led.note_search("VALUE", {"matches": [{"path": "src/app.py", "line": 1}]})
    led.note_change("src/app.py", "edited")
    led.note_run(["pytest", "tests/test_x.py"], {"exit_code": 0, "output": "1 passed in 0.1s"})
    text = led.render()
    assert "src/app.py" in text and "3 lines" in text
    assert "VALUE" in text and "src/app.py:1" in text
    assert "edited" in text
    assert "pytest tests/test_x.py" in text and "exit 0" in text and "1 passed" in text


def test_an_empty_ledger_renders_nothing():
    assert Ledger().render() == ""


def test_the_ledger_does_not_grow_for_ever():
    led = Ledger()
    for i in range(200):
        led.note_read(f"f{i}.py", {"path": f"f{i}.py", "text": "x"})
        led.note_search(f"q{i}", {"matches": []})
        led.note_run(["pytest"], {"exit_code": 0, "output": ""})
    text = led.render()
    assert len(text) < 8000
    assert "f0.py" not in text          # the oldest ones fell out
    assert "f199.py" in text            # the newest ones are there


# ── the ledger in the loop ─────────────────────────────────────────────────

def test_the_task_and_the_ledger_are_always_in_the_window(env):
    store = env["store"]
    prov = Scripted([
        J("read_file", path="src/app.py"),
        J("claim", patterns=["src/feature.py"]),
        J("write_file", path="src/feature.py", content="def f():\n    return 1\n"),
        J(final="added feature"),
    ])
    sid = env["session"]("a", prov=prov)
    store.enqueue_task(title="t", prompt="do it", assigned_session_id=sid)
    runner = SessionRunner(store, sid, provider_factory=lambda s: prov)
    runner.run_task(store.claim_task(sid))

    # The first message of every turn is the task.
    for call in prov.calls:
        assert "do it" in call[0]["content"]
    # The ledger grows with the work, and the model sees it.
    last = prov.calls[-1]
    joined = "\n".join(m["content"] for m in last)
    assert "WHAT YOU HAVE ALREADY DONE" in joined
    assert "src/app.py" in joined
    assert "src/feature.py" in joined


def test_the_ledger_survives_a_window_that_overflows(env, monkeypatch):
    """More turns than the window keeps: the ledger is still there."""
    monkeypatch.setattr("ops_dashboard.sessions.runner.KEEP_LAST_MESSAGES", 2)
    store = env["store"]
    prov = Scripted([J("read_file", path="src/app.py")] * 10, default=J(final="done"))
    sid = env["session"]("a", prov=prov)
    store.enqueue_task(title="t", prompt="do it", assigned_session_id=sid)
    runner = SessionRunner(store, sid, provider_factory=lambda s: prov)
    runner.run_task(store.claim_task(sid))
    joined = "\n".join(m["content"] for m in prov.calls[-1])
    assert "WHAT YOU HAVE ALREADY DONE" in joined
    assert "src/app.py" in joined
    assert len(prov.calls[-1]) <= KEEP_LAST_MESSAGES + 2
