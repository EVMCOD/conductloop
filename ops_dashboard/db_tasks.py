"""Tasks + runs CRUD: the task -> agent -> run -> artifact loop.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import json
import time

from ops_dashboard import db as _dbmod

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


def start_run(task_id: int, provider: str, model: str) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO runs (task_id, provider, model, status, started_at) VALUES (?, ?, ?, 'running', ?)",
            (task_id, provider, model, now),
        )
        # Mark the task as running too. The resolved provider/model live on the
        # run only: writing them into the task would make a rerun treat an
        # auto-picked default as the user's explicit choice (strict, pinned).
        conn.execute("UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ?", (now, task_id))
        return int(cur.lastrowid or 0)


def finish_run(run_id: int, *, status: str, output_path: str | None = None,
               artifact_doc_id: int | None = None,
               tokens_in: int = 0, tokens_out: int = 0,
               duration_ms: int = 0, tool_calls: list | None = None,
               output_text: str | None = None, error: str | None = None) -> None:
    now = int(time.time())
    with _dbmod.db() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, output_path = ?, artifact_doc_id = ?, "
            "tokens_in = ?, tokens_out = ?, duration_ms = ?, tool_calls = ?, "
            "output_text = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, output_path, artifact_doc_id, tokens_in, tokens_out, duration_ms,
             json.dumps(tool_calls or []), output_text, error, now, run_id),
        )
        # Mirror status + summary back to the task
        if status == "done":
            # Build a short output_summary from the output_text
            summary = (output_text or "")[:280] if output_text else None
            conn.execute(
                "UPDATE tasks SET status = 'done', output_summary = ?, updated_at = ? WHERE id = (SELECT task_id FROM runs WHERE id = ?)",
                (summary, now, run_id),
            )
        elif status == "error":
            conn.execute(
                "UPDATE tasks SET status = 'error', output_summary = ?, updated_at = ? WHERE id = (SELECT task_id FROM runs WHERE id = ?)",
                ((error or "error")[:280], now, run_id),
            )
        elif status == "needs_approval":
            conn.execute(
                "UPDATE tasks SET status = 'needs_approval', updated_at = ? WHERE id = (SELECT task_id FROM runs WHERE id = ?)",
                (now, run_id),
            )


def get_run(run_id: int) -> dict | None:
    with _dbmod.db() as conn:
        r = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(r) if r else None


def list_runs(task_id: int, limit: int = 20) -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


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
