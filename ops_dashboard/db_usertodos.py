"""Manual user todo list.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db``.
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ---- User-todo helpers (manual list) ----

def add_user_todo(content: str, project_key: str | None = None, tags: list[str] | None = None, priority: int = 0, due_date: int | None = None, assignee_id: int | None = None) -> int:
    import json as _json
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO user_todos (content, project_key, tags, priority, due_date, assignee_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (content, project_key, _json.dumps(tags or []), priority, due_date, assignee_id, int(time.time()), int(time.time())),
        )
        return cur.lastrowid


def recompute_member_workload() -> None:
    """Sync the team_members.workload field with open todos count."""
    with _dbmod.db() as conn:
        # Reset all workloads to 0
        conn.execute("UPDATE team_members SET workload = 0")
        # Set to actual count per assignee
        conn.execute("""
            UPDATE team_members
            SET workload = (
                SELECT COUNT(*) FROM user_todos
                WHERE user_todos.assignee_id = team_members.id
                AND user_todos.status = 'open'
            )
        """)


def add_activity(kind: str, summary: str, project_key: str | None = None) -> int:
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO activity_feed (kind, summary, project_key, created_at) VALUES (?, ?, ?, ?)",
            (kind, summary, project_key, int(time.time())),
        )
        return cur.lastrowid


def recent_activity(limit: int = 50) -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_feed ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_brain_conversations_text(limit: int = 200) -> list[dict]:
    """All messages in all conversations, flat, for search."""
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT c.id AS conv_id, c.title, m.role, m.content, m.created_at "
            "FROM conversations c JOIN messages m ON m.conversation_id = c.id "
            "ORDER BY m.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_user_todos(*, status: str | None = "open", project_key: str | None = None, limit: int = 200) -> list[dict]:
    import json as _json
    with _dbmod.db() as conn:
        sql = "SELECT * FROM user_todos"
        clauses: list[str] = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if project_key:
            clauses.append("project_key = ?")
            params.append(project_key)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["tags"] = _json.loads(d["tags"] or "[]")
        except Exception: d["tags"] = []
        out.append(d)
    return out


def update_user_todo(todo_id: int, **fields) -> bool:
    if not fields: return False
    allowed = {"content", "project_key", "tags", "status", "priority", "completed_at", "due_date", "assignee_id"}
    sets: list[str] = []
    params: list = []
    for k, v in fields.items():
        if k not in allowed: continue
        if k == "tags":
            import json as _json
            v = _json.dumps(v if isinstance(v, list) else [v])
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets: return False
    sets.append("updated_at = ?")
    params.append(int(time.time()))
    params.append(todo_id)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE user_todos SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete_user_todo(todo_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM user_todos WHERE id = ?", (todo_id,))
        return cur.rowcount > 0


def user_todo_summary() -> dict:
    with _dbmod.db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM user_todos").fetchone()["c"]
        open_ = conn.execute("SELECT COUNT(*) AS c FROM user_todos WHERE status = 'open'").fetchone()["c"]
        done = conn.execute("SELECT COUNT(*) AS c FROM user_todos WHERE status = 'done'").fetchone()["c"]
        per_project = conn.execute(
            "SELECT project_key, COUNT(*) AS c FROM user_todos WHERE status = 'open' GROUP BY project_key ORDER BY c DESC"
        ).fetchall()
        return {"total": total, "open": open_, "done": done, "by_project": [dict(r) for r in per_project]}
