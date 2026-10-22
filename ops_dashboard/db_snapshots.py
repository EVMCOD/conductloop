"""Weekly snapshot + diff helpers.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db``.
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ---- Snapshot helpers ----

def save_snapshot(day: str, kind: str, payload: dict) -> None:
    import json as _json
    with _dbmod.db() as conn:
        conn.execute(
            """
            INSERT INTO snapshots (day, kind, taken_at, stats, url_status, projects, todos)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day, kind) DO UPDATE SET
                taken_at = excluded.taken_at,
                stats = excluded.stats,
                url_status = excluded.url_status,
                projects = excluded.projects,
                todos = excluded.todos
            """,
            (day, kind, int(time.time()),
             _json.dumps(payload.get("stats", {}), ensure_ascii=False),
             _json.dumps(payload.get("url_status", []), ensure_ascii=False),
             _json.dumps(payload.get("projects", []), ensure_ascii=False),
             _json.dumps(payload.get("todos", {}), ensure_ascii=False)),
        )


def get_snapshot(day: str, kind: str = "auto") -> dict | None:
    import json as _json
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE day = ? AND kind = ?", (day, kind)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("stats", "url_status", "projects", "todos"):
        try:
            d[k] = _json.loads(d[k])
        except Exception:
            d[k] = {}
    return d


def get_previous_snapshot(day: str, kind: str = "auto") -> dict | None:
    import json as _json
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE day < ? AND kind = ? ORDER BY day DESC LIMIT 1",
            (day, kind),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("stats", "url_status", "projects", "todos"):
        try:
            d[k] = _json.loads(d[k])
        except Exception:
            d[k] = {}
    return d


def recent_snapshots(kind: str = "auto", limit: int = 14) -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT day, kind, taken_at FROM snapshots WHERE kind = ? ORDER BY day DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
    return [dict(r) for r in rows]
