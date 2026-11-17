"""The output budget of one model turn of a session.

It used to be 1500 tokens. That is less than a reasoning model spends before
it writes the first line of code: the answer ran out of room in the middle of
a file, the session read and searched and never wrote anything, and the exam
showed it — 60 turns, 716k input tokens, no file changed.

The budget is now what the same model gets through Claude Code on the same
endpoint (32k), and it can be changed without touching the code.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from ops_dashboard.sessions import SessionStore, limits
from ops_dashboard.sessions import worktrees as W
from ops_dashboard.sessions.limits import SessionValidationError
from ops_dashboard.sessions.runner import SessionRunner

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


class Scripted:
    """A provider that records what it was asked for."""

    name = "openai"
    model = "m"
    last_usage = None

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    def ask(self, system, messages, **kw):
        self.calls.append(dict(kw))
        return self.reply


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
    (repo / "app.py").write_text("VALUE = 1\n")
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
    for p in list(provs.values()):
        p.calls.clear()


def _run(env, prov):
    store = env["store"]
    sid = env["session"]("a", prov=prov)
    store.enqueue_task(title="t", prompt="do it", assigned_session_id=sid)
    runner = SessionRunner(store, sid, provider_factory=lambda s: prov)
    return runner.run_task(store.claim_task(sid))


# ── the budget ─────────────────────────────────────────────────────────────

def test_the_default_budget_leaves_room_to_write_a_file(env, monkeypatch):
    monkeypatch.delenv(limits.MAX_TOKENS_OUT_ENV, raising=False)
    assert limits.resolve_max_tokens_out() == 32000
    prov = Scripted(json.dumps({"final": "done"}))
    _run(env, prov)
    assert prov.calls[0]["max_tokens"] == 32000


def test_the_environment_can_change_the_budget(env, monkeypatch):
    monkeypatch.setenv(limits.MAX_TOKENS_OUT_ENV, "16000")
    assert limits.resolve_max_tokens_out() == 16000
    prov = Scripted(json.dumps({"final": "done"}))
    _run(env, prov)
    assert prov.calls[0]["max_tokens"] == 16000


def test_an_impossible_budget_is_refused(env, monkeypatch):
    monkeypatch.setenv(limits.MAX_TOKENS_OUT_ENV, "not-a-number")
    with pytest.raises(SessionValidationError):
        limits.resolve_max_tokens_out()
    monkeypatch.setenv(limits.MAX_TOKENS_OUT_ENV, "1000000")
    with pytest.raises(SessionValidationError):
        limits.resolve_max_tokens_out()
    monkeypatch.setenv(limits.MAX_TOKENS_OUT_ENV, "10")
    with pytest.raises(SessionValidationError):
        limits.resolve_max_tokens_out()


def test_the_projected_cost_counts_the_budget_of_the_turn(env, monkeypatch):
    """A turn may cost the whole budget of its output: the projection has to
    see it, or the session spends past what the person allowed."""
    monkeypatch.setenv(limits.MAX_TOKENS_OUT_ENV, "32000")
    store = env["store"]
    prov = Scripted(json.dumps({"final": "done"}))
    sid = env["session"]("a", prov=prov)
    store.enqueue_task(title="t", prompt="do it", assigned_session_id=sid)
    runner = SessionRunner(store, sid, provider_factory=lambda s: prov)
    session = store.get_session(sid)
    # 1500 output tokens at 10 000 c/Mtok = 15 cents of one turn.
    assert runner._projected_cents(session, 0) == pytest.approx(15.0)
