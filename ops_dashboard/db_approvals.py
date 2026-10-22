"""Approval queue: agents propose writes, the user approves or rejects.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import json
import time

from ops_dashboard import db as _dbmod

# ============================================================
# Approval queue: agents propose writes, the user approves/rejects
# ============================================================

def add_approval(agent_id: int, action: str, payload: dict, *, reason: str | None = None) -> int:
    """An agent proposes a write. Returns the approval id."""
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO agent_logs
               (agent_id, action, summary, details, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (agent_id, "approval_proposed", reason or action,
             json.dumps({"approval_action": action, "approval_payload": payload,
                         "status": "pending"}), now),
        )
        return int(cur.lastrowid or 0)


def list_pending_approvals(agent_id: int | None = None) -> list[dict]:
    """Return pending approval rows from agent_logs (action='approval_proposed',
    details.status='pending')."""
    sql = """SELECT al.*, m.name AS agent_name, m.role AS agent_role
             FROM agent_logs al
             LEFT JOIN team_members m ON al.agent_id = m.id
             WHERE al.action = 'approval_proposed'
               AND json_extract(al.details, '$.status') = 'pending'"""
    params: list = []
    if agent_id:
        sql += " AND al.agent_id = ?"; params.append(agent_id)
    sql += " ORDER BY al.created_at DESC LIMIT 50"
    with _dbmod.db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["details"] = json.loads(d.get("details") or "{}")
        except Exception: d["details"] = {}
        d["approval_action"] = d.get("details", {}).get("approval_action")
        d["approval_payload"] = d.get("details", {}).get("approval_payload")
        d["status"] = d.get("details", {}).get("status", "pending")
        out.append(d)
    return out


def resolve_approval(log_id: int, *, action: str) -> bool:
    """Mark an approval as approved or rejected."""
    if action not in ("approve", "reject"):
        return False
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT details FROM agent_logs WHERE id = ? AND action = 'approval_proposed'",
            (log_id,),
        ).fetchone()
        if not row:
            return False
        try: details = json.loads(row["details"] or "{}")
        except Exception: details = {}
        details["status"] = "approved" if action == "approve" else "rejected"
        details["resolved_at"] = int(time.time())
        conn.execute(
            "UPDATE agent_logs SET details = ? WHERE id = ?",
            (json.dumps(details), log_id),
        )
    return True


def list_pending_approvals_count() -> int:
    """Quick count for sidebar badge."""
    with _dbmod.db() as conn:
        return conn.execute(
            """SELECT COUNT(*) AS c FROM agent_logs
               WHERE action = 'approval_proposed'
                 AND json_extract(details, '$.status') = 'pending'"""
        ).fetchone()["c"]
