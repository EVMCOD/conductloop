"""Sentry error tracking: projects + issues, per-project polling.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ============================================================
# Sentry — error tracking (per project)
# ============================================================

def add_sentry_project(name: str, source_type: str, project_key: str, *,
                        sentry_project: str | None = None, org_slug: str | None = None,
                        auth_token: str | None = None, enabled: bool = True) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO sentry_projects
               (name, source_type, project_key, sentry_project, org_slug, auth_token,
                enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, source_type, project_key, sentry_project, org_slug, auth_token,
             1 if enabled else 0, now, now),
        )
        return int(cur.lastrowid or 0)


def list_sentry_projects(*, project_key: str | None = None, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM sentry_projects WHERE 1=1"
    params: list = []
    if project_key: sql += " AND project_key = ?"; params.append(project_key)
    if enabled_only: sql += " AND enabled = 1"
    sql += " ORDER BY project_key, name"
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_sentry_project(sid: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM sentry_projects WHERE id = ?", (sid,)).fetchone()
        return dict(row) if row else None


def get_sentry_project_by_name(name: str) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM sentry_projects WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def update_sentry_project(sid: int, **fields) -> bool:
    allowed = {"name", "source_type", "project_key", "sentry_project", "org_slug", "auth_token", "enabled"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed: continue
        if k == "enabled": v = 1 if v else 0
        sets.append(f"{k} = ?"); params.append(v)
    if not sets: return False
    sets.append("updated_at = ?"); params.append(int(time.time()))
    params.append(sid)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE sentry_projects SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def record_sentry_project_poll(sid: int, status: str, error: str | None = None) -> None:
    now = int(time.time())
    with _dbmod.db() as conn:
        conn.execute(
            """UPDATE sentry_projects
               SET last_poll_at = ?, last_poll_status = ?, last_poll_error = ?, updated_at = ?
               WHERE id = ?""",
            (now, status, error, now, sid),
        )


def delete_sentry_project(sid: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM sentry_issues WHERE project_id = ?", (sid,))
        cur = conn.execute("DELETE FROM sentry_projects WHERE id = ?", (sid,))
        return cur.rowcount > 0


def upsert_sentry_issue(*, project_id: int, sentry_project_slug: str, sentry_project_id: str,
                          project_key: str, issue_id: str, title: str, culprit: str = "",
                          level: str = "error", status: str = "unresolved",
                          event_count: int = 0, user_count: int = 0,
                          first_seen: int = 0, last_seen: int = 0,
                          permalink: str | None = None, raw_payload: str | None = None) -> int:
    """Idempotent upsert keyed on (project_id, issue_id)."""
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO sentry_issues
               (project_id, sentry_project_slug, sentry_project_id, project_key, issue_id,
                title, culprit, level, status, event_count, user_count,
                first_seen, last_seen, permalink, raw_payload, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, issue_id) DO UPDATE SET
                title=excluded.title, culprit=excluded.culprit, level=excluded.level,
                status=excluded.status, event_count=excluded.event_count,
                user_count=excluded.user_count, first_seen=excluded.first_seen,
                last_seen=excluded.last_seen, permalink=excluded.permalink,
                raw_payload=excluded.raw_payload, updated_at=excluded.updated_at""",
            (project_id, sentry_project_slug, sentry_project_id, project_key, issue_id,
             title, culprit, level, status, event_count, user_count,
             first_seen, last_seen, permalink, raw_payload, now),
        )
        return int(cur.lastrowid or 0)


def list_sentry_issues(*, project_key: str | None = None, level: str | None = None,
                        status: str | None = None, since: int | None = None,
                        limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM sentry_issues WHERE 1=1"
    params: list = []
    if project_key: sql += " AND project_key = ?"; params.append(project_key)
    if level: sql += " AND level = ?"; params.append(level)
    if status: sql += " AND status = ?"; params.append(status)
    if since: sql += " AND last_seen >= ?"; params.append(since)
    sql += " ORDER BY last_seen DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def sentry_summary(*, since: int | None = None) -> dict:
    """Aggregate counts of issues by level + status + project."""
    where = "WHERE 1=1"
    params: list = []
    if since: where += " AND last_seen >= ?"; params.append(since)
    sql_total = f"SELECT COUNT(*) FROM sentry_issues {where}"
    sql_by_level = f"SELECT level, COUNT(*) as cnt FROM sentry_issues {where} GROUP BY level"
    sql_by_status = f"SELECT status, COUNT(*) as cnt FROM sentry_issues {where} GROUP BY status"
    sql_by_project = f"SELECT project_key, COUNT(*) as cnt, SUM(event_count) as events FROM sentry_issues {where} GROUP BY project_key ORDER BY cnt DESC"
    with _dbmod.db() as conn:
        total = conn.execute(sql_total, params).fetchone()[0]
        by_level = [dict(r) for r in conn.execute(sql_by_level, params).fetchall()]
        by_status = [dict(r) for r in conn.execute(sql_by_status, params).fetchall()]
        by_project = [dict(r) for r in conn.execute(sql_by_project, params).fetchall()]
    return {"total": total, "by_level": by_level, "by_status": by_status, "by_project": by_project}
