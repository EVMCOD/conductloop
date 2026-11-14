"""documents_fts really works: MATCH over synthetic documents (no skips).

Covers a fresh DB, the trigger sync (insert/update/delete), tags derived from
metadata, malformed metadata, and the repair of a DB created by the old schema
(non-existent `tags` column, no triggers) without touching `documents`.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from ops_dashboard import db, db_fts

LEGACY_FTS = """
CREATE VIRTUAL TABLE documents_fts USING fts5(
    title, content, tags,
    content='documents', content_rowid='id', tokenize='porter unicode61'
);
"""
LEGACY_TRIGGER = """
CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, json_extract(new.metadata, '$.tags_text'));
END;
"""


@pytest.fixture()
def dbpath(tmp_path, monkeypatch):
    p = tmp_path / "fts.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(p))
    monkeypatch.setattr(db, "DB_PATH", p)
    return p


def _match(p, q: str) -> list[tuple]:
    c = sqlite3.connect(p)
    try:
        return c.execute(
            "SELECT rowid, title, tags FROM documents_fts WHERE documents_fts MATCH ? "
            "ORDER BY rank", (q,)).fetchall()
    finally:
        c.close()


def _doc(path: str, title: str, content: str, tags: list[str] | None = None, mtime: int = 1):
    meta = {"tags": tags} if tags is not None else {}
    return db.upsert_document(source="test", path=path, title=title, content=content,
                              metadata=meta, mtime=mtime, size=len(content))


def _ids(rows):
    return {r[0] for r in rows}


def _id_of(p, path: str) -> int:
    c = sqlite3.connect(p)
    try:
        return c.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()[0]
    finally:
        c.close()


def _integrity_ok(p) -> None:
    c = sqlite3.connect(p)
    try:
        c.execute("INSERT INTO documents_fts(documents_fts, rank) VALUES('integrity-check', 1)")
    finally:
        c.close()


def test_fresh_db_match_title_content_and_tags(dbpath):
    db.init_db()
    _doc("a.md", "Quarterly roadmap", "We migrate the billing service to Postgres.", ["planning", "infra"])
    _doc("b.md", "Holiday notes", "Running along the beaches of Cádiz.", ["personal"])
    _doc("c.md", "Untagged", "Nothing special here about postgres either.")
    a, b, c = (_id_of(dbpath, x) for x in ("a.md", "b.md", "c.md"))

    assert _ids(_match(dbpath, "postgres")) == {a, c}             # content
    assert _ids(_match(dbpath, "title:roadmap")) == {a}            # title column
    assert _ids(_match(dbpath, "tags:infra")) == {a}               # tags from metadata
    assert _ids(_match(dbpath, "tags:personal")) == {b}
    assert _ids(_match(dbpath, "run")) == {b}                      # porter stemming
    assert _ids(_match(dbpath, "cadiz")) == {b}                    # unicode61 diacritics
    assert _ids(_match(dbpath, "postgres AND tags:planning")) == {a}
    # reading column values used to fail with "no such column: T.tags"
    row = [r for r in _match(dbpath, "tags:infra")][0]
    assert row[1] == "Quarterly roadmap" and row[2] == "planning infra"
    c_ = sqlite3.connect(dbpath)
    assert c_.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0] == 3
    c_.close()
    _integrity_ok(dbpath)


def test_triggers_keep_index_in_sync_on_update_and_delete(dbpath):
    db.init_db()
    _doc("a.md", "Draft", "alpha content", ["one"], mtime=1)
    a = _id_of(dbpath, "a.md")
    assert _ids(_match(dbpath, "alpha")) == {a}

    _doc("a.md", "Draft", "omega content", ["two"], mtime=2)      # upsert → UPDATE
    assert _match(dbpath, "alpha") == []
    assert _match(dbpath, "tags:one") == []
    assert _ids(_match(dbpath, "omega")) == {a}
    assert _ids(_match(dbpath, "tags:two")) == {a}

    c = sqlite3.connect(dbpath)
    c.execute("DELETE FROM documents WHERE id = ?", (a,))
    c.commit()
    c.close()
    assert _match(dbpath, "omega") == []
    _integrity_ok(dbpath)


def test_malformed_metadata_does_not_break_index(dbpath):
    db.init_db()
    c = sqlite3.connect(dbpath)
    c.execute("INSERT INTO documents (source, path, title, content, metadata, indexed_at) "
              "VALUES ('test', 'x.md', 'Broken meta', 'zebra stripes', '{not json', 0)")
    c.commit()
    c.close()
    rows = _match(dbpath, "zebra")
    assert len(rows) == 1 and rows[0][2] is None
    _integrity_ok(dbpath)


def test_repairs_legacy_schema_without_losing_documents(dbpath):
    """A DB created by the old schema: broken FTS + no sync triggers."""
    db.init_db()
    c = sqlite3.connect(dbpath)
    c.executescript("DROP TRIGGER IF EXISTS documents_ai; DROP TRIGGER IF EXISTS documents_ad;"
                    "DROP TRIGGER IF EXISTS documents_au; DROP TABLE documents_fts;"
                    "DROP VIEW documents_fts_src;" + LEGACY_FTS + LEGACY_TRIGGER)
    rows = [("test", f"n{i}.md", f"Note {i}", f"legacy body {i} kiwi" if i % 2 else f"body {i}",
             json.dumps({"tags": ["old"], "tags_text": "old"}), i, 10, 0) for i in range(20)]
    c.executemany("INSERT INTO documents (source, path, title, content, metadata, mtime, size, "
                  "indexed_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    with pytest.raises(sqlite3.OperationalError, match="T.tags"):
        c.execute("SELECT COUNT(*) FROM documents_fts").fetchone()
    before = c.execute("SELECT * FROM documents ORDER BY id").fetchall()
    c.close()

    db.init_db()                                                   # repair

    c = sqlite3.connect(dbpath)
    assert c.execute("SELECT * FROM documents ORDER BY id").fetchall() == before
    assert c.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0] == 20
    c.close()
    assert len(_match(dbpath, "kiwi")) == 10                      # pre-existing rows indexed
    assert len(_match(dbpath, "tags:old")) == 20
    _doc("new.md", "Fresh", "kiwi again", ["new"])                 # new triggers work
    assert len(_match(dbpath, "kiwi")) == 11
    _integrity_ok(dbpath)


def test_ensure_is_idempotent(dbpath):
    db.init_db()
    with db.get_connection() as conn:
        assert db_fts.is_current(conn)
        assert db_fts.ensure_documents_fts(conn) is False
    db.init_db()
    _doc("a.md", "T", "persistent words")
    db.init_db()                                                   # no rebuild, still indexed
    assert len(_match(dbpath, "persistent")) == 1
