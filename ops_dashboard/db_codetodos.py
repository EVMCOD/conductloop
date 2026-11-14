"""Code TODOs auto-extracted from source files.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import json
import time

from ops_dashboard import db as _dbmod

# ============================================================================
# Code TODOs (auto-extracted from source files)
# ============================================================================

def mark_url_changes_notified(ids: list[int]) -> None:
    if not ids: return
    placeholders = ",".join("?" * len(ids))
    with _dbmod.db() as conn:
        conn.execute(f"UPDATE url_changes SET notified = 1 WHERE id IN ({placeholders})", ids)

def update_decision(decision_id: int, **fields) -> bool:
    allowed = {"title", "context", "decision", "consequences", "project_key", "links", "tags"}
    sets = []
    args = []
    for k, v in fields.items():
        if k not in allowed: continue
        if k in ("links", "tags") and isinstance(v, list):
            v = json.dumps(v)
        sets.append(f"{k} = ?"); args.append(v)
    if not sets: return False
    sets.append("updated_at = ?"); args.append(int(time.time()))
    args.append(decision_id)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE decisions SET {', '.join(sets)} WHERE id = ?", args)
        return cur.rowcount > 0

def delete_voice_note(note_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM voice_notes WHERE id = ?", (note_id,))
        return cur.rowcount > 0


# ---- URL changes ----

def replace_code_todos(items: list[dict]) -> None:
    """Replace all code_todos with the given list (idempotent full reload)."""
    with _dbmod.db() as conn:
        conn.execute("DELETE FROM code_todos")
        for it in items:
            conn.execute(
                """INSERT INTO code_todos
                   (project_key, file_path, line, kind, priority, content, done, extracted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    it.get("project_key"),
                    it["file_path"],
                    int(it["line"]),
                    it["kind"],
                    it["priority"],
                    it["content"],
                    int(bool(it.get("done", False))),
                    int(__import__("time").time()),
                ),
            )


def list_code_todos(
    *,
    project: str | None = None,
    priority: str | None = None,
    only_open: bool = True,
    limit: int = 200,
) -> list[dict]:
    sql = "SELECT * FROM code_todos WHERE 1=1"
    params: list = []
    if project:
        sql += " AND project_key = ?"
        params.append(project)
    if priority:
        sql += " AND priority = ?"
        params.append(priority)
    if only_open:
        sql += " AND done = 0"
    sql += " ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, line LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def code_todo_summary() -> dict:
    with _dbmod.db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM code_todos").fetchone()["c"]
        by_prio = dict((r["priority"], r["c"]) for r in conn.execute(
            "SELECT priority, COUNT(*) AS c FROM code_todos WHERE done = 0 GROUP BY priority"
        ).fetchall())
        by_kind = dict((r["kind"], r["c"]) for r in conn.execute(
            "SELECT kind, COUNT(*) AS c FROM code_todos WHERE done = 0 GROUP BY kind"
        ).fetchall())
        by_proj = [dict(r) for r in conn.execute(
            """SELECT project_key, COUNT(*) AS c
               FROM code_todos WHERE done = 0
               GROUP BY project_key ORDER BY c DESC"""
        ).fetchall()]
    return {"total": total, "by_priority": by_prio, "by_kind": by_kind, "by_project": by_proj}


def mark_code_todo_done(todo_id: int, done: bool = True) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("UPDATE code_todos SET done = ? WHERE id = ?", (int(done), todo_id))
        return cur.rowcount > 0
