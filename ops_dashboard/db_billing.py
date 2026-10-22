"""Founders pricing tiers + founder registry + Stripe payment-event log.

Split out of db.py (2026-07-20). These functions are re-exported from
``ops_dashboard.db`` so existing ``db.<name>`` callers keep working. They use
the shared transaction context manager via ``_dbmod.db()`` — importing the db
module by reference (not its attributes) is safe even while db.py is still
being imported, because Python has already registered the partially-built
module in sys.modules.
"""
from __future__ import annotations

import json
import time

from ops_dashboard import db as _dbmod

# ============================================================
# Dynamic pricing tiers (Founders pricing — local-first, single-user)
# ============================================================
# Pivot 2026-06-23: ConductLoop is local-first. No team/multi-seat. Only one
# paid plan: Lifetime. Price scales with paid-lifetime count (excluding founders
# who paid at the original $300 early-bird rate).
#
# Progression (10x compounding):
#   0-10 sales    $10    (Early traction — anyone can try)
#   11-50 sales   $30    (Beta)
#   51-200        $99    (Launch)
#   201-500       $299   (Early growth)
#   501-2000      $999   (Growth)
#   2001+         $2999  (Final price — full anchor)

PRICING_TIERS = [
    {"max_sales": 10,    "cents":   1000, "label_en": "Early traction",   "label_es": "Tracción inicial",    "pct": 0.3},
    {"max_sales": 50,    "cents":   3000, "label_en": "Beta",             "label_es": "Beta",                "pct": 1},
    {"max_sales": 200,   "cents":   9900, "label_en": "Launch",           "label_es": "Lanzamiento",         "pct": 3},
    {"max_sales": 500,   "cents":  29900, "label_en": "Early growth",     "label_es": "Crecimiento",         "pct": 10},
    {"max_sales": 2000,  "cents":  99900, "label_en": "Growth",           "label_es": "Crecimiento alto",   "pct": 33},
    {"max_sales": None,  "cents": 299900, "label_en": "Final price",      "label_es": "Precio final",        "pct": 100},
]

FINAL_PRICE_CENTS = 299900  # The "anchored" full price (used for marketing only)


def current_tier() -> dict:
    """Return the current pricing tier based on the number of paid lifetime customers.

    Founders (original $300 supporters) are excluded from the public sales count.
    """
    with _dbmod.db() as conn:
        count = conn.execute(
            "SELECT COUNT(DISTINCT e.user_id) FROM entitlements e "
            "LEFT JOIN founders f ON f.user_id = e.user_id "
            "WHERE e.plan = 'lifetime' "
            "AND e.refunded_at IS NULL "
            "AND f.id IS NULL"
        ).fetchone()[0]
    tier = PRICING_TIERS[0]
    for t in PRICING_TIERS:
        max_sales = t.get("max_sales")
        if max_sales is None or count < max_sales:
            tier = t
            break
    if tier.get("max_sales") is None:
        sales_until_next = None
    else:
        sales_until_next = max(0, tier["max_sales"] - count)
    next_label = None
    if tier.get("max_sales") is not None:
        idx = PRICING_TIERS.index(tier)
        if idx + 1 < len(PRICING_TIERS):
            next_label = PRICING_TIERS[idx + 1]["label_en"]
    return {
        "sales_count": count,
        "price_cents": tier["cents"],
        "label_en": tier["label_en"],
        "label_es": tier["label_es"],
        "pct_of_final": tier["pct"],
        "final_price_cents": FINAL_PRICE_CENTS,
        "savings_cents": FINAL_PRICE_CENTS - tier["cents"],
        "sales_until_next_tier": sales_until_next,
        "next_tier_label": next_label,
        "all_tiers": PRICING_TIERS,
        "model": "founders_pricing",
    }


def is_founder(user_id: int) -> bool:
    """True if the user is a founding member (paid at the original $300 early-bird)."""
    with _dbmod.db() as conn:
        row = conn.execute("SELECT 1 FROM founders WHERE user_id = ?", (user_id,)).fetchone()
    return row is not None


def get_founder(user_id: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT f.*, u.email, u.name FROM founders f "
            "JOIN users u ON u.id = f.user_id WHERE f.user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def list_founders() -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT f.*, u.email, u.name FROM founders f "
            "JOIN users u ON u.id = f.user_id ORDER BY f.joined_at"
        ).fetchall()
    return [dict(r) for r in rows]


def register_founder(user_id: int, original_plan: str, original_cents: int,
                      badge_color: str = "gold", note: str | None = None) -> int:
    """Record an existing entitlement holder as a founding member.

    Idempotent: re-registering the same user is a no-op.
    Audit 2026-06-23: validate badge_color (CHECK constraint also enforced
    at the DB layer). Raise ValueError on invalid input rather than silently
    storing garbage.
    """
    allowed = {"gold", "silver", "bronze"}
    if badge_color not in allowed:
        raise ValueError(f"badge_color must be one of {sorted(allowed)}, got {badge_color!r}")
    import time as _t
    now = int(_t.time())
    with _dbmod.db() as conn:
        existing = conn.execute("SELECT id FROM founders WHERE user_id = ?", (user_id,)).fetchone()
        if existing:
            return existing[0]
        cur = conn.execute(
            "INSERT INTO founders (user_id, original_plan, original_cents, badge_color, note, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, original_plan, original_cents, badge_color, note, now),
        )
        conn.commit()
        return cur.lastrowid


def migrate_existing_founders() -> int:
    """One-time migration: register existing pre-pivot lifetime holders as founders.

    Run on startup; idempotent.
    """
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT e.user_id, e.plan, e.amount_cents FROM entitlements e "
            "LEFT JOIN founders f ON f.user_id = e.user_id "
            "WHERE e.plan IN ('lifetime','team_lifetime','small_business_lifetime') "
            "AND e.refunded_at IS NULL AND f.id IS NULL"
        ).fetchall()
    count = 0
    for user_id, plan, amount in rows:
        register_founder(user_id, plan, amount or 0)
        count += 1
    return count




def log_payment_event(*, user_id: int | None, kind: str,
                       stripe_event_id: str | None = None,
                       payload: dict | None = None) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO payment_events (user_id, kind, stripe_event_id, payload, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, kind, stripe_event_id, json.dumps(payload or {}, default=str), now),
        )
        return int(cur.lastrowid or 0)


def is_payment_event_seen(stripe_event_id: str) -> bool:
    """Idempotency check for Stripe webhooks."""
    if not stripe_event_id:
        return False
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT 1 FROM payment_events WHERE stripe_event_id = ? LIMIT 1",
            (stripe_event_id,),
        ).fetchone()
        return bool(row)
