"""Brain RAG search + conversation helpers.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db``.
"""
from __future__ import annotations

import sqlite3
import time

from ops_dashboard import db as _dbmod

# ---- Brain (RAG) helpers ----

def create_conversation(title: str | None = None) -> int:
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title or "new", int(time.time()), int(time.time())),
        )
        return cur.lastrowid


def add_message(conversation_id: int, role: str, content: str, *,
                citations: list[dict] | None = None,
                provider: str | None = None, model: str | None = None) -> int:
    import json as _json
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, citations, provider, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content,
             _json.dumps(citations or [], ensure_ascii=False) if citations else None,
             provider, model, int(time.time())),
        )
        # Touch conversation
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                     (int(time.time()), conversation_id))
        return cur.lastrowid


def save_conversation_turn(*, title: str, user_content: str, assistant_content: str,
                            citations: list[dict] | None = None,
                            provider: str | None = None, model: str | None = None) -> int:
    """Atomic conversation + 2 messages (user + assistant). Audit 2026-06-23.

    Replaces the previous 3-call pattern (create_conversation + add_message +
    add_message) that could leave orphan conversations if a write failed
    mid-sequence. All 3 inserts happen in a single transaction — either all
    commit or all roll back.
    """
    import json as _json
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title or "new", now, now),
        )
        conv_id = int(cur.lastrowid)
        for role, content in (("user", user_content), ("assistant", assistant_content)):
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, citations, provider, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conv_id, role, content,
                 _json.dumps(citations or [], ensure_ascii=False) if role == "assistant" and citations else None,
                 provider, model, now),
            )
        # Final touch so updated_at reflects both writes
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
        conn.commit()
        return conv_id


def list_conversations(limit: int = 20, agent_key: str | None = None) -> list[dict]:
    """Recent conversations, newest first, with their message count.

    ``agent_key`` narrows the list to one agent's threads. The team chat
    stores it as ``"m:<member id>"``; the UI asks for it by member_id. The
    endpoint this feeds used to 404, which left the agent panel on
    "loading…" forever. 2026-10-23.
    """
    base = ("SELECT c.id, c.title, c.created_at, c.updated_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c")
    params: list = []
    sql = base
    if agent_key:
        sql += " JOIN chat_threads t ON t.conversation_id = c.id AND t.agent_key = ?"
        params.append(agent_key)
    sql += " ORDER BY c.updated_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    with _dbmod.db() as conn:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # chat_threads is created on demand by team_chat; a fresh DB may
            # not have it yet. Then that agent simply has no threads — do not
            # fall back to "every conversation", which would show other
            # agents' chats inside the agent panel.
            if agent_key:
                return []
            sql = base + " ORDER BY c.updated_at DESC LIMIT ?"
            rows = conn.execute(sql, [params[-1]]).fetchall()
        return [dict(r) for r in rows]


# Snapshot helpers moved to db_snapshots.py (2026-07-20). Re-exported so `db.<name>` keeps working.
