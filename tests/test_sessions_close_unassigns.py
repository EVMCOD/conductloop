"""Closing a session gives its unfinished work back to nobody, not to itself.

``set_session_status(..., "closed")`` put the session's claimed, running and
blocked tasks back in ``queued`` with no owner, but left
``assigned_session_id`` pointing at the closed session. Only that session may
claim a task assigned to it, and a closed session never claims again, so the
tasks sat in the queue for ever, looking ready. Tasks that were assigned to it
and not started yet had the same problem.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard.sessions import SessionStore


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(path))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    store = SessionStore(clock=Clock())
    repo = store.mark_repo(str(tmp_path / "repo"), project_key="demo")
    return store, repo


def _session(store, repo, **kw):
    args = dict(name="backend", role="backend", provider="openai", model="gpt-x",
                repo_path=repo, base_ref="main", budget_cost_cents=200)
    args.update(kw)
    return store.create_session(**args)


@pytest.mark.parametrize("terminal", ["closed", "failed"])
def test_a_closed_session_keeps_no_task_assigned_to_it(env, terminal):
    store, repo = env
    a = _session(store, repo, name="a")
    blocked = store.enqueue_task(title="blocked", prompt="p", assigned_session_id=a)
    waiting = store.enqueue_task(title="waiting", prompt="p", assigned_session_id=a)
    assert store.claim_task(a, blocked)["id"] == blocked
    assert store.set_task_status(blocked, a, "blocked", result_summary="dry-merge conflict")
    store.set_session_status(a, terminal)
    for tid in (blocked, waiting):
        task = store.get_task(tid)
        assert task["status"] == "queued", task
        assert task["owner_session_id"] is None
        assert task["assigned_session_id"] is None, "still assigned to a session that is gone"
    # The reason it came back is on the task.
    assert f"session #{a} {terminal}" in store.get_task(blocked)["result_summary"]
    assert "dry-merge conflict" in store.get_task(blocked)["result_summary"]
    # A new session can be given the work and pick it up.
    b = _session(store, repo, name="b")
    assert store.assign_task(blocked, b) and store.assign_task(waiting, b)
    assert store.claim_task(b) is not None


def test_closing_leaves_finished_and_other_sessions_tasks_alone(env):
    store, repo = env
    a, b = _session(store, repo, name="a"), _session(store, repo, name="b")
    review = store.enqueue_task(title="review", prompt="p", assigned_session_id=a)
    done = store.enqueue_task(title="done", prompt="p", assigned_session_id=a)
    other = store.enqueue_task(title="other", prompt="p", assigned_session_id=b)
    store.claim_task(a, done)
    store.set_task_status(done, a, "done")
    store.claim_task(a, review)
    store.set_task_status(review, a, "needs_review")
    store.set_session_status(a, "closed")
    assert store.get_task(review)["status"] == "needs_review"
    assert store.get_task(review)["owner_session_id"] == a      # its result is on a's branch
    assert store.get_task(done)["status"] == "done"
    assert store.get_task(done)["assigned_session_id"] == a     # history stays
    assert store.get_task(other)["assigned_session_id"] == b


def test_the_close_route_unassigns_the_tasks(env, monkeypatch):
    store, repo = env
    from ops_dashboard import routes_sessions
    from ops_dashboard.server import DashboardHandler

    a = _session(store, repo, name="a")
    blocked = store.enqueue_task(title="blocked", prompt="p", assigned_session_id=a)
    store.claim_task(a, blocked)
    store.set_task_status(blocked, a, "blocked", result_summary="worktree: missing")

    class _Sup:
        def __init__(self):
            self.store = store

        def stop(self, _sid):
            return True

        def running(self):
            return set()

    monkeypatch.setattr(routes_sessions, "supervisor", lambda: _Sup())

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sessions/{a}/close",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200 and json.loads(r.read())["ok"] is True
    finally:
        srv.shutdown()
    task = store.get_task(blocked)
    assert task["status"] == "queued" and task["assigned_session_id"] is None
