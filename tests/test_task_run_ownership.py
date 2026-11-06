"""One live attempt owns a task, and approval has to actually land.

These tests pin behaviour that used to be wrong:

* ``approve_run(task_id=...)`` answered success and wrote nothing, because
  the run id it finished with was still None.
* A second start was allowed while the first attempt was still running, and
  whichever thread finished last overwrote the task.
* A run left ``running`` by a process that is gone could not be started
  again, and the screen kept saying it was running.

A normal single attempt still completes, and approval still does not write
the staged artifact anywhere.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_dashboard import brain

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for var in ("BRAIN_PROVIDER", "BRAIN_MODEL", "OLLAMA_MODEL", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "MINIMAX_API_KEY", "GATEWAY_API_KEY",
                "OPS_REQUIRE_APPROVAL", "CONDUCTLOOP_DEMO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "runs.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("OPS_AGENT_VAULT", str(tmp_path / "vault" / "_agents"))
    (tmp_path / "vault").mkdir()
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    from ops_dashboard import db
    db.DB_PATH = tmp_path / "runs.db"  # type: ignore[attr-defined]
    db.init_db()
    return db


def _task(db, title: str = "Write the brief") -> int:
    return db.create_task(None, title, "do the work", provider="mock", model="mock-1")


def test_a_single_run_still_finishes_the_task(env, monkeypatch):
    monkeypatch.setattr(brain.MockProvider, "ask", lambda self, system, messages, **kw: "the brief")
    from ops_dashboard import orchestrator
    tid = _task(env)
    res = orchestrator.run_task(tid, sync=True)
    assert res["ok"] is True
    run = env.get_run(res["run_id"])
    assert run["status"] == "done"
    assert run["output_text"] == "the brief"
    assert run["owner_token"]
    task = env.get_task(tid)
    assert task["status"] == "done"
    assert task["output_summary"] == "the brief"
    # A finished task can be run again. That second attempt is a new row.
    again = orchestrator.run_task(tid, sync=True)
    assert again["ok"] is True and again["run_id"] != res["run_id"]
    assert len(env.list_runs(tid)) == 2
    assert env.get_task(tid)["status"] == "done"


def test_a_second_start_is_refused_while_the_first_is_live(env, monkeypatch):
    gate = threading.Event()

    def _ask(self, system, messages, **kw):
        if not gate.wait(5):
            raise TimeoutError("provider was not released")
        return "finished after the gate"

    monkeypatch.setattr(brain.MockProvider, "ask", _ask)
    from ops_dashboard import orchestrator
    tid = _task(env)
    first: dict = {"run_id": None}
    try:
        first = orchestrator.run_task(tid, sync=False)
        assert first["ok"] is True
        second = orchestrator.run_task(tid, sync=False)
        assert second == {"ok": False, "error": "task is already running"}
        again = orchestrator.re_run(tid, sync=False)
        assert again["ok"] is False and "already running" in again["error"]
        # The old re_run reset the task to queued, which is what let the
        # second attempt start. The task stays with the live run.
        assert env.get_task(tid)["status"] == "running"
        assert len(env.list_runs(tid)) == 1
        assert env.list_runs(tid)[0]["status"] == "running"
    finally:
        gate.set()
        if first.get("run_id"):
            for _ in range(100):
                if env.get_run(first["run_id"])["status"] != "running":
                    break
                time.sleep(0.05)
    assert env.get_run(first["run_id"])["status"] == "done"
    assert env.get_task(tid)["status"] == "done"
    assert env.get_task(tid)["output_summary"] == "finished after the gate"


def test_two_callers_cannot_both_claim_the_task(env):
    tid = _task(env)
    barrier = threading.Barrier(8)
    found: list[int | None] = []
    lock = threading.Lock()

    def go():
        barrier.wait()
        try:
            run_id: int | None = env.start_run(tid, "mock", "mock-1")
        except Exception:
            run_id = None
        with lock:
            found.append(run_id)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert all(not t.is_alive() for t in threads)
    assert len(found) == 8
    claimed = [r for r in found if r is not None]
    assert len(claimed) == 1
    runs = env.list_runs(tid)
    assert len(runs) == 1 and runs[0]["status"] == "running" and runs[0]["id"] == claimed[0]


def test_a_late_finish_does_not_overwrite_a_newer_attempt(env):
    tid = _task(env)
    older = env.start_run(tid, "mock", "mock-1")
    assert env.finish_run(older, status="needs_approval", output_text="OLD result")
    assert env.get_task(tid)["status"] == "needs_approval"
    newer = env.start_run(tid, "mock", "mock-1")
    assert newer and newer != older
    # Approving the older run while the newer one is in flight records that
    # run, and leaves the task with the attempt that is still running.
    assert env.finish_run(older, status="done", output_text="OLD result\n\n[approved]")
    assert env.get_run(older)["status"] == "done"
    assert env.get_run(newer)["status"] == "running"
    assert env.get_task(tid)["status"] == "running"
    assert env.finish_run(newer, status="done", output_text="NEW result")
    assert env.get_task(tid)["status"] == "done"
    assert env.get_task(tid)["output_summary"] == "NEW result"
    # And once the newer attempt has finished, a repeated delivery of the
    # older one still cannot replace it.
    assert env.finish_run(older, status="done", output_text="OLD result again") is False
    assert env.get_task(tid)["output_summary"] == "NEW result"
    assert env.get_run(newer)["status"] == "done"


def test_approval_persists_the_run_and_does_not_write_the_artifact(env, tmp_path):
    from ops_dashboard import orchestrator
    tid = _task(env)
    rid = env.start_run(tid, "mock", "mock-1")
    assert env.finish_run(rid, status="needs_approval", output_text="draft")
    # This is the call the HTTP handler makes. It used to finish run_id=None,
    # return ok, and leave both rows at needs_approval.
    res = orchestrator.approve_run(task_id=tid)
    assert res["ok"] is True
    assert res["run_id"] == rid and res["task_id"] == tid
    assert res["artifact_written"] is False
    assert res["status"] == "done"
    run = env.get_run(rid)
    assert run["status"] == "done"
    assert "not written to the vault" in run["output_text"]
    assert "draft" in run["output_text"]
    assert env.get_task(tid)["status"] == "done"
    vault = tmp_path / "vault" / "_agents"
    assert not vault.exists() or not any(vault.rglob("*"))
    # Approving again is an honest failure, not a second success.
    again = orchestrator.approve_run(task_id=tid, run_id=rid)
    assert again["ok"] is False
    assert env.get_run(rid)["status"] == "done"


def test_approval_of_another_tasks_run_changes_nothing(env):
    from ops_dashboard import orchestrator
    first = _task(env, "first")
    second = _task(env, "second")
    rid = env.start_run(first, "mock", "mock-1")
    env.finish_run(rid, status="needs_approval", output_text="keep me")
    other = env.start_run(second, "mock", "mock-1")
    env.finish_run(other, status="needs_approval", output_text="other")
    res = orchestrator.approve_run(task_id=second, run_id=rid)
    assert res == {"ok": False, "error": "run does not belong to this task"}
    assert env.get_run(rid)["status"] == "needs_approval"
    assert env.get_run(other)["status"] == "needs_approval"
    assert env.get_task(first)["status"] == "needs_approval"
    assert env.get_task(second)["status"] == "needs_approval"


def test_a_dead_owner_is_interrupted_and_a_live_one_is_not(env, monkeypatch):
    from ops_dashboard import db_tasks
    live_task = _task(env, "live")
    dead_task = _task(env, "dead")
    foreign_task = _task(env, "foreign")
    live = env.start_run(live_task, "mock", "mock-1")
    dead = env.start_run(dead_task, "mock", "mock-1")
    foreign = env.start_run(foreign_task, "mock", "mock-1")
    with env.db() as conn:
        # This pid, but not this process: a restart that reused the pid.
        conn.execute("UPDATE runs SET owner_token = ? WHERE id = ?",
                     (f"{os.getpid()}:previous-boot", dead))
        conn.execute("UPDATE runs SET owner_token = ? WHERE id = ?",
                     ("424242:other", foreign))
    # 424242 stands in for another process that is still running.
    monkeypatch.setattr(db_tasks, "_pid_alive", lambda pid: pid == 424242 or pid == os.getpid())
    recovered = env.recover_interrupted_runs()
    assert recovered == [dead]
    assert env.get_run(dead)["status"] == "error"
    assert env.get_run(dead)["error"].startswith("interrupted:")
    assert "no result was produced" in env.get_run(dead)["error"]
    assert env.get_task(dead_task)["status"] == "error"
    assert env.get_task(dead_task)["output_summary"].startswith("interrupted:")
    assert env.get_run(live)["status"] == "running"
    assert env.get_run(foreign)["status"] == "running"
    assert env.get_task(live_task)["status"] == "running"
    # The interrupted task can be started again.
    replacement = env.start_run(dead_task, "mock", "mock-1")
    assert replacement and replacement != dead
    assert env.get_task(dead_task)["status"] == "running"
    assert env.get_run(dead)["status"] == "error"
    # Restart-style recovery (init_db) must not take the live run.
    env.init_db()
    assert env.get_run(live)["status"] == "running"
    assert env.get_run(replacement)["status"] == "running"


def test_migration_collapses_duplicate_live_runs_and_is_idempotent(env):
    from ops_dashboard import migrations
    tid = _task(env)
    now = int(time.time())
    with env.db() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_runs_one_live")
        conn.execute("ALTER TABLE runs DROP COLUMN owner_token")
        for _ in range(2):
            conn.execute(
                "INSERT INTO runs (task_id, provider, model, status, started_at) "
                "VALUES (?, 'mock', 'mock-1', 'running', ?)",
                (tid, now),
            )
    conn = env.get_connection()
    try:
        migrations._mig_29_one_live_run(conn)
        conn.commit()
        migrations._mig_29_one_live_run(conn)  # gap-fill may run it again
        conn.commit()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
        assert "owner_token" in cols
        running = conn.execute(
            "SELECT id FROM runs WHERE task_id = ? AND status = 'running' ORDER BY id",
            (tid,),
        ).fetchall()
        errors = conn.execute(
            "SELECT id, error FROM runs WHERE task_id = ? AND status = 'error' ORDER BY id",
            (tid,),
        ).fetchall()
        assert len(running) == 1 and len(errors) == 1
        assert errors[0][0] < running[0][0]
        assert "another run" in errors[0][1]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (task_id, provider, model, status, started_at) "
                "VALUES (?, 'mock', 'mock-1', 'running', ?)",
                (tid, now),
            )
        conn.rollback()
    finally:
        conn.close()


def test_approve_route_names_the_task_and_the_run(env):
    from ops_dashboard.server import DashboardHandler
    tid = _task(env)
    decoy = _task(env, "decoy")
    earlier = env.start_run(tid, "mock", "mock-1")
    assert env.finish_run(earlier, status="done", output_text="earlier")
    rid = env.start_run(tid, "mock", "mock-1")
    assert env.finish_run(rid, status="needs_approval", output_text="draft")
    # The run id and the task id are different numbers. The old button posted
    # the run id on /api/tasks/<id>/approve, which looks up a task.
    assert rid != tid
    assert rid == decoy

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        # What the old button sent: the run id in the task slot, no body.
        # Task ``rid`` is a different task and has nothing waiting.
        status, body = _post(f"{base}/api/tasks/{rid}/approve", {})
        assert status == 400 and body["ok"] is False
        assert env.get_run(rid)["status"] == "needs_approval"
        assert env.get_task(tid)["status"] == "needs_approval"

        status, body = _post(f"{base}/api/tasks/{decoy}/approve", {"run_id": rid})
        assert status == 400 and body["error"] == "run does not belong to this task"
        assert env.get_run(rid)["status"] == "needs_approval"

        status, body = _post(f"{base}/api/tasks/{tid}/approve", {"run_id": "nope"})
        assert status == 400 and "integer" in body["error"]

        status, body = _post(f"{base}/api/tasks/{tid}/approve", {"run_id": rid})
        assert status == 200 and body["ok"] is True
        assert body["run_id"] == rid and body["task_id"] == tid
        assert body["artifact_written"] is False
        assert env.get_run(rid)["status"] == "done"
        assert env.get_task(tid)["status"] == "done"
        assert "not written to the vault" in env.get_run(rid)["output_text"]
    finally:
        server.shutdown()


def test_owner_liveness_does_not_signal_the_process(monkeypatch):
    """Windows maps os.kill(pid, 0) to TerminateProcess. Asking whether the
    owner is alive must not do that, including for this process."""
    from ops_dashboard import db_tasks, parent_watch
    seen: list[int] = []

    def boom(*_a, **_k):
        raise AssertionError("os.kill must not be used to test a run owner")

    monkeypatch.setattr(os, "kill", boom)
    monkeypatch.setattr(parent_watch, "alive", lambda pid: seen.append(pid) or True)
    assert db_tasks.owner_is_alive(db_tasks.process_owner_token()) is True
    assert seen == []
    assert db_tasks.owner_is_alive(f"{os.getpid()}:previous-boot") is False
    assert seen == []
    assert db_tasks.owner_is_alive("4242:other") is True
    assert seen == [4242]
    assert db_tasks._pid_alive(4242) is True
    assert seen == [4242, 4242]


def test_the_approval_button_posts_the_task_and_the_run():
    src = (WEB / "app.js").read_text(encoding="utf-8")
    start = src.index("async function renderTaskDetail")
    detail = src[start:src.index("\ninitCanvasInteractions", start)]
    assert "Approve &amp; run write" not in detail
    assert "Mark approved" in detail
    assert "not written to the vault" in detail
    assert "'/api/tasks/' + rid + '/approve'" not in detail
    assert "'/api/tasks/' + t.id + '/approve'" in detail
    assert "run_id: rid" in detail
    assert "alert(" not in detail
    assert "confirm(" not in detail
    assert "prompt(" not in detail
    # A refused re-run is shown. The old handler ignored the response.
    assert "re-run failed" in detail


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())
