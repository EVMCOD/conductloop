"""Tasks + runs CRUD: the task -> agent -> run -> artifact loop.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from ops_dashboard import db as _dbmod

# One token per process. A run stores ``pid:token``. After a restart the pid
# may be reused by something else, but the token will not match, so that run
# is not treated as work this process is still doing.
_PROCESS_OWNER_ID = uuid.uuid4().hex

# Shown on the run and, when this run still owns the task, on the task.
INTERRUPTED_ERROR = (
    "interrupted: the process running this task is gone, so no result was produced"
)

# An outcome can only close a run that is still ``running``. Approval of a
# run that stopped at ``needs_approval`` is ``record_approval``: it keeps the
# run's metrics, tool log and artifact fields instead of resetting them.
_FINISH_FROM = {
    "done": ("running",),
    "error": ("running",),
    "needs_approval": ("running",),
}

# ============================================================================
# Tasks + Runs CRUD (the loop: task -> agent -> run -> artifact)
# ============================================================================

def create_task(project_key: str | None, title: str, prompt: str,
                agent_member_id: int | None = None,
                provider: str | None = None,
                model: str | None = None,
                created_by: str | None = None) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (project_key, title, prompt, agent_member_id, provider, model, status, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (project_key, title, prompt, agent_member_id, provider, model, now, now, created_by),
        )
        return int(cur.lastrowid or 0)


def list_tasks(project_key: str | None = None, status: str | None = None,
               limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM tasks"
    args: list = []
    where = []
    if project_key:
        where.append("project_key = ?")
        args.append(project_key)
    if status:
        where.append("status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    with _dbmod.db() as conn:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def get_task(task_id: int) -> dict | None:
    with _dbmod.db() as conn:
        r = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(r) if r else None


def update_task(task_id: int, **fields) -> bool:
    allowed = {"title", "prompt", "project_key", "agent_member_id", "provider", "model", "status", "output_summary"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    sets["updated_at"] = int(time.time())
    sql = "UPDATE tasks SET " + ", ".join(f"{k} = ?" for k in sets) + " WHERE id = ?"
    with _dbmod.db() as conn:
        cur = conn.execute(sql, [*sets.values(), task_id])
        return cur.rowcount > 0


def process_owner_token() -> str:
    """Identity of this process, stored on every run it starts."""
    return f"{os.getpid()}:{_PROCESS_OWNER_ID}"


def _split_owner(token: str | None) -> tuple[int | None, str | None]:
    if not token or ":" not in token:
        return None, None
    pid_s, uid = token.split(":", 1)
    if not uid:
        return None, None
    try:
        pid = int(pid_s)
    except ValueError:
        return None, None
    if pid <= 0:
        return None, None
    return pid, uid


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process other than a signal to it.

    ``os.kill(pid, 0)`` is a liveness check on POSIX. On Windows, CPython turns
    every signal except the console events into ``TerminateProcess``, so the
    check would kill the process it is asking about (this server, or another
    live owner). ``parent_watch.alive`` already uses a handle wait there.
    """
    from .parent_watch import alive
    return alive(pid)


def owner_is_alive(token: str | None) -> bool:
    """True when the process that started the run is still this one, or another
    live process. A missing token, a dead pid, or this pid with a different
    boot token means the run has nobody left to finish it.

    This process is recognised from the pid and the boot token before any
    liveness call, so recovery never signals the server that is running it.
    """
    pid, uid = _split_owner(token)
    if pid is None or uid is None:
        return False
    if pid == os.getpid():
        return uid == _PROCESS_OWNER_ID
    return _pid_alive(pid)


@contextmanager
def _immediate() -> Iterator[sqlite3.Connection]:
    """One write transaction. Two starts of the same task cannot both pass."""
    conn = _dbmod.get_connection()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def _interrupt_run(conn: sqlite3.Connection, run_id: int, now: int) -> None:
    cur = conn.execute(
        "UPDATE runs SET status = 'error', error = ?, finished_at = ? "
        "WHERE id = ? AND status = 'running'",
        (INTERRUPTED_ERROR, now, run_id),
    )
    if cur.rowcount != 1:
        return
    # Only the latest run may publish onto the task. A newer attempt that
    # already started keeps the task.
    conn.execute(
        "UPDATE tasks SET status = 'error', output_summary = ?, updated_at = ? "
        "WHERE id = (SELECT task_id FROM runs WHERE id = ?) AND status = 'running' "
        "AND NOT EXISTS (SELECT 1 FROM runs newer "
        "WHERE newer.task_id = tasks.id AND newer.id > ?)",
        (INTERRUPTED_ERROR[:280], now, run_id, run_id),
    )


def recover_interrupted_runs() -> list[int]:
    """Close runs left ``running`` by a process that is gone.

    Returns the run ids that were marked ``error``. A live owner — this
    process, or another process whose pid is still alive — is not touched.
    """
    now = int(time.time())
    recovered: list[int] = []
    with _immediate() as conn:
        tables = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()
        if tables is None:
            return []
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        if "owner_token" not in cols:
            return []
        rows = conn.execute(
            "SELECT id, owner_token FROM runs WHERE status = 'running' ORDER BY id"
        ).fetchall()
        for row in rows:
            if owner_is_alive(row["owner_token"]):
                continue
            _interrupt_run(conn, int(row["id"]), now)
            recovered.append(int(row["id"]))
    return recovered


