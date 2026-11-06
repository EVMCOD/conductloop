"""A task that waits on a dependency that can no longer finish says so.

``claim_task`` only hands out a task whose dependencies are all ``done``. When
a dependency failed, was cancelled or got blocked, the task behind it stayed
``queued`` with nothing in ``result_summary``: it looked ready, no session
would ever take it, and nothing said why. It now moves to ``blocked`` with
the reason, and back to ``queued`` if the dependency recovers.
"""
from __future__ import annotations

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


def _pair(store, a):
    dep = store.enqueue_task(title="dep", prompt="p", assigned_session_id=a)
    child = store.enqueue_task(title="child", prompt="p", assigned_session_id=a,
                               depends_on=[dep])
    return dep, child


@pytest.mark.parametrize("outcome", ["failed", "blocked"])
def test_a_dependency_that_fails_or_blocks_blocks_the_task_with_a_reason(env, outcome):
    store, repo = env
    a = _session(store, repo)
    dep, child = _pair(store, a)
    assert store.claim_task(a)["id"] == dep
    assert store.set_task_status(dep, a, outcome, result_summary="boom")
    task = store.get_task(child)
    assert task["status"] == "blocked", task
    assert task["result_summary"] == f"blocked by dependency: #{dep} is {outcome}"
    assert task["owner_session_id"] is None
    assert store.claim_task(a) is None


def test_a_cancelled_dependency_blocks_the_whole_chain(env):
    store, repo = env
    a = _session(store, repo)
    dep, child = _pair(store, a)
    grandchild = store.enqueue_task(title="grandchild", prompt="p", assigned_session_id=a,
                                    depends_on=[child])
    assert store.cancel_task(dep)
    assert store.get_task(child)["status"] == "blocked"
    assert store.get_task(child)["result_summary"] == f"blocked by dependency: #{dep} is cancelled"
    assert store.get_task(grandchild)["status"] == "blocked"
    assert store.get_task(grandchild)["result_summary"] == \
        f"blocked by dependency: #{child} is blocked"


def test_a_rejected_review_blocks_the_dependent(env):
    store, repo = env
    a = _session(store, repo)
    dep, child = _pair(store, a)
    store.claim_task(a)
    store.set_task_status(dep, a, "needs_review")
    assert store.get_task(child)["status"] == "queued"      # still waiting, not blocked
    assert store.review_task(dep, accept=False, note="wrong")
    assert store.get_task(child)["status"] == "blocked"
    assert store.get_task(child)["result_summary"] == f"blocked by dependency: #{dep} is failed"


def test_a_task_enqueued_behind_a_failed_dependency_starts_blocked(env):
    store, repo = env
    a = _session(store, repo)
    dep = store.enqueue_task(title="dep", prompt="p", assigned_session_id=a)
    store.claim_task(a)
    store.set_task_status(dep, a, "failed")
    child = store.enqueue_task(title="child", prompt="p", assigned_session_id=a,
                               depends_on=[dep])
    assert store.get_task(child)["status"] == "blocked"


def test_a_dependency_that_recovers_unblocks_the_task(env):
    store, repo = env
    a = _session(store, repo)
    dep, child = _pair(store, a)
    store.claim_task(a)
    store.set_task_status(dep, a, "blocked", result_summary="dry-merge conflict")
    assert store.get_task(child)["status"] == "blocked"
    # The blocked dependency goes back to the queue (e.g. its session closed).
    store.set_task_status(dep, a, "queued")
    task = store.get_task(child)
    assert task["status"] == "queued" and task["result_summary"] is None
    assert store.claim_task(a)["id"] == dep
    assert store.set_task_status(dep, a, "done")
    assert store.claim_task(a)["id"] == child


def test_a_missing_dependency_is_named(env):
    store, repo = env
    a = _session(store, repo)
    dep, child = _pair(store, a)
    with store._tx() as c:
        c.execute("DELETE FROM session_tasks WHERE id = ?", (dep,))
    other = store.enqueue_task(title="other", prompt="p")   # any write reconciles
    assert other
    task = store.get_task(child)
    assert task["status"] == "blocked"
    assert task["result_summary"] == f"blocked by dependency: #{dep} is missing"


def test_a_blocked_task_for_another_reason_is_not_unblocked(env):
    store, repo = env
    a = _session(store, repo)
    t = store.enqueue_task(title="t", prompt="p", assigned_session_id=a)
    store.claim_task(a)
    store.set_task_status(t, a, "blocked", result_summary="worktree: missing")
    store.enqueue_task(title="other", prompt="p")
    task = store.get_task(t)
    assert task["status"] == "blocked" and task["result_summary"] == "worktree: missing"


def test_several_bad_dependencies_are_all_listed(env):
    store, repo = env
    a = _session(store, repo)
    d1 = store.enqueue_task(title="d1", prompt="p", assigned_session_id=a)
    d2 = store.enqueue_task(title="d2", prompt="p", assigned_session_id=a)
    child = store.enqueue_task(title="child", prompt="p", assigned_session_id=a,
                               depends_on=[d1, d2])
    store.cancel_task(d1)
    store.cancel_task(d2)
    assert store.get_task(child)["result_summary"] == \
        f"blocked by dependency: #{d1} is cancelled, #{d2} is cancelled"
