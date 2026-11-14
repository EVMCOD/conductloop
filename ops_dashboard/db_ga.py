"""Google Analytics 4: site registry + daily metrics + polling.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ============================================================
# Google Analytics 4 — sites + daily metrics
# ============================================================

def add_ga_site(property_id: str, display_name: str, project_key: str, *,
                url: str | None = None, service_account_json: str | None = None,
                source_type: str | None = None, enabled: bool = True) -> int:
    now = int(time.time())
    if source_type is None:
        source_type = "ga4" if (service_account_json or "").strip() else "mock"
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO ga_sites
               (property_id, display_name, url, project_key, service_account_json,
                source_type, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (property_id, display_name, url, project_key, service_account_json,
             source_type, 1 if enabled else 0, now, now),
        )
        return int(cur.lastrowid or 0)


def list_ga_sites(*, project_key: str | None = None, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM ga_sites WHERE 1=1"
    params: list = []
    if project_key:
        sql += " AND project_key = ?"; params.append(project_key)
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY project_key, display_name"
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def revenue_margin_by_project(*, since: int | None = None, until: int | None = None) -> list[dict]:
    """Per-project: revenue, costs, margin (revenue - costs), margin %.

    Costs are aggregated from the `costs` table (period column = 'YYYY-MM').
    """
    rev_where = "WHERE 1=1"
    rev_params: list = []
    if since is not None: rev_where += " AND occurred_at >= ?"; rev_params.append(since)
    if until is not None: rev_where += " AND occurred_at < ?"; rev_params.append(until)

    cost_where = "WHERE 1=1"
    cost_params: list = []
    if since is not None:
        from datetime import datetime
        since_period = datetime.fromtimestamp(since).strftime("%Y-%m")
        cost_where += " AND period >= ?"; cost_params.append(since_period)
    if until is not None:
        from datetime import datetime
        until_period = datetime.fromtimestamp(until - 1).strftime("%Y-%m")
        cost_where += " AND period <= ?"; cost_params.append(until_period)

    sql = f"""
        WITH
        rev AS (
            SELECT project_key, SUM(amount_cents) AS rev_cents, COUNT(*) AS rev_count
            FROM revenue_events {rev_where} GROUP BY project_key
        ),
        cost AS (
            SELECT project_key, SUM(amount_cents) AS cost_cents, COUNT(*) AS cost_count
            FROM costs {cost_where} GROUP BY project_key
        )
        SELECT
            COALESCE(rev.project_key, cost.project_key) AS project_key,
            COALESCE(rev.rev_cents, 0) AS revenue_cents,
            COALESCE(rev.rev_count, 0) AS revenue_count,
            COALESCE(cost.cost_cents, 0) AS cost_cents,
            COALESCE(cost.cost_count, 0) AS cost_count,
            (COALESCE(rev.rev_cents, 0) - COALESCE(cost.cost_cents, 0)) AS margin_cents
        FROM rev FULL OUTER JOIN cost ON rev.project_key = cost.project_key
        ORDER BY margin_cents DESC
    """
    with _dbmod.db() as conn:
        rows = [dict(r) for r in conn.execute(sql, rev_params + cost_params).fetchall()]
    for r in rows:
        rev = r.get("revenue_cents", 0) or 0
        if rev > 0:
            r["margin_pct"] = round((r["margin_cents"] / rev) * 100, 1)
        else:
            r["margin_pct"] = None
    return rows


def get_ga_site(site_id: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM ga_sites WHERE id = ?", (site_id,)).fetchone()
        return dict(row) if row else None


def update_ga_site(site_id: int, **fields) -> bool:
    allowed = {"property_id", "display_name", "url", "project_key", "service_account_json", "enabled"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed: continue
        if k == "enabled": v = 1 if v else 0
        sets.append(f"{k} = ?"); params.append(v)
    if not sets: return False
    sets.append("updated_at = ?"); params.append(int(time.time()))
    params.append(site_id)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE ga_sites SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def record_ga_site_poll(site_id: int, status: str, error: str | None = None) -> None:
    now = int(time.time())
    with _dbmod.db() as conn:
        conn.execute(
            """UPDATE ga_sites
               SET last_poll_at = ?, last_poll_status = ?, last_poll_error = ?, updated_at = ?
               WHERE id = ?""",
            (now, status, error, now, site_id),
        )


def delete_ga_site(site_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM ga_sites WHERE id = ?", (site_id,))
        return cur.rowcount > 0


def upsert_ga_metrics_daily(*, site_id: int, property_id: str, project_key: str, date: str,
                              users: int = 0, new_users: int = 0, sessions: int = 0,
                              page_views: int = 0, events: int = 0,
                              engagement_rate: float | None = None,
                              avg_session_duration: float | None = None,
                              bounce_rate: float | None = None,
                              source_organic: int = 0, source_direct: int = 0,
                              source_referral: int = 0, source_social: int = 0,
                              source_email: int = 0, source_paid: int = 0,
                              top_country: str | None = None,
                              raw_payload: str | None = None) -> int:
    """Idempotent upsert keyed on (site_id, date)."""
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO ga_metrics_daily
               (site_id, property_id, project_key, date, users, new_users, sessions, page_views,
                events, engagement_rate, avg_session_duration, bounce_rate,
                source_organic, source_direct, source_referral, source_social, source_email,
                source_paid, top_country, raw_payload, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(site_id, date) DO UPDATE SET
                users=excluded.users, new_users=excluded.new_users, sessions=excluded.sessions,
                page_views=excluded.page_views, events=excluded.events,
                engagement_rate=excluded.engagement_rate,
                avg_session_duration=excluded.avg_session_duration,
                bounce_rate=excluded.bounce_rate,
                source_organic=excluded.source_organic,
                source_direct=excluded.source_direct,
                source_referral=excluded.source_referral,
                source_social=excluded.source_social,
                source_email=excluded.source_email,
                source_paid=excluded.source_paid,
                top_country=excluded.top_country,
                raw_payload=excluded.raw_payload,
                ingested_at=excluded.ingested_at""",
            (site_id, property_id, project_key, date, users, new_users, sessions, page_views,
             events, engagement_rate, avg_session_duration, bounce_rate,
             source_organic, source_direct, source_referral, source_social, source_email,
             source_paid, top_country, raw_payload, now),
        )
        return int(cur.lastrowid or 0)


