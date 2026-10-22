"""Ingest helpers shared by native connectors: everything lands in the local
vault (``documents``, the second brain's searchable store) and, for calendars,
in ``calendar_events`` (the briefing's agenda). Local SQLite only — no new
tables, no migration.

Paths are namespaced per connector (``<connector>/<kind>/<id>``) so a
re-sync upserts instead of duplicating, and ``prune()`` can drop items that
disappeared upstream without touching any other source.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from .. import db

MAX_CONTENT = 200_000


def to_vault(*, source: str, path: str, title: str | None, content: str, metadata: dict[str, Any] | None = None,
             mtime: int | None = None) -> bool:
    content = content[:MAX_CONTENT]
    meta = dict(metadata or {})
    meta.setdefault("connector", source)
    size = len(content.encode("utf-8"))
    # size alone misses same-length edits; fold a short content hash into mtime-less items
    if mtime is None:
        mtime = int(hashlib.sha256(content.encode("utf-8")).hexdigest()[:8], 16)
    return db.upsert_document(source=source, path=path, title=title, content=content, metadata=meta,
                              mtime=mtime, size=size, compare_content=True)


def prune(source: str, prefix: str, keep: set[str]) -> int:
    """Delete vault documents of ``source`` under ``prefix`` not in ``keep``."""
    with db.db() as conn:
        rows = conn.execute("SELECT id, path FROM documents WHERE source = ? AND path LIKE ?",
                            (source, prefix.replace("%", r"\%") + "%")).fetchall()
        # LIKE treats "_" as a wildcard: re-check the real prefix in Python
        gone = [r[0] for r in rows if r[1].startswith(prefix) and r[1] not in keep]
        for doc_id in gone:
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return len(gone)


def prune_window(source: str, prefix: str, keep: set[str], start: int, end: int) -> int:
    """prune() limited to documents whose ``metadata.starts_at`` falls in
    [start, end): events outside the synced window stay in the vault."""
    with db.db() as conn:
        rows = conn.execute("SELECT id, path FROM documents WHERE source = ? AND path LIKE ? "
                            "AND CAST(json_extract(metadata, '$.starts_at') AS INTEGER) >= ? "
                            "AND CAST(json_extract(metadata, '$.starts_at') AS INTEGER) < ?",
                            (source, prefix.replace("%", r"\%") + "%", int(start), int(end))).fetchall()
        gone = [r[0] for r in rows if r[1].startswith(prefix) and r[1] not in keep]
        for doc_id in gone:
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return len(gone)


def upsert_calendar_event(*, uid: str, title: str, starts_at: int, all_day: bool, source: str) -> None:
    with db.db() as conn:
        conn.execute(
            "INSERT INTO calendar_events (uid, title, starts_at, all_day, source, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET title=excluded.title, starts_at=excluded.starts_at, "
            "all_day=excluded.all_day, source=excluded.source",
            (uid, title, int(starts_at), 1 if all_day else 0, source, int(time.time())))


def prune_calendar(source: str, uid_prefix: str, start: int, end: int, keep: set[str]) -> int:
    with db.db() as conn:
        rows = conn.execute("SELECT id, uid FROM calendar_events WHERE source = ? AND uid LIKE ? "
                            "AND starts_at >= ? AND starts_at < ?",
                            (source, uid_prefix + "%", int(start), int(end))).fetchall()
        gone = [r[0] for r in rows if r[1].startswith(uid_prefix) and r[1] not in keep]
        for rid in gone:
            conn.execute("DELETE FROM calendar_events WHERE id = ?", (rid,))
    return len(gone)


def clamp_int(payload: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(payload.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))
