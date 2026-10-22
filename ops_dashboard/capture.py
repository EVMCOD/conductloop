"""Quick-capture inbox.

A frictionless dump zone: throw a URL, a thought, an idea, or a task at
ConductLoop from anywhere (API / CLI / bookmarklet) and triage it later. Rows
land unprocessed in the `captures` table; `process()` marks them done and can
route them onward (e.g. an idea -> the ideas list).
"""
from __future__ import annotations

import logging
import time

from . import db

log = logging.getLogger("conductloop.capture")

_KINDS = {"note", "url", "idea", "task"}


def add(content: str, *, kind: str = "note", source: str | None = None,
        url: str | None = None) -> int:
    content = (content or "").strip()
    if not content:
        raise ValueError("capture content is empty")
    if kind not in _KINDS:
        kind = "note"
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO captures (content, kind, source, url, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (content, kind, source, url, int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def list_captures(*, only_open: bool = True, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM captures"
    if only_open:
        sql += " WHERE processed = 0"
    sql += " ORDER BY created_at DESC LIMIT ?"
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]


def process(capture_id: int, *, route: str | None = None) -> dict:
    """Mark a capture processed. Optional route: 'idea' files it into the ideas
    list (if available). Returns {ok, id, routed}."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "not found"}
        conn.execute("UPDATE captures SET processed = 1, processed_at = ? WHERE id = ?",
                     (int(time.time()), capture_id))
        conn.commit()
    routed = None
    if route == "idea" and hasattr(db, "add_idea"):
        try:
            db.add_idea(row["content"])
            routed = "idea"
        except Exception:
            log.warning("could not route capture %s to ideas", capture_id, exc_info=True)
    return {"ok": True, "id": capture_id, "routed": routed}


def stats() -> dict:
    with db.get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
        open_ = conn.execute("SELECT COUNT(*) FROM captures WHERE processed = 0").fetchone()[0]
    return {"total": total, "open": open_, "processed": total - open_}
