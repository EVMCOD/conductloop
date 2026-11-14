"""URL state-change history.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

from ops_dashboard import db as _dbmod

# ============================================================
# URL state changes
# ============================================================

def record_url_change(url_name: str, from_status: str, to_status: str, *, status_code: int | None = None, latency_ms: int | None = None) -> int | None:
    """Record a URL state change. Returns id if a new change was recorded, None if no actual change."""
    if from_status == to_status:
        return None
    now = int(__import__("time").time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO url_changes (url_name, from_status, to_status, status_code, latency_ms, changed_at, notified)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (url_name, from_status, to_status, status_code, latency_ms, now),
        )
        return int(cur.lastrowid or 0)


def list_url_changes(*, limit: int = 50, only_unnotified: bool = False) -> list[dict]:
    sql = "SELECT * FROM url_changes WHERE 1=1"
    params: list = []
    if only_unnotified:
        sql += " AND notified = 0"
    sql += " ORDER BY changed_at DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def mark_url_change_notified(change_id: int) -> None:
    with _dbmod.db() as conn:
        conn.execute("UPDATE url_changes SET notified = 1 WHERE id = ?", (change_id,))


def latest_url_state() -> dict[str, str]:
    """Return map of url_name -> latest status ('ok' | 'fail') from url_changes."""
    with _dbmod.db() as conn:
        rows = conn.execute(
            """SELECT url_name, to_status, MAX(changed_at) AS t
               FROM url_changes GROUP BY url_name"""
        ).fetchall()
    return {r["url_name"]: r["to_status"] for r in rows}
