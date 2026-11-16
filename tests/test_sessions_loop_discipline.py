"""The loop has discipline.

Three things the exam showed:

* the agent read the same file over and over, and the model was never told
  that it was going round in circles;
* nothing said what the agent was going to do before it did it: the plan was
  in its head, and the card in the chat could not show it;
* the card said "step 12 of 60" whatever the runner was, and the 60 was a
  number nobody had looked at since the runner changed.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from ops_dashboard.sessions import SessionStore, limits
from ops_dashboard.sessions import worktrees as W
from ops_dashboard.sessions.runner import LOOP_THRESHOLD, Ledger, SessionRunner

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


class Scripted:
    """Replies from a list (then ``default``); records what it was sent."""

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
        return self.default or json.dumps({"final": "nothing left"})


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
    (repo / "src" / "app.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setenv(W.WORKTREES_ENV, str(tmp_path / "wt-root"))
    store = SessionStore()
    store.mark_repo(str(repo))
    store.set_model_price("openai", "m", in_cents_per_mtok=0, out_cents_per_mtok=10_000)

    def session(name="s", *, prov, max_iterations=limits.DEFAULT_MAX_ITERATIONS):
        sid = store.create_session(name=name, role=name, provider="openai", model="m",
                                   repo_path=str(repo), base_ref="main", budget_cost_cents=10_000,
                                   max_iterations=max_iterations)
        info = W.create(repo, "main", session_id=sid, role=name)
        store.set_session_worktree(sid, info.path, info.branch)
        return sid

    return {"store": store, "repo": repo, "session": session}


def _run(env, prov, **kw):
    """Run one task. Returns (outcome, session id)."""
    store = env["store"]
    sid = env["session"]("a", prov=prov, **kw)
    store.enqueue_task(title="t", prompt="do it", assigned_session_id=sid)
    runner = SessionRunner(store, sid, provider_factory=lambda s: prov)
    return runner.run_task(store.claim_task(sid)), sid


def _events(store, sid):
    return store.list_events(sid, limit=1000)


# ── the plan ───────────────────────────────────────────────────────────────

def test_the_first_turn_is_a_plan_and_it_is_kept_as_an_event(env):
    prov = Scripted(["I will change src/app.py and run pytest.",
                     json.dumps({"final": "done"})])
    out, sid = _run(env, prov)
    assert out.task_status == "needs_review"
    plans = [e for e in _events(env["store"], sid) if e["kind"] == "agent.plan"]
    assert len(plans) == 1
    assert "src/app.py" in json.loads(plans[0]["payload"])["plan"]


def test_the_plan_is_shown_to_the_model_and_the_work_goes_on(env):
    prov = Scripted(["I will change src/app.py and run pytest.",
                     json.dumps({"tool": "diff", "args": {}}),
                     json.dumps({"final": "done"})])
    _run(env, prov)
    # The second turn carries the go-ahead, not the plan question again.
    joined = "\n".join(str(m.get("content")) for m in prov.calls[1])
    assert "Plan noted" in joined
    assert "write a short plan" not in joined


def test_a_model_that_goes_straight_to_work_is_not_asked_twice(env):
    """A provider that answers in the JSON protocol never sees the plan
    question as a plan: it just works."""
    prov = Scripted([json.dumps({"tool": "diff", "args": {}}),
                     json.dumps({"final": "done"})])
    out, sid = _run(env, prov)
    assert out.task_status == "needs_review"
    assert not [e for e in _events(env["store"], sid) if e["kind"] == "agent.plan"]


# ── the loops ──────────────────────────────────────────────────────────────

def test_the_ledger_notices_a_file_read_three_times():
    led = Ledger()
    for _ in range(LOOP_THRESHOLD):
        led.note_read("src/app.py", {"path": "src/app.py", "text": "x = 1\n"})
    assert led.loops
    assert "src/app.py" in led.loops[0]
    assert "GOING ROUND IN CIRCLES" in led.render()


def test_the_ledger_notices_a_search_repeated_three_times():
    led = Ledger()
    for _ in range(LOOP_THRESHOLD):
        led.note_search("VALUE", {"matches": [{"path": "src/app.py", "line": 1}]})
    assert led.loops
    assert "VALUE" in led.loops[0]


def test_a_file_read_twice_is_not_a_loop_yet():
    led = Ledger()
    for _ in range(LOOP_THRESHOLD - 1):
        led.note_read("src/app.py", {"path": "src/app.py", "text": "x = 1\n"})
    assert not led.loops


def test_the_model_is_told_when_it_is_going_round_in_circles(env):
    """The same file read three times: the model is told what it already
    knows, instead of reading it a fourth time."""
    read = json.dumps({"tool": "read_file", "args": {"path": "src/app.py"}})
    prov = Scripted([read, read, read, json.dumps({"final": "done"})])
    _run(env, prov)
    last = "\n".join(str(m.get("content")) for m in prov.calls[-1])
    assert "GOING ROUND IN CIRCLES" in last
    assert "src/app.py" in last


# ── the cap ────────────────────────────────────────────────────────────────

def test_a_code_session_gets_a_hundred_and_twenty_turns(env):
    assert limits.DEFAULT_MAX_ITERATIONS == 120
    store = env["store"]
    sid = env["session"]("a", prov=Scripted([]))
    assert store.get_session(sid)["max_iterations"] == 120


def test_the_cap_is_still_the_one_that_stops_the_task(env):
    prov = Scripted([], default=json.dumps({"tool": "diff", "args": {}}))
    out, _ = _run(env, prov, max_iterations=3)
    assert out.iterations == 3
    assert out.session_status == "waiting_human"
