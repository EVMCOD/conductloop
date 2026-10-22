"""Full-text index over ``documents`` (FTS5), kept correct and in sync.

History: the original schema declared ``documents_fts(title, content, tags)``
as an external-content table over ``documents``, but ``documents`` has no
``tags`` column (tags live in ``metadata.tags_text``), so any read of the index
failed with "no such column: T.tags". Its sync triggers were also never
created: ``init_db`` splits SCHEMA on ';', which cuts ``BEGIN … END`` trigger
bodies apart, and the failures were swallowed — so the index stayed empty.

Fix: the index's content source is a view exposing ``tags`` derived from
``metadata`` (exactly what the triggers always meant to index), the triggers
are created as whole statements here, and an existing index is dropped and
rebuilt from ``documents``. ``documents`` itself is never modified, so no data
can be lost; the index is derived data. Idempotent: a no-op once in place.
"""
from __future__ import annotations

import logging
import re
import sqlite3

log = logging.getLogger("conductloop.db_fts")

# Same expression everywhere (view + triggers) so 'delete' entries always match
# what was indexed; json_valid guards rows with malformed metadata.
_TAGS = "CASE WHEN json_valid({m}) THEN json_extract({m}, '$.tags_text') END"

DESIRED: dict[tuple[str, str], str] = {
    ("view", "documents_fts_src"): (
        "CREATE VIEW documents_fts_src AS SELECT id, title, content, "
        + _TAGS.format(m="metadata") + " AS tags FROM documents"),
    ("table", "documents_fts"): (
        "CREATE VIRTUAL TABLE documents_fts USING fts5(title, content, tags, "
        "content='documents_fts_src', content_rowid='id', tokenize='porter unicode61')"),
    ("trigger", "documents_ai"): (
        "CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN "
        "INSERT INTO documents_fts(rowid, title, content, tags) VALUES "
        "(new.id, new.title, new.content, " + _TAGS.format(m="new.metadata") + "); END"),
    ("trigger", "documents_ad"): (
        "CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN "
        "INSERT INTO documents_fts(documents_fts, rowid, title, content, tags) VALUES "
        "('delete', old.id, old.title, old.content, " + _TAGS.format(m="old.metadata") + "); END"),
    ("trigger", "documents_au"): (
        "CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN "
        "INSERT INTO documents_fts(documents_fts, rowid, title, content, tags) VALUES "
        "('delete', old.id, old.title, old.content, " + _TAGS.format(m="old.metadata") + "); "
        "INSERT INTO documents_fts(rowid, title, content, tags) VALUES "
        "(new.id, new.title, new.content, " + _TAGS.format(m="new.metadata") + "); END"),
}


def _norm(sql: str | None) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip().rstrip(";")).lower()


def is_current(conn: sqlite3.Connection) -> bool:
    have = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name IN "
        "('documents_fts_src','documents_fts','documents_ai','documents_ad','documents_au')")}
    return all(_norm(have.get(k)) == _norm(sql) for k, sql in DESIRED.items())


def ensure_documents_fts(conn: sqlite3.Connection) -> bool:
    """Create or repair the FTS index. Returns True if it (re)built the index."""
    if is_current(conn):
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts5_probe")
    except sqlite3.Error:
        log.warning("SQLite has no FTS5; documents_fts not built (search uses LIKE)")
        return False
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name in ("documents_ai", "documents_ad", "documents_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute("DROP TABLE IF EXISTS documents_fts")   # derived index only
        conn.execute("DROP VIEW IF EXISTS documents_fts_src")
        for sql in DESIRED.values():
            conn.execute(sql)
        conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        # Never block startup: v1 search uses LIKE, the index is optional.
        log.exception("documents_fts rebuild failed; left unchanged")
        return False
    log.info("documents_fts (re)built from documents")
    return True
