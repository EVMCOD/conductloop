"""Decision log entries.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

from ops_dashboard import db as _dbmod

# ============================================================
# Decisions log
# ============================================================

def add_decision(
    title: str,
    decision: str,
    *,
    project_key: str | None = None,
    context: str | None = None,
    consequences: str | None = None,
    links: list[dict] | None = None,
    tags: list[str] | None = None,
    decided_at: int | None = None,
) -> int:
    now = int(__import__("time").time())
    decided_at = decided_at or now
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO decisions
               (project_key, title, context, decision, consequences, links, tags, decided_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_key, title, context, decision, consequences,
                __import__("json").dumps(links or []),
                __import__("json").dumps(tags or []),
                decided_at, now, now,
            ),
        )
        return int(cur.lastrowid or 0)


def list_decisions(
    *,
    project: str | None = None,
    limit: int = 50,
    search: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM decisions WHERE 1=1"
    params: list = []
    if project:
        sql += " AND project_key = ?"
        params.append(project)
    if search:
        sql += " AND (title LIKE ? OR decision LIKE ? OR context LIKE ?)"
        s = f"%{search}%"
        params.extend([s, s, s])
    sql += " ORDER BY decided_at DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["links"] = __import__("json").loads(d.get("links") or "[]")
        except Exception: d["links"] = []
        try: d["tags"] = __import__("json").loads(d.get("tags") or "[]")
        except Exception: d["tags"] = []
        out.append(d)
    return out


def get_decision(decision_id: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if not row: return None
    d = dict(row)
    try: d["links"] = __import__("json").loads(d.get("links") or "[]")
    except Exception: d["links"] = []
    try: d["tags"] = __import__("json").loads(d.get("tags") or "[]")
    except Exception: d["tags"] = []
    return d


def delete_decision(decision_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))
        return cur.rowcount > 0
