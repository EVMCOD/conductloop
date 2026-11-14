"""Auto-extracted repo todos.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db``.
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ---- Todo helpers ----

def replace_todos(source: str, todos: list[dict]) -> None:
    """Replace all todos for a given source. The `doc_id` field is opaque; we
    match by (source, doc_id, content) so re-runs are idempotent at the row level."""
    with _dbmod.db() as conn:
        conn.execute("DELETE FROM todos WHERE source = ?", (source,))
        for t in todos:
            conn.execute(
                "INSERT INTO todos (project_key, source, doc_id, content, done, mtime, extracted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (t.get("project_key"), source, t.get("doc_id"), t["content"],
                 1 if t["done"] else 0, t.get("mtime"), int(time.time())),
            )


def list_todos(*, project: str | None = None, only_open: bool = True, limit: int = 200) -> list[dict]:
    with _dbmod.db() as conn:
        sql = "SELECT * FROM todos"
        clauses: list[str] = []
        params: list = []
        if only_open:
            clauses.append("done = 0")
        if project:
            clauses.append("project_key = ?")
            params.append(project)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY mtime DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
