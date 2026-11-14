"""Note backlinks: forward/back links between indexed documents.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so
``db.<name>`` callers keep working. Uses the shared transaction context
manager via ``_dbmod.db()`` (module-reference import — safe mid-import).
"""
from __future__ import annotations

import time

from ops_dashboard import db as _dbmod

# ============================================================
# Note links (backlinks)
# ============================================================

def add_note_link(source_doc_id: int, target_doc_id: int, link_type: str = "mention",
                   context: str | None = None, created_at: int | None = None) -> int | None:
    """Insert a link. Idempotent on (source, target, link_type). Returns id or None if dup."""
    if source_doc_id == target_doc_id:
        return None  # don't link to self
    now = created_at or int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO note_links
               (source_doc_id, target_doc_id, link_type, context, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (source_doc_id, target_doc_id, link_type, context, now),
        )
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid or 0)


def get_backlinks(doc_id: int, *, limit: int = 50) -> list[dict]:
    """Get all docs that link TO this doc (backlinks).
    Returns [{source_doc_id, source_title, source_path, link_type, context}]"""
    with _dbmod.db() as conn:
        rows = conn.execute(
            """SELECT nl.source_doc_id, nl.link_type, nl.context,
                      d.title as source_title, d.path as source_path,
                      d.source as source_source
               FROM note_links nl
               JOIN documents d ON d.id = nl.source_doc_id
               WHERE nl.target_doc_id = ?
               ORDER BY nl.created_at DESC
               LIMIT ?""",
            (doc_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_outgoing_links(doc_id: int, *, limit: int = 50) -> list[dict]:
    """Get all docs that this doc links TO."""
    with _dbmod.db() as conn:
        rows = conn.execute(
            """SELECT nl.target_doc_id, nl.link_type, nl.context,
                      d.title as target_title, d.path as target_path
               FROM note_links nl
               JOIN documents d ON d.id = nl.target_doc_id
               WHERE nl.source_doc_id = ?
               ORDER BY nl.created_at DESC
               LIMIT ?""",
            (doc_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_doc_id_by_path(path: str) -> int | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT id FROM documents WHERE path = ? LIMIT 1", (path,)).fetchone()
        return int(row["id"]) if row else None


def get_doc_id_by_title(title: str) -> int | None:
    if not title: return None
    with _dbmod.db() as conn:
        row = conn.execute("SELECT id FROM documents WHERE lower(title) = lower(?) LIMIT 1", (title,)).fetchone()
        return int(row["id"]) if row else None


def count_links() -> dict:
    with _dbmod.db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM note_links").fetchone()[0]
        by_type = [dict(r) for r in conn.execute(
            "SELECT link_type, COUNT(*) as cnt FROM note_links GROUP BY link_type ORDER BY cnt DESC"
        ).fetchall()]
        docs_with_links = conn.execute(
            "SELECT COUNT(DISTINCT source_doc_id) FROM note_links"
        ).fetchone()[0]
    return {"total": total, "by_type": by_type, "docs_with_outgoing": docs_with_links}
