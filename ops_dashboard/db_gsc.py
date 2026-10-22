"""Google Search Console: site registry + daily search metrics.

Mirrors the ``db_ga`` pattern (see [[db_ga.py]]): a registry table
(``gsc_sites``) plus one row per (site, day) of search performance
(``gsc_metrics_daily``). Re-exported from ``ops_dashboard.db`` so
``db.<name>`` callers keep working. Uses the shared transaction context
manager via ``_dbmod.db()`` (module-reference import, safe mid-import).
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ============================================================
# Google Search Console — sites + daily metrics
# ============================================================

def add_gsc_site(site_url: str, display_name: str, project_key: str, *,
                 credentials_json: str | None = None, enabled: bool = True) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO gsc_sites
               (site_url, display_name, project_key, credentials_json,
                enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (site_url, display_name, project_key, credentials_json,
             1 if enabled else 0, now, now),
        )
        return int(cur.lastrowid or 0)


def list_gsc_sites(*, project_key: str | None = None, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM gsc_sites WHERE 1=1"
    params: list = []
    if project_key:
        sql += " AND project_key = ?"; params.append(project_key)
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY project_key, display_name"
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_gsc_site(site_id: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM gsc_sites WHERE id = ?", (site_id,)).fetchone()
        return dict(row) if row else None


def update_gsc_site(site_id: int, **fields) -> bool:
    allowed = {"site_url", "display_name", "project_key", "credentials_json", "enabled"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    sets["updated_at"] = int(time.time())
    assign = ", ".join(f"{k} = ?" for k in sets)
    with _dbmod.db() as conn:
        cur = conn.execute(
            f"UPDATE gsc_sites SET {assign} WHERE id = ?", (*sets.values(), site_id)
        )
        return cur.rowcount > 0


def delete_gsc_site(site_id: int) -> bool:
    with _dbmod.db() as conn:
        conn.execute("DELETE FROM gsc_metrics_daily WHERE site_id = ?", (site_id,))
        cur = conn.execute("DELETE FROM gsc_sites WHERE id = ?", (site_id,))
        return cur.rowcount > 0


def record_gsc_site_poll(site_id: int, status: str, error: str | None = None) -> None:
    with _dbmod.db() as conn:
        conn.execute(
            "UPDATE gsc_sites SET last_poll_at = ?, last_poll_status = ?, last_poll_error = ? WHERE id = ?",
            (int(time.time()), status, error, site_id),
        )


def upsert_gsc_metrics_daily(*, site_id: int, site_url: str, project_key: str, date: str,
                             clicks: int = 0, impressions: int = 0,
                             ctr: float | None = None, position: float | None = None,
                             raw_payload: str | None = None) -> None:
    now = int(time.time())
    with _dbmod.db() as conn:
        conn.execute(
            """INSERT INTO gsc_metrics_daily
               (site_id, site_url, project_key, date, clicks, impressions, ctr, position,
                raw_payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(site_id, date) DO UPDATE SET
                 clicks = excluded.clicks,
                 impressions = excluded.impressions,
                 ctr = excluded.ctr,
                 position = excluded.position,
                 raw_payload = excluded.raw_payload,
                 updated_at = excluded.updated_at""",
            (site_id, site_url, project_key, date, clicks, impressions, ctr, position,
             raw_payload, now, now),
        )


def gsc_summary(*, project_key: str | None = None, since: str | None = None,
                until: str | None = None) -> dict:
    """Totals + per-site breakdown across the date range.

    Note: ctr/position are impression-weighted averages, not plain AVG —
    a day with 3 impressions must not weigh like a day with 300.
    """
    where = "WHERE 1=1"
    params: list = []
    if project_key: where += " AND m.project_key = ?"; params.append(project_key)
    if since: where += " AND m.date >= ?"; params.append(since)
    if until: where += " AND m.date <= ?"; params.append(until)

    sql_total = f"""SELECT
        COALESCE(SUM(m.clicks), 0) as clicks,
        COALESCE(SUM(m.impressions), 0) as impressions,
        CASE WHEN SUM(m.impressions) > 0
             THEN 1.0 * SUM(m.clicks) / SUM(m.impressions) ELSE 0 END as ctr,
        CASE WHEN SUM(m.impressions) > 0
             THEN SUM(m.position * m.impressions) / SUM(m.impressions) ELSE 0 END as position
        FROM gsc_metrics_daily m {where}"""
    sql_by_site = f"""SELECT
        s.id as site_id, s.display_name, s.site_url, s.project_key,
        COALESCE(SUM(m.clicks), 0) as clicks,
        COALESCE(SUM(m.impressions), 0) as impressions,
        CASE WHEN SUM(m.impressions) > 0
             THEN 1.0 * SUM(m.clicks) / SUM(m.impressions) ELSE 0 END as ctr,
        CASE WHEN SUM(m.impressions) > 0
             THEN SUM(m.position * m.impressions) / SUM(m.impressions) ELSE 0 END as position
        FROM gsc_metrics_daily m JOIN gsc_sites s ON s.id = m.site_id
        {where} GROUP BY s.id ORDER BY impressions DESC"""

    with _dbmod.db() as conn:
        totals = dict(conn.execute(sql_total, params).fetchone() or {})
        by_site = [dict(r) for r in conn.execute(sql_by_site, params).fetchall()]
    return {"totals": totals, "by_site": by_site}


def gsc_daily(*, site_id: int | None = None, project_key: str | None = None,
              since: str | None = None, until: str | None = None) -> list[dict]:
    sql = "SELECT * FROM gsc_metrics_daily WHERE 1=1"
    params: list = []
    if site_id: sql += " AND site_id = ?"; params.append(site_id)
    if project_key: sql += " AND project_key = ?"; params.append(project_key)
    if since: sql += " AND date >= ?"; params.append(since)
    if until: sql += " AND date <= ?"; params.append(until)
    sql += " ORDER BY date"
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
