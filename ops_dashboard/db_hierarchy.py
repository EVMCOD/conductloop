"""Org hierarchy: manager_id + level tree.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

from ops_dashboard import db as _dbmod

# ============================================================================
# Hierarchy (manager_id + level) — the org tree
# ============================================================================

LEVEL_ORDER = ["observer", "agent", "manager", "director", "vp", "ceo"]


def set_member_manager(member_id: int, manager_id: int | None) -> bool:
    """Set a member's manager. Validates no cycle (manager_id can't be a descendant)."""
    if manager_id == member_id:
        return False
    if manager_id is not None:
        # Walk up the proposed manager's chain; if we hit member_id, cycle.
        cur = manager_id
        seen: set[int] = set()
        while cur is not None and cur not in seen:
            if cur == member_id:
                return False  # cycle
            seen.add(cur)
            with _dbmod.db() as conn:
                row = conn.execute("SELECT manager_id FROM team_members WHERE id = ?", (cur,)).fetchone()
            cur = row["manager_id"] if row else None
    with _dbmod.db() as conn:
        conn.execute("UPDATE team_members SET manager_id = ? WHERE id = ?", (manager_id, member_id))
        conn.commit()
    return True


def set_member_level(member_id: int, level: str) -> bool:
    if level not in LEVEL_ORDER:
        return False
    with _dbmod.db() as conn:
        conn.execute("UPDATE team_members SET level = ? WHERE id = ?", (level, member_id))
        conn.commit()
    return True


def get_member_reports(member_id: int) -> list[dict]:
    """Members that report to this one (direct children)."""
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT * FROM team_members WHERE manager_id = ? ORDER BY level, name",
            (member_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_member_chain(member_id: int) -> list[dict]:
    """Walk up to the root: [self, parent, grandparent, ...]."""
    chain = []
    cur = member_id
    seen: set[int] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        with _dbmod.db() as conn:
            row = conn.execute("SELECT * FROM team_members WHERE id = ?", (cur,)).fetchone()
        if not row:
            break
        chain.append(dict(row))
        cur = row["manager_id"]
    return chain


def get_org_tree(root_id: int | None = None) -> dict:
    """Return the full org tree as nested dicts. root_id=None returns the
    forest (all top-level members)."""
    with _dbmod.db() as conn:
        if root_id is None:
            rows = conn.execute(
                "SELECT * FROM team_members WHERE manager_id IS NULL ORDER BY level, name"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM team_members WHERE id = ?", (root_id,)).fetchall()
        members = [dict(r) for r in rows]
        # All members indexed by id
        all_rows = conn.execute("SELECT * FROM team_members").fetchall()
        by_id = {r["id"]: dict(r) for r in all_rows}
    children_of: dict[int, list[int]] = {}
    for m in by_id.values():
        if m["manager_id"] is not None:
            children_of.setdefault(m["manager_id"], []).append(m["id"])
    def build(mid: int) -> dict:
        m = by_id[mid]
        reports_ids = children_of.get(mid, [])
        return {
            "id": m["id"],
            "name": m["name"],
            "type": m["type"],
            "role": m["role"],
            "level": m.get("level") or "agent",
            "model": m.get("model"),
            "status": m.get("status") or "active",
            "workload": m.get("workload") or 0,
            "manager_id": m.get("manager_id"),
            "reports": [build(rid) for rid in sorted(reports_ids, key=lambda i: by_id[i].get("level") or "agent")],
        }
    forest = [build(m["id"]) for m in members]
    return {"tree": forest, "total": len(by_id)}


def hierarchy_balance_suggestions() -> list[dict]:
    """Heuristic: members at director/manager level with high workload and 0 reports
    are candidates for delegating down; members with many reports are doing well."""
    with _dbmod.db() as conn:
        rows = conn.execute("""
            SELECT m.*,
                (SELECT COUNT(*) FROM team_members r WHERE r.manager_id = m.id) AS report_count,
                (SELECT COUNT(*) FROM team_members r WHERE r.manager_id = m.id
                 AND r.status = 'active' AND r.type = 'ai_agent') AS agent_report_count
            FROM team_members m
            WHERE m.type = 'ai_agent' AND m.level IN ('director', 'manager', 'vp')
            ORDER BY m.workload DESC, m.name
        """).fetchall()
    suggestions = []
    for r in rows:
        d = dict(r)
        wl = d.get("workload") or 0
        if wl >= 2 and d.get("report_count", 0) == 0:
            suggestions.append({
                "type": "overloaded_no_reports",
                "member_id": d["id"],
                "member_name": d["name"],
                "level": d["level"],
                "workload": wl,
                "report_count": 0,
                "message": f"{d['name']} has {wl} open todos but no reports — consider creating a sub-agent or delegating to existing agents.",
            })
    return suggestions
