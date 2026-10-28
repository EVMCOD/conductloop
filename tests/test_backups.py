"""Backups of the database: consistent, restorable, and taken on their own.

The old backup was a file copy of ops.db: it missed whatever was still in
ops.db-wal, and it looked for the database next to the repository, which does
not exist inside the packaged app.
"""
from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from ops_dashboard import backups, db, migrations, routes_backups, scheduler

LATEST = max(v for v, _, _ in migrations.MIGRATIONS)


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A real ConductLoop database in a throwaway data dir."""
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ops.db")
    db.init_db()
    return tmp_path / "ops.db"


def _copy(src, dst) -> None:
    """A copy with the WAL folded in (a file copy would miss it, see below)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    s.backup(d)
    d.close()
    s.close()


def _titles(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT title FROM documents")}
    finally:
        conn.close()


def _add_doc(path, title: str) -> None:
    assert path == db.DB_PATH
    db.create_document(source="test", path=f"/t/{title}.md", title=title)


def test_a_backup_has_the_rows_still_in_the_wal(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ops.db")
    writer = sqlite3.connect(tmp_path / "ops.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE documents (title TEXT)")
    writer.executemany("INSERT INTO documents VALUES (?)", [(f"n{i}",) for i in range(50)])
    writer.commit()  # committed, but only in ops.db-wal: no checkpoint yet

    plain_copy = tmp_path / "plain-copy.db"
    shutil.copy2(tmp_path / "ops.db", plain_copy)
    with pytest.raises(sqlite3.OperationalError):
        _titles(plain_copy)  # what the old file copy saved: not even the table

    out = backups.backup_now("manual")
    writer.close()
    assert len(_titles(out)) == 50
    assert out.parent == tmp_path / "backups"
    assert not list(out.parent.glob("*.partial"))
    assert not (out.parent / (out.name + "-wal")).exists()
    conn = sqlite3.connect(out)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_names_are_safe_and_never_overwrite(live):
    first = backups.backup_now("Before ../../etc trip!")
    second = backups.backup_now("Before ../../etc trip!")
    assert first != second
    assert first.name.endswith("-before-etc-trip.db") and "/" not in first.name
    assert {b["reason"] for b in backups.list_backups()} == {"before-etc-trip"}


def test_only_the_newest_are_kept(live, monkeypatch):
    monkeypatch.setattr(backups, "MAX_KEEP", 3)
    for _ in range(5):
        backups.backup_now("manual")
    assert len(backups.list_backups()) == 3


def test_restore_brings_the_data_back_and_keeps_the_state_it_replaced(live):
    _add_doc(live, "kept")
    snap = backups.backup_now("manual")
    _add_doc(live, "added-later")

    result = backups.restore(snap.name)

    assert _titles(live) == {"kept"}
    safety = live.parent / "backups" / result["safety_backup"]
    assert "pre-restore" in safety.name
    assert _titles(safety) == {"kept", "added-later"}
    conn = sqlite3.connect(live)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_restore_upgrades_an_older_backup(live):
    old = backups.backup_dir() / "ops-20260101-000000-manual.db"
    _copy(live, old)
    conn = sqlite3.connect(old)
    conn.execute("PRAGMA user_version = 20")
    conn.commit()
    conn.close()
    backups.restore(old.name)
    conn = sqlite3.connect(live)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
    conn.close()
    assert sorted(b["reason"] for b in backups.list_backups()) == ["manual", "pre-restore"]


@pytest.mark.parametrize("name", ["../ops.db", "ops.db", "ops-20260101-000000-x.db/../../ops.db",
                                  "/etc/passwd", "", "ops-20260101-000000-missing.db"])
def test_restore_only_takes_a_backup_from_the_folder(live, name):
    with pytest.raises(backups.BackupError):
        backups.restore(name)


def test_restore_refuses_a_damaged_or_foreign_or_newer_file(live):
    folder = backups.backup_dir()
    folder.mkdir(parents=True, exist_ok=True)

    (folder / "ops-20260101-000000-junk.db").write_bytes(b"not a database" * 100)
    foreign = folder / "ops-20260101-000001-foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE something_else (x)")
    conn.commit()
    conn.close()
    newer = folder / "ops-20260101-000002-newer.db"
    _copy(live, newer)
    conn = sqlite3.connect(newer)
    conn.execute(f"PRAGMA user_version = {LATEST + 1}")
    conn.commit()
    conn.close()

    _add_doc(live, "untouched")
    for name, why in [("ops-20260101-000000-junk.db", "not a readable database"),
                      (foreign.name, "not a ConductLoop database"),
                      (newer.name, "newer version")]:
        with pytest.raises(backups.BackupError, match=why):
            backups.restore(name)
    assert _titles(live) == {"untouched"}
    assert not [b for b in backups.list_backups() if b["reason"] == "pre-restore"]


def test_an_upgrade_backs_up_the_database_first(live):
    conn = sqlite3.connect(live)
    conn.execute("PRAGMA user_version = 24")
    conn.commit()
    conn.close()
    db.init_db()
    assert [b["reason"] for b in backups.list_backups()] == ["pre-migrate-v24"]
    db.init_db()  # nothing left to migrate: no second backup
    assert len(backups.list_backups()) == 1


def test_a_fresh_install_takes_no_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ops.db")
    db.init_db()
    assert backups.list_backups() == []


def test_the_scheduled_backup_runs_at_most_once_a_day(live):
    first = backups.scheduled()
    assert first["skipped"] is False and "daily" in first["backup"]
    assert backups.scheduled()["skipped"] is True
    assert "backup" in [name for name, _, _ in scheduler._system_jobs()]


class _Handler:
    def __init__(self, body=None):
        self._body = body or {}

    def _read_json(self):
        return self._body

    def _send(self, code, body, ctype):
        self.code, self.data = code, json.loads(body)


def test_the_routes_take_list_and_restore(live):
    h = _Handler()
    routes_backups.backups_create(h)
    assert h.code == 200 and h.data["backup"] in [b["name"] for b in h.data["backups"]]
    assert "path" not in h.data["backups"][0]

    h = _Handler()
    routes_backups.backups_list(h)
    assert h.code == 200 and len(h.data["backups"]) == 1 and h.data["dir"].endswith("backups")

    name = h.data["backups"][0]["name"]
    h = _Handler({"name": name})
    routes_backups.backups_restore(h)
    assert h.code == 200 and h.data["restored"] == name

    h = _Handler({"name": "../ops.db"})
    routes_backups.backups_restore(h)
    assert h.code == 400 and h.data["error"] == "cannot_restore"
