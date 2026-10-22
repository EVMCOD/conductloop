"""Team + org membership helpers.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db``.
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ---- Team helpers ----

def create_team(key: str, name: str, description: str = None, type: str = "mixed",
               color: str = None, icon: str = None) -> int:
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO teams (key, name, description, type, color, icon, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, name, description, type, color, icon, int(time.time()), int(time.time())),
        )
        return cur.lastrowid


def list_teams() -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT t.*, "
            " (SELECT COUNT(*) FROM team_members m WHERE m.team_id = t.id AND m.status = 'active') AS member_count, "
            " (SELECT COUNT(*) FROM user_todos ut WHERE ut.project_key = t.key AND ut.status = 'open') AS open_todos "
            "FROM teams t ORDER BY t.type, t.name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_team(team_id: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not row: return None
    d = dict(row)
    with _dbmod.db() as conn:
        d["members"] = [dict(r) for r in conn.execute(
            "SELECT * FROM team_members WHERE team_id = ? ORDER BY type, role, name",
            (team_id,),
        ).fetchall()]
    return d


def update_team(team_id: int, **fields) -> bool:
    if not fields: return False
    allowed = {"name", "description", "type", "color", "icon"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed: sets.append(f"{k} = ?"); params.append(v)
    if not sets: return False
    sets.append("updated_at = ?"); params.append(int(time.time())); params.append(team_id)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE teams SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete_team(team_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        return cur.rowcount > 0


def add_member(team_id: int, name: str, type: str = "human", role: str = None,
               status: str = "active", contact: str = None, capabilities: str = None,
               model: str = None, notes: str = None,
               level: str = "agent", manager_id: int | None = None) -> int:
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, contact, capabilities, model, notes, level, manager_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, name, type, role, status, contact, capabilities, model, notes,
             level, manager_id,
             int(time.time()), int(time.time())),
        )
        return cur.lastrowid


def list_members(team_id: int | None = None) -> list[dict]:
    with _dbmod.db() as conn:
        if team_id:
            rows = conn.execute(
                "SELECT * FROM team_members WHERE team_id = ? ORDER BY type, role, name",
                (team_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM team_members ORDER BY team_id, type, role, name"
            ).fetchall()
    return [dict(r) for r in rows]


def get_member(member_id: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (member_id,)).fetchone()
    if not row: return None
    d = dict(row)
    with _dbmod.db() as conn:
        d["open_todos"] = [dict(r) for r in conn.execute(
            "SELECT * FROM user_todos WHERE assignee_id = ? AND status = 'open' ORDER BY priority DESC, due_date, created_at",
            (member_id,),
        ).fetchall()]
    return d


def update_member(member_id: int, **fields) -> bool:
    if not fields: return False
    allowed = {"name", "type", "role", "status", "contact", "capabilities", "model", "notes",
               "level", "manager_id", "team_id"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed: sets.append(f"{k} = ?"); params.append(v)
    if not sets: return False
    sets.append("updated_at = ?"); params.append(int(time.time())); params.append(member_id)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE team_members SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete_member(member_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM team_members WHERE id = ?", (member_id,))
        return cur.rowcount > 0


def seed_default_teams() -> dict:
    """Seed the built-in AI squad (product agents) if not already present.

    Identity-neutral: seeds no companies and no personal owner. A fresh
    install starts blank — the user adds their own teams/companies via the
    UI. The only seeded team is the product's built-in AI agent squad, which
    powers the brain / indexer / webhook / daily-note / snapshot features.
    """
    defaults = [
        ("ai-squad", "AI Squad", "ai_squad", "🤖",
         "Built-in AI agents: brain, indexer, embedder, webhooks, daily note."),
    ]
    default_members = [
        ("ai-squad", "Brain",           "ai_agent", "agent",    "active", None,
         "streaming chat, RAG, function calling, Ollama/Anthropic/OpenAI", "ollama:llama3.2"),
        ("ai-squad", "Indexer System",  "ai_agent", "agent",    "active", None,
         "index vault, repos, memory, embeddings", "all-MiniLM-L6-v2"),
        ("ai-squad", "Webhook Bot",     "ai_agent", "observer", "active", None,
         "Slack/Discord/Telegram notifications on URL changes", "system"),
        ("ai-squad", "Daily Note",      "ai_agent", "agent",    "active", None,
         "generate daily markdown note in vault", "system"),
        ("ai-squad", "Snapshot Engine", "ai_agent", "agent",    "active", None,
         "daily diffs, weekly reports", "system"),
    ]
    created = {"teams": 0, "members": 0}
    with _dbmod.db() as conn:
        existing_teams = {r[0]: r[1] for r in conn.execute("SELECT key, id FROM teams").fetchall()}
        for key, name, type_, icon, desc in defaults:
            if key in existing_teams: continue
            cur = conn.execute(
                "INSERT INTO teams (key, name, description, type, color, icon, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, name, desc, type_, None, icon, int(time.time()), int(time.time())),
            )
            existing_teams[key] = cur.lastrowid
            created["teams"] += 1
        # Dedupe by (team_id, name): only add if not already there
        existing_members = {(r[1], r[0]) for r in conn.execute("SELECT team_id, name FROM team_members").fetchall()}
        for team_key, name, type_, role, status, contact, *rest in default_members:
            capabilities = rest[0] if rest else None
            model = rest[1] if len(rest) > 1 else None
            tid = existing_teams.get(team_key)
            if not tid: continue
            if (tid, name) in existing_members: continue
            conn.execute(
                "INSERT INTO team_members (team_id, name, type, role, status, contact, capabilities, model, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, name, type_, role, status, contact, capabilities, model,
                 int(time.time()), int(time.time())),
            )
            existing_members.add((tid, name))
            created["members"] += 1
    return created