def start_run(task_id: int, provider: str, model: str) -> int | None:
    """Claim the task and insert its run. None if a live run already owns it.

    A run whose owner process is gone is recorded as interrupted first, then
    this start takes the task. The resolved provider and model stay on the
    run only: copying them onto the task would make a rerun treat an
    auto-picked default as the user's explicit choice.
    """
    now = int(time.time())
    token = process_owner_token()
    with _immediate() as conn:
        live = conn.execute(
            "SELECT id, owner_token FROM runs WHERE task_id = ? AND status = 'running' ORDER BY id",
            (task_id,),
        ).fetchall()
        if any(owner_is_alive(row["owner_token"]) for row in live):
            return None
        for row in live:
            _interrupt_run(conn, int(row["id"]), now)
        try:
            cur = conn.execute(
                "INSERT INTO runs (task_id, provider, model, status, started_at, owner_token) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (task_id, provider, model, now, token),
            )
        except sqlite3.IntegrityError:
            # Lost the race with another start that committed the live row.
            still = conn.execute(
                "SELECT 1 FROM runs WHERE task_id = ? AND status = 'running'",
                (task_id,),
            ).fetchone()
            if still is None:
                raise
            return None
        conn.execute(
            "UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ?",
            (now, task_id),
        )
        return int(cur.lastrowid or 0)


def finish_run(run_id: int, *, status: str, output_path: str | None = None,
               artifact_doc_id: int | None = None,
               tokens_in: int = 0, tokens_out: int = 0,
               duration_ms: int = 0, tool_calls: list | None = None,
               output_text: str | None = None, error: str | None = None) -> bool:
    """Record the outcome of one run.

    Returns False when this outcome cannot leave the run's current state
    (it already finished, or it was never running). The task is updated only
    when this run is still the latest one and the task is still in the state
    that run owns. A late finish of an older attempt does not overwrite a
    newer one.
    """
    now = int(time.time())
    allowed = _FINISH_FROM.get(status, ("running",))
    placeholders = ",".join("?" * len(allowed))
    with _immediate() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = ?, output_path = ?, artifact_doc_id = ?, "
            "tokens_in = ?, tokens_out = ?, duration_ms = ?, tool_calls = ?, "
            "output_text = ?, error = ?, finished_at = ? "
            f"WHERE id = ? AND status IN ({placeholders})",
            (status, output_path, artifact_doc_id, tokens_in, tokens_out, duration_ms,
             json.dumps(tool_calls or []), output_text, error, now, run_id, *allowed),
        )
        if cur.rowcount != 1:
            return False
        # The task mirror is conditional so a newer attempt keeps the task.
        owns = (
            "id = (SELECT task_id FROM runs WHERE id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM runs newer "
            "WHERE newer.task_id = tasks.id AND newer.id > ?)"
        )
        if status == "done":
            summary = (output_text or "")[:280] if output_text else None
            conn.execute(
                "UPDATE tasks SET status = 'done', output_summary = ?, updated_at = ? "
                f"WHERE {owns} AND status IN ('running', 'needs_approval')",
                (summary, now, run_id, run_id),
            )
        elif status == "error":
            conn.execute(
                "UPDATE tasks SET status = 'error', output_summary = ?, updated_at = ? "
                f"WHERE {owns} AND status = 'running'",
                ((error or "error")[:280], now, run_id, run_id),
            )
        elif status == "needs_approval":
            conn.execute(
                "UPDATE tasks SET status = 'needs_approval', updated_at = ? "
                f"WHERE {owns} AND status = 'running'",
                (now, run_id, run_id),
            )
        return True


def record_approval(run_id: int, output_text: str) -> bool:
    """Mark a ``needs_approval`` run as ``done`` and say so in its text.

    Only the status and the text change: tokens, duration, the tool log and
    the artifact fields are what the run actually did and stay as recorded.
    Returns False when the run is not waiting for approval any more. The task
    follows only when this is its latest run and it is still waiting too.
    """
    now = int(time.time())
    with _immediate() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = 'done', output_text = ? "
            "WHERE id = ? AND status = 'needs_approval'",
            (output_text, run_id),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "UPDATE tasks SET status = 'done', output_summary = ?, updated_at = ? "
            "WHERE id = (SELECT task_id FROM runs WHERE id = ?) "
            "AND status = 'needs_approval' "
            "AND NOT EXISTS (SELECT 1 FROM runs newer "
            "WHERE newer.task_id = tasks.id AND newer.id > ?)",
            ((output_text or "")[:280] or None, now, run_id, run_id),
        )
        return True


def get_run(run_id: int) -> dict | None:
    with _dbmod.db() as conn:
        r = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(r) if r else None


def list_runs(task_id: int, limit: int = 20) -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def latest_pending_approval(task_id: int) -> dict | None:
    """The newest run of this task that is still waiting for approval."""
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE task_id = ? AND status = 'needs_approval' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None


def task_stats() -> dict:
    """Aggregate counts for the tasks panel."""
    with _dbmod.db() as conn:
        rows = conn.execute("SELECT status, COUNT(*) as c FROM tasks GROUP BY status").fetchall()
        by_status = {r["status"]: r["c"] for r in rows}
        total = sum(by_status.values())
        runs_total = conn.execute("SELECT COUNT(*) as c FROM runs").fetchone()["c"]
        runs_done = conn.execute("SELECT COUNT(*) as c FROM runs WHERE status = 'done'").fetchone()["c"]
        artifacts = conn.execute("SELECT COUNT(*) as c FROM runs WHERE artifact_doc_id IS NOT NULL").fetchone()["c"]
        return {
            "total": total,
            "by_status": by_status,
            "runs_total": runs_total,
            "runs_done": runs_done,
            "artifacts": artifacts,
        }
