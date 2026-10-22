"""Voice note transcripts.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

from ops_dashboard import db as _dbmod

# ============================================================
# Voice notes
# ============================================================

def add_voice_note(transcript: str, *, audio_path: str | None = None, duration_sec: float | None = None, project_key: str | None = None) -> int:
    now = int(__import__("time").time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO voice_notes (audio_path, transcript, duration_sec, project_key, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (audio_path, transcript, duration_sec, project_key, now),
        )
        return int(cur.lastrowid or 0)


def list_voice_notes(*, limit: int = 50, project: str | None = None) -> list[dict]:
    sql = "SELECT * FROM voice_notes WHERE 1=1"
    params: list = []
    if project:
        sql += " AND project_key = ?"
        params.append(project)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def search_voice_notes(query: str, *, limit: int = 30) -> list[dict]:
    sql = "SELECT * FROM voice_notes WHERE transcript LIKE ? ORDER BY created_at DESC LIMIT ?"
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, (f"%{query}%", limit)).fetchall()]
