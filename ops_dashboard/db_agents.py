"""Agent memory, triggers, and activity logs.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import json
import time

from ops_dashboard import db as _dbmod

# ============================================================
# Agent memory
# ============================================================

def add_agent_memory(agent_id: int, content: str, *, tags: list[str] | None = None, importance: int = 5) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_memory (agent_id, content, tags, importance, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, content, json.dumps(tags or []), int(importance), now, now),
        )
        return int(cur.lastrowid or 0)


def list_agent_memory(agent_id: int, *, limit: int = 100, search: str | None = None) -> list[dict]:
    sql = "SELECT * FROM agent_memory WHERE agent_id = ?"
    params: list = [agent_id]
    if search:
        sql += " AND content LIKE ?"
        params.append(f"%{search}%")
    sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["tags"] = json.loads(d.get("tags") or "[]")
        except Exception: d["tags"] = []
        out.append(d)
    return out


def top_agent_memory(agent_id: int, *, k: int = 10) -> list[dict]:
    """Return the top-K most important memories for an agent, formatted for system prompt."""
    rows = list_agent_memory(agent_id, limit=k)
    if not rows:
        return []
    out = []
    for r in rows:
        tags = ", ".join(r.get("tags") or [])
        tag_str = f" [{tags}]" if tags else ""
        out.append(f"- ({r['importance']}/10{tag_str}) {r['content'][:300]}")
    return out


def update_agent_memory(memory_id: int, **fields) -> bool:
    allowed = {"content", "tags", "importance"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed: continue
        if k == "tags" and isinstance(v, list):
            v = json.dumps(v)
        sets.append(f"{k} = ?"); params.append(v)
    if not sets: return False
    sets.append("updated_at = ?"); params.append(int(time.time())); params.append(memory_id)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE agent_memory SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete_agent_memory(memory_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM agent_memory WHERE id = ?", (memory_id,))
        return cur.rowcount > 0


# ============================================================
# Agent triggers
# ============================================================

def add_trigger(agent_id: int, kind: str, spec: str, *, prompt: str | None = None, enabled: bool = True) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO agent_triggers
               (agent_id, kind, spec, prompt, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, kind, spec, prompt, int(enabled), now, now),
        )
        return int(cur.lastrowid or 0)


def get_trigger_by_token(token: str) -> dict | None:
    """Look up a webhook trigger by its secret token (in the spec)."""
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT * FROM agent_triggers WHERE kind = 'webhook' AND spec = ? AND enabled = 1",
            (token,),
        ).fetchone()
    return dict(row) if row else None


def list_triggers(agent_id: int | None = None, *, kind: str | None = None, enabled_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM agent_triggers WHERE 1=1"
    params: list = []
    if agent_id:
        sql += " AND agent_id = ?"; params.append(agent_id)
    if kind:
        sql += " AND kind = ?"; params.append(kind)
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY created_at DESC"
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_all_enabled_triggers() -> list[dict]:
    """For the cron loop: all enabled triggers across all agents."""
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM agent_triggers WHERE enabled = 1 ORDER BY last_fired_at IS NOT NULL, last_fired_at"
        ).fetchall()]


def update_trigger(trigger_id: int, **fields) -> bool:
    allowed = {"spec", "prompt", "enabled"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed: continue
        if k == "enabled": v = int(bool(v))
        sets.append(f"{k} = ?"); params.append(v)
    if not sets: return False
    sets.append("updated_at = ?"); params.append(int(time.time())); params.append(trigger_id)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE agent_triggers SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete_trigger(trigger_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM agent_triggers WHERE id = ?", (trigger_id,))
        return cur.rowcount > 0


def record_trigger_fire(trigger_id: int, *, status: str, error: str | None = None) -> None:
    with _dbmod.db() as conn:
        conn.execute(
            """UPDATE agent_triggers
               SET last_fired_at = ?, last_status = ?, last_error = ?,
                   fire_count = fire_count + 1, updated_at = ?
               WHERE id = ?""",
            (int(time.time()), status, error, int(time.time()), trigger_id),
        )


# ============================================================
# Agent activity logs
# ============================================================

def log_agent_action(agent_id: int, action: str, *,
                     trigger_id: int | None = None,
                     task_id: int | None = None,
                     summary: str | None = None,
                     details: dict | None = None,
                     duration_ms: int | None = None) -> int:
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO agent_logs
               (agent_id, trigger_id, task_id, action, summary, details, duration_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, trigger_id, task_id, action, summary,
             json.dumps(details) if details else None, duration_ms, now),
        )
        return int(cur.lastrowid or 0)


def list_agent_logs(agent_id: int | None = None, *, limit: int = 100, action: str | None = None) -> list[dict]:
    sql = "SELECT * FROM agent_logs WHERE 1=1"
    params: list = []
    if agent_id:
        sql += " AND agent_id = ?"; params.append(agent_id)
    if action:
        sql += " AND action = ?"; params.append(action)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["details"] = json.loads(d.get("details") or "null")
        except Exception: d["details"] = None
        out.append(d)
    return out


def recent_agent_activity(hours: int = 24) -> list[dict]:
    since = int(time.time()) - hours * 3600
    sql = """SELECT al.*, m.name AS agent_name, m.role AS agent_role
             FROM agent_logs al
             LEFT JOIN team_members m ON al.agent_id = m.id
             WHERE al.created_at >= ?
             ORDER BY al.created_at DESC LIMIT 200"""
    with _dbmod.db() as conn:
        rows = conn.execute(sql, (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["details"] = json.loads(d.get("details") or "null")
        except Exception: d["details"] = None
        out.append(d)
    return out
