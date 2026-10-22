"""Revenue sources, events, forecasts, and cohort analysis.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ============================================================
# Revenue sources
# ============================================================

def add_revenue_source(name: str, source_type: str, project_key: str, *,
                       connection_id: int | None = None, currency: str = "USD",
                       enabled: bool = True) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO revenue_sources
               (name, source_type, project_key, connection_id, currency, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, source_type, project_key, connection_id, currency, 1 if enabled else 0, now, now),
        )
        return int(cur.lastrowid or 0)


def list_revenue_sources(*, project_key: str | None = None,
                          source_type: str | None = None,
                          enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM revenue_sources WHERE 1=1"
    params: list = []
    if project_key:
        sql += " AND project_key = ?"; params.append(project_key)
    if source_type:
        sql += " AND source_type = ?"; params.append(source_type)
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY project_key, name"
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_revenue_source(source_id: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM revenue_sources WHERE id = ?", (source_id,)).fetchone()
        return dict(row) if row else None


def update_revenue_source(source_id: int, **fields) -> bool:
    allowed = {"name", "source_type", "project_key", "connection_id", "currency", "enabled"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed: continue
        if k == "enabled": v = 1 if v else 0
        sets.append(f"{k} = ?"); params.append(v)
    if not sets: return False
    sets.append("updated_at = ?"); params.append(int(time.time()))
    params.append(source_id)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE revenue_sources SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def record_revenue_source_poll(source_id: int, status: str, error: str | None = None) -> None:
    now = int(time.time())
    with _dbmod.db() as conn:
        conn.execute(
            """UPDATE revenue_sources
               SET last_poll_at = ?, last_poll_status = ?, last_poll_error = ?, updated_at = ?
               WHERE id = ?""",
            (now, status, error, now, source_id),
        )


def delete_revenue_source(source_id: int) -> bool:
    with _dbmod.db() as conn:
        # Also delete events for this source to keep DB clean (cascading is via application logic)
        conn.execute("DELETE FROM revenue_events WHERE source_id = ?", (source_id,))
        cur = conn.execute("DELETE FROM revenue_sources WHERE id = ?", (source_id,))
        return cur.rowcount > 0


# ============================================================
# Revenue events
# ============================================================

def insert_revenue_event(*, source_id: int | None, source_type: str, project_key: str,
                         external_id: str, event_type: str, amount_cents: int,
                         currency: str, occurred_at: int,
                         customer_ref: str | None = None, customer_email: str | None = None,
                         product_ref: str | None = None, product_name: str | None = None,
                         status: str = "succeeded", raw_payload: str | None = None,
                         ) -> int | None:
    """Idempotent insert. Returns event id, or None if duplicate (already exists)."""
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO revenue_events
               (source_id, source_type, project_key, external_id, event_type,
                amount_cents, currency, customer_ref, customer_email, product_ref,
                product_name, status, raw_payload, occurred_at, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_id, source_type, project_key, external_id, event_type,
             amount_cents, currency, customer_ref, customer_email, product_ref,
             product_name, status, raw_payload, occurred_at, now),
        )
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid or 0)


def insert_revenue_events_bulk(events: list[dict]) -> dict:
    """Bulk insert. Returns counters {inserted, duplicates}."""
    inserted = 0
    duplicates = 0
    for ev in events:
        result = insert_revenue_event(**ev)
        if result is None:
            duplicates += 1
        else:
            inserted += 1
    return {"inserted": inserted, "duplicates": duplicates, "total": len(events)}


def list_revenue_events(*, project_key: str | None = None, source_type: str | None = None,
                         event_type: str | None = None, status: str | None = None,
                         since: int | None = None, until: int | None = None,
                         limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM revenue_events WHERE 1=1"
    params: list = []
    if project_key: sql += " AND project_key = ?"; params.append(project_key)
    if source_type: sql += " AND source_type = ?"; params.append(source_type)
    if event_type: sql += " AND event_type = ?"; params.append(event_type)
    if status: sql += " AND status = ?"; params.append(status)
    if since is not None: sql += " AND occurred_at >= ?"; params.append(since)
    if until is not None: sql += " AND occurred_at < ?"; params.append(until)
    sql += " ORDER BY occurred_at DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def revenue_summary(*, project_key: str | None = None, source_type: str | None = None,
                     since: int | None = None, until: int | None = None) -> dict:
    """Aggregated revenue metrics: total, by_source, by_project, by_event_type, by_status."""
    where = "WHERE 1=1"
    params: list = []
    if project_key: where += " AND project_key = ?"; params.append(project_key)
    if source_type: where += " AND source_type = ?"; params.append(source_type)
    if since is not None: where += " AND occurred_at >= ?"; params.append(since)
    if until is not None: where += " AND occurred_at < ?"; params.append(until)

    sql_total = f"SELECT COALESCE(SUM(amount_cents), 0), COUNT(*) FROM revenue_events {where}"
    sql_by_source = f"""SELECT source_type, COALESCE(SUM(amount_cents), 0) as total, COUNT(*) as cnt
                        FROM revenue_events {where} GROUP BY source_type"""
    sql_by_project = f"""SELECT project_key, COALESCE(SUM(amount_cents), 0) as total, COUNT(*) as cnt
                         FROM revenue_events {where} GROUP BY project_key ORDER BY total DESC"""
    sql_by_type = f"""SELECT event_type, COALESCE(SUM(amount_cents), 0) as total, COUNT(*) as cnt
                      FROM revenue_events {where} GROUP BY event_type ORDER BY total DESC"""
    sql_by_status = f"""SELECT status, COALESCE(SUM(amount_cents), 0) as total, COUNT(*) as cnt
                        FROM revenue_events {where} GROUP BY status"""

    with _dbmod.db() as conn:
        total, count = conn.execute(sql_total, params).fetchone()
        by_source = [dict(r) for r in conn.execute(sql_by_source, params).fetchall()]
        by_project = [dict(r) for r in conn.execute(sql_by_project, params).fetchall()]
        by_type = [dict(r) for r in conn.execute(sql_by_type, params).fetchall()]
        by_status = [dict(r) for r in conn.execute(sql_by_status, params).fetchall()]
    return {
        "total_cents": int(total or 0),
        "count": int(count or 0),
        "by_source": by_source,
        "by_project": by_project,
        "by_type": by_type,
        "by_status": by_status,
    }


def revenue_daily_buckets(*, project_key: str | None = None, source_type: str | None = None,
                          since: int | None = None, until: int | None = None,
                          bucket_days: int = 1) -> list[dict]:
    """Time-series: total amount grouped by day (UTC). bucket_days>1 for weekly/monthly."""
    where = "WHERE 1=1"
    params: list = []
    if project_key: where += " AND project_key = ?"; params.append(project_key)
    if source_type: where += " AND source_type = ?"; params.append(source_type)
    if since is not None: where += " AND occurred_at >= ?"; params.append(since)
    if until is not None: where += " AND occurred_at < ?"; params.append(until)

    # Truncate to day boundary (UTC). bucket_days could be 1/7/30 for day/week/month.
    sql = f"""
        SELECT (occurred_at / 86400) * 86400 as day_start,
               COALESCE(SUM(amount_cents), 0) as total,
               COUNT(*) as cnt
        FROM revenue_events
        {where}
        GROUP BY day_start
        ORDER BY day_start
    """
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_revenue_source_by_name(name: str) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM revenue_sources WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


# ============================================================
# Forecasts (linear regression on time-series)
# ============================================================

def revenue_forecast(*, days: int = 30, project_key: str | None = None) -> dict:
    """Linear regression on daily revenue totals. Returns predicted next `days` days.

    Uses ordinary least squares: y = a + b * x where x is day index.
    Returns: history (last 90d), predicted_next (next `days` days with date + cents),
             slope_per_day_cents, monthly_projection_cents, r_squared.
    """
    import datetime as dt
    # Pull last 90 days of daily totals
    since = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    sql = "SELECT date(occurred_at, 'unixepoch') as d, SUM(amount_cents) as total FROM revenue_events WHERE date(occurred_at, 'unixepoch') >= ?"
    params: list = [since]
    if project_key:
        sql += " AND project_key = ?"
        params.append(project_key)
    sql += " GROUP BY d ORDER BY d"
    with _dbmod.db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not rows:
        return {"history": [], "predicted_next": [], "slope_per_day_cents": 0,
                "monthly_projection_cents": 0, "r_squared": 0, "sample_size": 0}
    # Convert date strings to day index (0-based from earliest in window)
    base_date = dt.datetime.strptime(rows[0]["d"], "%Y-%m-%d").date()
    history: list[dict] = []
    xs: list[int] = []
    ys: list[int] = []
    for r in rows:
        d = dt.datetime.strptime(r["d"], "%Y-%m-%d").date()
        x = (d - base_date).days
        y = int(r["total"] or 0)
        history.append({"date": r["d"], "x": x, "y": y})
        xs.append(x); ys.append(y)
    n = len(xs)
    if n < 2:
        return {"history": history, "predicted_next": [], "slope_per_day_cents": 0,
                "monthly_projection_cents": 0, "r_squared": 0, "sample_size": n}
    # OLS: y = a + b * x
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    slope = num / den if den > 0 else 0
    intercept = mean_y - slope * mean_x
    # R²
    ss_res = sum((ys[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
    ss_tot = sum((ys[i] - mean_y) ** 2 for i in range(n))
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
    # Predict next `days` days
    last_x = xs[-1]
    last_date = dt.datetime.strptime(rows[-1]["d"], "%Y-%m-%d").date()
    predicted_next = []
    total_predicted = 0
    for i in range(1, days + 1):
        x = last_x + i
        d = last_date + dt.timedelta(days=i)
        y = max(0, int(intercept + slope * x))
        predicted_next.append({"date": d.isoformat(), "x": x, "y": y})
        total_predicted += y
    return {
        "history": history,
        "predicted_next": predicted_next,
        "slope_per_day_cents": round(slope),
        "monthly_projection_cents": round(slope * 30 + intercept),
        "r_squared": round(r_squared, 3),
        "sample_size": n,
        "base_date": rows[0]["d"],
        "last_date": rows[-1]["d"],
    }


def ga_forecast(*, days: int = 30, metric: str = "users", project_key: str | None = None) -> dict:
    """Same as revenue_forecast but on GA4 daily users (or page_views)."""
    import datetime as dt
    since = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    sql = f"SELECT date, SUM({metric}) as total FROM ga_metrics_daily WHERE date >= ?"
    params: list = [since]
    if project_key:
        sql += " AND project_key = ?"
        params.append(project_key)
    sql += " GROUP BY date ORDER BY date"
    with _dbmod.db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not rows:
        return {"history": [], "predicted_next": [], "slope_per_day": 0,
                "monthly_projection": 0, "r_squared": 0, "sample_size": 0, "metric": metric}
    base_date = dt.datetime.strptime(rows[0]["date"], "%Y-%m-%d").date()  # ga_metrics_daily.date is already YYYY-MM-DD
    history, xs, ys = [], [], []
    for r in rows:
        d = dt.datetime.strptime(r["date"], "%Y-%m-%d").date()
        x = (d - base_date).days
        y = int(r["total"] or 0)
        history.append({"date": r["date"], "x": x, "y": y})
        xs.append(x); ys.append(y)
    n = len(xs)
    if n < 2:
        return {"history": history, "predicted_next": [], "slope_per_day": 0,
                "monthly_projection": 0, "r_squared": 0, "sample_size": n, "metric": metric}
    mean_x = sum(xs) / n; mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    slope = num / den if den > 0 else 0
    intercept = mean_y - slope * mean_x
    ss_res = sum((ys[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
    ss_tot = sum((ys[i] - mean_y) ** 2 for i in range(n))
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
    last_x = xs[-1]
    last_date = dt.datetime.strptime(rows[-1]["date"], "%Y-%m-%d").date()
    predicted_next = []
    total_predicted = 0
    for i in range(1, days + 1):
        x = last_x + i
        d = last_date + dt.timedelta(days=i)
        y = max(0, int(intercept + slope * x))
        predicted_next.append({"date": d.isoformat(), "x": x, "y": y})
        total_predicted += y
    return {
        "history": history, "predicted_next": predicted_next,
        "slope_per_day": round(slope), "monthly_projection": round(slope * 30 + intercept),
        "r_squared": round(r_squared, 3), "sample_size": n, "metric": metric,
        "base_date": rows[0]["date"], "last_date": rows[-1]["date"],
    }


# ============================================================
# Cohort analysis — new vs returning customers, retention
# ============================================================

def _month_diff(yyyymm1: str, yyyymm2: str) -> int:
    """Months between two YYYY-MM strings (positive if yyyymm2 > yyyymm1)."""
    y1, m1 = map(int, yyyymm1.split("-"))
    y2, m2 = map(int, yyyymm2.split("-"))
    return (y2 - y1) * 12 + (m2 - m1)


def _add_months(yyyymm: str, n: int) -> str:
    """Add n months to a YYYY-MM string, return YYYY-MM."""
    y, m = map(int, yyyymm.split("-"))
    total = y * 12 + (m - 1) + n
    new_y, new_m = divmod(total, 12)
    return f"{new_y:04d}-{new_m + 1:02d}"


def customer_cohorts(*, project_key: str | None = None, months: int = 6) -> dict:
    """Build a retention cohort table.

    Logic:
    1. For each customer (customer_ref) in revenue_events, find their first month
       of activity → that's their cohort.
    2. For each cohort month and each subsequent month, count unique customers
       who had at least one event in that month.
    3. Retention % = active_in_month_N / cohort_size.

    Returns:
    - cohorts: list of {cohort: 'YYYY-MM', size: N, retention: [pct_m0, pct_m1, ...]}
    - summary: total_customers, new_this_month, returning_this_month
    """
    import datetime as dt
    sql = """SELECT customer_ref, project_key,
                     strftime('%Y-%m', MIN(occurred_at), 'unixepoch') as cohort,
                     strftime('%Y-%m', occurred_at, 'unixepoch') as month
              FROM revenue_events
              WHERE customer_ref IS NOT NULL AND customer_ref != ''"""
    params: list = []
    if project_key:
        sql += " AND project_key = ?"; params.append(project_key)
    sql += " GROUP BY customer_ref, project_key, strftime('%Y-%m', occurred_at, 'unixepoch') ORDER BY cohort, month"
    with _dbmod.db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not rows:
        return {"cohorts": [], "summary": {"total_customers": 0, "new_this_month": 0, "returning_this_month": 0}}
    # Group by cohort
    cohort_data: dict[str, set[str]] = {}  # cohort_month -> set of customer_refs
    activity: dict[tuple[str, str], set[str]] = {}  # (cohort, month) -> set of customers
    for r in rows:
        cohort = r["cohort"]
        month = r["month"]
        cust = r["customer_ref"]
        cohort_data.setdefault(cohort, set()).add(cust)
        activity.setdefault((cohort, month), set()).add(cust)

    # Build cohort list sorted ascending
    sorted_cohorts = sorted(cohort_data.keys())[-months:]
    cohort_list = []
    for cohort in sorted_cohorts:
        size = len(cohort_data[cohort])
        # Audit 2026-06-23: was O(N^2) — for each cohort, scanned ALL
        # (cohort, month) keys looking for ones matching this cohort.
        # Replaced with a precomputed per-cohort month map built in O(N)
        # outside the loop, then each retention lookup is O(1).
        cohort_months_map: dict[str, set[str]] = {}
        for (c, m), customers in activity.items():
            if c == cohort:
                cohort_months_map.setdefault(m, set()).update(customers)
        cohort_months = list(cohort_months_map.keys())
        max_offset = max((_month_diff(cohort, m) + 1) for m in cohort_months) if cohort_months else 1
        retention = []
        for offset in range(max_offset):
            target_month = _add_months(cohort, offset)
            # Sum active customers across all months that start with target_month
            active_count = sum(len(s) for m, s in cohort_months_map.items() if m.startswith(target_month))
            retention.append(round((active_count / size) * 100, 1) if size > 0 else 0)
        cohort_list.append({
            "cohort": cohort,
            "size": size,
            "retention": retention,
        })

    # Summary
    now_month = dt.date.today().isoformat()[:7]
    this_month_customers = set()
    last_month_customers = set()
    for r in rows:
        if r["month"].startswith(now_month):
            this_month_customers.add(r["customer_ref"])
        # Approximate last month
        last_month = (dt.date.today() - dt.timedelta(days=30)).isoformat()[:7]
        if r["month"].startswith(last_month):
            last_month_customers.add(r["customer_ref"])
    # New = first appeared this month
    new_this_month = set()
    for cust in this_month_customers:
        first = min((r["cohort"] for r in rows if r["customer_ref"] == cust), default=None)
        if first and first.startswith(now_month):
            new_this_month.add(cust)
    returning_this_month = this_month_customers - new_this_month

    return {
        "cohorts": cohort_list,
        "summary": {
            "total_customers": len({r["customer_ref"] for r in rows if r["customer_ref"]}),
            "new_this_month": len(new_this_month),
            "returning_this_month": len(returning_this_month),
            "active_this_month": len(this_month_customers),
        }
    }


