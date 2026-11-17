"""The session cap counts the sessions that are working, not the ones that exist.

A session created and never started, a session whose task is already waiting
for a person, a session that sits idle after its task finished: none of them
is doing any work, and none of them may hold a slot. Before this change the
cap counted every session that was not closed or failed, so the third empty
session blocked the next task of the chat for ever.

The cap is still a cap: three sessions with work in flight are three sessions,
and the fourth one is refused — atomically, under concurrency.
"""
from __future__ import annotations

import threading

import pytest

from ops_dashboard.sessions import SessionLimitError, SessionStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(path))
    from ops_dashboard import db

    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    store = SessionStore()
    repo = store.mark_repo(str(tmp_path / "repo"), project_key="demo")
    return store, repo


def _session(store, repo, **kw):
    args = dict(name="worker", role="worker", provider="openai", model="gpt-x",
                repo_path=repo, base_ref="main", budget_cost_cents=200)
    args.update(kw)
    return store.create_session(**args)


def _session_with_task(store, repo, name, *, status):
    """A session holding one task, moved to ``status``."""
    sid = _session(store, repo, name=name)
    tid = store.enqueue_task(title="work", prompt="p", assigned_session_id=sid)
    assert store.claim_task(sid, tid) is not None
    if status != "claimed":
        assert store.set_task_status(tid, sid, status)
    return sid


# ── sessions without work do not take a slot ────────────────────────────────

def test_three_sessions_without_tasks_allow_a_fourth(env):
    store, repo = env
    for i in range(3):
        _session(store, repo, name=f"empty{i}")
    # The first three are not working: there is room for a fourth.
    fourth = _session(store, repo, name="fourth")
    assert store.get_session(fourth)["id"] == fourth


def test_a_session_whose_task_is_waiting_for_review_does_not_count(env):
    store, repo = env
    for i in range(3):
        _session_with_task(store, repo, f"review{i}", status="needs_review")
    # A finished task waiting for a person is not work in flight.
    _session(store, repo, name="fourth")


def test_a_session_whose_task_is_done_does_not_count(env):
    store, repo = env
    for i in range(3):
        _session_with_task(store, repo, f"done{i}", status="done")
    _session(store, repo, name="fourth")


# ── sessions with work do take a slot ───────────────────────────────────────

def test_three_sessions_with_a_queued_task_refuse_a_fourth(env):
    store, repo = env
    for i in range(3):
        sid = _session(store, repo, name=f"queued{i}")
        store.enqueue_task(title="work", prompt="p", assigned_session_id=sid)
    with pytest.raises(SessionLimitError):
        _session(store, repo, name="fourth")


def test_three_sessions_with_a_running_task_refuse_a_fourth(env):
    store, repo = env
    for i in range(3):
        _session_with_task(store, repo, f"running{i}", status="running")
    with pytest.raises(SessionLimitError):
        _session(store, repo, name="fourth")


def test_the_cap_is_atomic_under_concurrency(env):
    """Ten threads race for the three slots of three working sessions.

    The three working sessions are already there, so the count has to see
    them: the race is over the slot that is left, and the count must be
    taken in one transaction, or two threads could both take it.
    """
    store, repo = env
    for i in range(3):
        sid = _session(store, repo, name=f"busy{i}")
        store.enqueue_task(title="work", prompt="p", assigned_session_id=sid)
    barrier, ok, errs = threading.Barrier(10, timeout=30), [], []

    def worker(i):
        barrier.wait(timeout=30)
        try:
            ok.append(_session(store, repo, name=f"c{i}"))
        except SessionLimitError:
            errs.append(i)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert len(ok) == 0 and len(errs) == 10


def test_the_count_is_exact(env):
    """Two working sessions and one empty: the third working one fits, the
    fourth does not."""
    store, repo = env
    for name in ("a", "b"):
        sid = _session(store, repo, name=name)
        store.enqueue_task(title="work", prompt="p", assigned_session_id=sid)
    _session(store, repo, name="empty")      # no task: it does not count
    _session(store, repo, name="third")      # the third working one fits
    with pytest.raises(SessionLimitError):
        _session(store, repo, name="fourth")
