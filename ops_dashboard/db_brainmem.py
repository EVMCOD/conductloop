"""Persistent brain memory.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db``.
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ---- Brain memory helpers ----

def memory_get(key: str) -> str | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT value FROM brain_memory WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def memory_set(key: str, value: str) -> None:
    with _dbmod.db() as conn:
        conn.execute(
            "INSERT INTO brain_memory (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, int(time.time())),
        )


def memory_list() -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM brain_memory ORDER BY key").fetchall()
    return [dict(r) for r in rows]


# Team helpers moved to db_teams.py (2026-07-20). Re-exported so `db.<name>` keeps working.
