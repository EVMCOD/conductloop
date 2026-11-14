"""Cost tracker entries.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

from ops_dashboard import db as _dbmod

# ============================================================
# Costs tracker
# ============================================================

def add_cost(
    provider: str,
    amount_cents: int,
    *,
    period: str | None = None,
    project_key: str | None = None,
    category: str = "infra",
    currency: str = "EUR",
    description: str | None = None,
    billed_at: int | None = None,
) -> int:
    now = int(__import__("time").time())
    if not period:
        from datetime import datetime
        period = datetime.fromtimestamp(billed_at or now).strftime("%Y-%m")
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO costs
               (project_key, provider, category, amount_cents, currency, description, period, billed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_key, provider, category, amount_cents, currency,
                description, period, billed_at or now, now,
            ),
        )
        return int(cur.lastrowid or 0)


def list_costs(
    *,
    period: str | None = None,
    project: str | None = None,
    provider: str | None = None,
    limit: int = 200,
) -> list[dict]:
    sql = "SELECT * FROM costs WHERE 1=1"
    params: list = []
    if period:
        sql += " AND period = ?"
        params.append(period)
    if project:
        sql += " AND project_key = ?"
        params.append(project)
    if provider:
        sql += " AND provider = ?"
        params.append(provider)
    sql += " ORDER BY billed_at DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def cost_summary(period: str | None = None) -> dict:
    """Aggregate costs by provider, project, and category for a given month.

    Each row in by_provider/by_project/by_category has both `cents` (raw int)
    and `total_cents` (alias), `c` (count), and `currency` for display.
    """
    sql = """SELECT provider, category, currency,
                    SUM(amount_cents) AS cents,
                    SUM(amount_cents) AS total_cents,
                    COUNT(*) AS c
             FROM costs"""
    params: list = []
    if period:
        sql += " WHERE period = ?"
        params.append(period)
    sql += " GROUP BY provider, category, currency ORDER BY cents DESC"
    with _dbmod.db() as conn:
        by_provider = [dict(r) for r in conn.execute(sql, params).fetchall()]
        sql2 = """SELECT project_key, currency,
                          SUM(amount_cents) AS cents,
                          SUM(amount_cents) AS total_cents,
                          COUNT(*) AS c
                   FROM costs"""
        params2: list = []
        if period:
            sql2 += " WHERE period = ?"
            params2.append(period)
        sql2 += " GROUP BY project_key, currency ORDER BY cents DESC"
        by_project = [dict(r) for r in conn.execute(sql2, params2).fetchall()]
        sql3 = """SELECT category, currency,
                          SUM(amount_cents) AS cents,
                          SUM(amount_cents) AS total_cents,
                          COUNT(*) AS c
                   FROM costs"""
        params3: list = []
        if period:
            sql3 += " WHERE period = ?"
            params3.append(period)
        sql3 += " GROUP BY category, currency ORDER BY cents DESC"
        by_category = [dict(r) for r in conn.execute(sql3, params3).fetchall()]
        sql4 = "SELECT SUM(amount_cents) AS cents FROM costs"
        params4: list = []
        if period:
            sql4 += " WHERE period = ?"
            params4.append(period)
        total = conn.execute(sql4, params4).fetchone()["cents"] or 0
        periods = [r["period"] for r in conn.execute(
            "SELECT DISTINCT period FROM costs ORDER BY period DESC LIMIT 24"
        ).fetchall()]
    return {
        "period": period,
        "total_cents": total,
        "by_provider": by_provider,
        "by_project": by_project,
        "by_category": by_category,
        "periods": periods,
    }


def delete_cost(cost_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM costs WHERE id = ?", (cost_id,))
        return cur.rowcount > 0
