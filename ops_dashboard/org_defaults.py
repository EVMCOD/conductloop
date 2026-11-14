"""Default reporting lines, where the person never drew one.

The starter C-Suite was added with nobody reporting to anybody (manager_id
NULL for all twelve), and so were the project CEOs: the agent panel had no
tree to draw — a Holding CEO with nothing under it — and team mode had no
line to delegate along.

This fills in the obvious lines, and only where a member has none:

* the C-level (CSO, CFO, CTO, CMO, COO, General Counsel, Heads of Product,
  Sales and Customer Success) and the project CEOs report to the Holding CEO;
* the VPs report to the CTO (to the Holding CEO when there is no CTO);
* an AI agent in a team that has a CEO reports to that CEO.

People (type 'human') are never moved, a line the person drew is never
changed, and nothing that would close a loop is written.
"""
from __future__ import annotations

import sqlite3

C_LEVEL = {"cso", "cfo", "cto", "cmo", "coo", "general_counsel", "head_product",
           "head_sales", "head_cs"}


def _role(row) -> str:
    return (row["role"] or "").strip().lower()


def _is_ceo(row) -> bool:
    return _role(row) == "ceo" or ((row["level"] or "") == "ceo" and _role(row) not in C_LEVEL)


def _holding_id(conn: sqlite3.Connection, ai: list) -> int | None:
    """The chosen Holding CEO; else an AI agent named so; else None."""
    try:
        row = conn.execute("SELECT holding_ceo_id FROM boardroom_settings WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    ids = {r["id"] for r in ai}
    if row and row[0] in ids:
        return int(row[0])
    named = [r["id"] for r in ai if (r["name"] or "").strip().lower() == "holding ceo"]
    return named[0] if named else None


def _ancestors(conn: sqlite3.Connection, member_id: int) -> set[int]:
    seen: set[int] = set()
    cur = member_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = conn.execute("SELECT manager_id FROM team_members WHERE id = ?", (cur,)).fetchone()
        cur = row[0] if row else None
    return seen


def fill_missing_managers(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """Write the default lines that are missing. Returns [(member_id, manager_id)]."""
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return _fill(conn)
    finally:
        conn.row_factory = previous


def _fill(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    ai = conn.execute(
        "SELECT id, name, role, level, team_id, manager_id FROM team_members "
        "WHERE type = 'ai_agent' ORDER BY id").fetchall()
    if not ai:
        return []
    holding = _holding_id(conn, ai)
    ctos = [r["id"] for r in ai if _role(r) == "cto"]
    cto = ctos[0] if ctos else None
    team_ceo: dict[int, int] = {}
    for r in ai:
        if _is_ceo(r) and r["id"] != holding and r["team_id"] is not None:
            team_ceo.setdefault(r["team_id"], r["id"])

    written: list[tuple[int, int]] = []
    for r in ai:
        if r["manager_id"] is not None or r["id"] == holding:
            continue
        role = _role(r)
        if role in C_LEVEL or _is_ceo(r):
            target = holding
        elif role.startswith("vp_"):
            target = cto or holding
        else:
            target = team_ceo.get(r["team_id"])
        if target is None or target == r["id"] or r["id"] in _ancestors(conn, target):
            continue
        conn.execute("UPDATE team_members SET manager_id = ? WHERE id = ? AND manager_id IS NULL",
                     (target, r["id"]))
        written.append((r["id"], target))
    return written