def list_ga_metrics_daily(*, project_key: str | None = None, site_id: int | None = None,
                            since: str | None = None, until: str | None = None,
                            limit: int = 365) -> list[dict]:
    sql = "SELECT * FROM ga_metrics_daily WHERE 1=1"
    params: list = []
    if project_key: sql += " AND project_key = ?"; params.append(project_key)
    if site_id: sql += " AND site_id = ?"; params.append(site_id)
    if since: sql += " AND date >= ?"; params.append(since)
    if until: sql += " AND date <= ?"; params.append(until)
    sql += " ORDER BY date DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def ga_summary(*, project_key: str | None = None, since: str | None = None,
                until: str | None = None) -> dict:
    """Aggregate metrics across the date range."""
    where = "WHERE 1=1"
    params: list = []
    if project_key: where += " AND project_key = ?"; params.append(project_key)
    if since: where += " AND date >= ?"; params.append(since)
    if until: where += " AND date <= ?"; params.append(until)

    sql_total = f"""SELECT
        COALESCE(SUM(users), 0) as users,
        COALESCE(SUM(new_users), 0) as new_users,
        COALESCE(SUM(sessions), 0) as sessions,
        COALESCE(SUM(page_views), 0) as page_views,
        COALESCE(SUM(events), 0) as events,
        COALESCE(AVG(engagement_rate), 0) as avg_engagement_rate,
        COALESCE(AVG(avg_session_duration), 0) as avg_session_duration,
        COALESCE(AVG(bounce_rate), 0) as avg_bounce_rate
        FROM ga_metrics_daily {where}"""
    sql_by_site = f"""SELECT
        s.id as site_id, s.display_name, s.url, s.project_key,
        COALESCE(SUM(m.users), 0) as users,
        COALESCE(SUM(m.sessions), 0) as sessions,
        COALESCE(SUM(m.page_views), 0) as page_views
        FROM ga_metrics_daily m JOIN ga_sites s ON s.id = m.site_id
        {where} GROUP BY s.id ORDER BY users DESC"""

    with _dbmod.db() as conn:
        totals = dict(conn.execute(sql_total, params).fetchone() or {})
        by_site = [dict(r) for r in conn.execute(sql_by_site, params).fetchall()]
    return {"totals": totals, "by_site": by_site}


def ga_daily_totals(*, project_key: str | None = None, since: str | None = None,
                     until: str | None = None) -> list[dict]:
    """Time-series of total users + page_views per date, across all sites (or filter)."""
    where = "WHERE 1=1"
    params: list = []
    if project_key: where += " AND project_key = ?"; params.append(project_key)
    if since: where += " AND date >= ?"; params.append(since)
    if until: where += " AND date <= ?"; params.append(until)
    sql = f"""SELECT date,
              SUM(users) as users,
              SUM(sessions) as sessions,
              SUM(page_views) as page_views,
              SUM(events) as events
              FROM ga_metrics_daily
              {where} GROUP BY date ORDER BY date"""
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
