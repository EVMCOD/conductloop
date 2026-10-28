"""Backups of the SQLite database, consistent while the app is writing.

They go to <data dir>/backups/ (app_paths) as plain SQLite files that open on
their own. They are taken with SQLite's online backup API, so a backup made
while the app writes (WAL mode) is a consistent snapshot; a copy of the file
is not, and it misses whatever is still in ops.db-wal.

When:
    - automatically, at most once a day (scheduler, ``scheduled()``);
    - before a new version changes the schema (db.init_db, "pre-migrate");
    - by hand: Setup → Backups, or ``python -m ops_dashboard backup``.

Restore (Setup, or ``python -m ops_dashboard restore <name>``) backs up the
current database first ("pre-restore"), then writes the backup into it through
SQLite, so the running app sees the restored data without a restart.

Keeps the last 30 backups.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import app_paths as _app_paths

log = logging.getLogger("conductloop.backups")

MAX_KEEP = 30
# A day of the scheduler's hourly check, minus some slack for a Mac that slept.
SCHEDULED_EVERY_S = 20 * 3600
_NAME_RE = re.compile(r"^ops-\d{8}-\d{6}(?:-\d+)?-[a-z0-9-]{1,40}\.db$")
_lock = threading.RLock()


class BackupError(Exception):
    """A backup or a restore that could not be done; the message says why."""


def db_path() -> Path:
    # Read at call time: the packaged app, the tests and OPS_DATA_DIR all move it.
    from . import db
    return Path(db.DB_PATH)


def backup_dir() -> Path:
    return _app_paths.path("backups")


def _tag(reason: str) -> str:
    tag = re.sub(r"[^a-z0-9-]+", "-", (reason or "").lower()).strip("-")[:40]
    return tag or "manual"


def _free_name(out_dir: Path, tag: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"ops-{ts}-{tag}.db"
    n = 2
    while out.exists():
        out = out_dir / f"ops-{ts}-{n}-{tag}.db"
        n += 1
    return out


def _check(path: Path) -> int:
    """Is ``path`` an intact ConductLoop database? Returns its schema version.
    Opens it read-only: checking a backup never changes it."""
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise BackupError(f"{path.name} cannot be opened: {e}") from e
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"{path.name} is damaged: {result}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "documents" not in tables:
            raise BackupError(f"{path.name} is not a ConductLoop database")
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError as e:
        raise BackupError(f"{path.name} is not a readable database: {e}") from e
    finally:
        conn.close()


def backup_now(reason: str = "manual", *, keep: Path | None = None) -> Path:
    """Take a backup of the DB. Returns the path of the new backup.
    ``keep`` is never pruned (the backup a restore is about to read)."""
    src = db_path()
    if not src.exists():
        raise FileNotFoundError(f"DB not found: {src}")
    out_dir = backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        out = _free_name(out_dir, _tag(reason))
        partial = out.with_name(out.name + ".partial")
        try:
            source = sqlite3.connect(str(src), timeout=30)
            target = sqlite3.connect(str(partial))
            try:
                source.backup(target)
                # A backup is a single file: no -wal/-shm next to it.
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
                source.close()
            _check(partial)
            os.replace(partial, out)
        finally:
            partial.unlink(missing_ok=True)
        _prune(out_dir, keep=keep)
    log.info("backup saved: %s", out.name)
    return out


def _prune(out_dir: Path | None = None, *, keep: Path | None = None) -> None:
    out_dir = out_dir or backup_dir()
    if not out_dir.exists():
        return
    files = sorted(out_dir.glob("ops-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if keep is not None:
        files = [f for f in files if f.resolve() != keep.resolve()]
        files.insert(0, keep)
    for old in files[MAX_KEEP:]:
        try:
            old.unlink()
        except Exception:
            log.warning("could not delete old backup %s", old, exc_info=True)


def _reason(name: str) -> str:
    m = re.match(r"^ops-\d{8}-\d{6}(?:-\d+)?-(.+)\.db$", name)
    return m.group(1) if m else ""


def list_backups() -> list[dict]:
    out_dir = backup_dir()
    if not out_dir.exists():
        return []
    out = []
    for f in sorted(out_dir.glob("ops-*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        out.append({"path": str(f), "size_kb": round(st.st_size / 1024, 1), "mtime": int(st.st_mtime),
                    "name": f.name, "reason": _reason(f.name)})
    return out


def _resolve(name: str) -> Path:
    """A backup by its file name, and only one in the backups folder."""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise BackupError(f"not a backup name: {name!r}")
    out_dir = backup_dir().resolve()
    path = (out_dir / name).resolve()
    if path.parent != out_dir or not path.is_file():
        raise BackupError(f"no backup called {name}")
    return path


def restore(name: str) -> dict:
    """Put the backup ``name`` back as the live database.

    The current database is backed up first, so a restore can be undone by
    restoring that one. Refuses a damaged file, a file that is not a
    ConductLoop database, and one made by a newer version of the app."""
    from . import db, migrations

    path = _resolve(name)
    with _lock:
        version = _check(path)
        latest = max(v for v, _, _ in migrations.MIGRATIONS)
        if version > latest:
            raise BackupError(f"{name} was made by a newer version of ConductLoop "
                              f"(schema {version}, this one knows {latest}): update the app first")
        safety = backup_now("pre-restore", keep=path)
        source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(str(db_path()), timeout=30)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as e:
            raise BackupError(f"could not restore {name}: {e}. "
                              f"The database before the attempt is in {safety.name}") from e
        finally:
            target.close()
            source.close()
    # An older backup gets the schema of this version, the same way an upgrade
    # does (without a "pre-migrate" copy: the backup itself is that state).
    db.init_db(backup_before_upgrade=False)
    log.info("restored %s (the previous state is in %s)", name, safety.name)
    return {"restored": name, "safety_backup": safety.name, "schema_version_of_backup": version}


def scheduled() -> dict:
    """The automatic backup: at most one a day, whatever else was taken."""
    newest = list_backups()[:1]
    if newest and time.time() - newest[0]["mtime"] < SCHEDULED_EVERY_S:
        return {"skipped": True, "newest": newest[0]["name"]}
    if not db_path().exists():
        return {"skipped": True, "newest": None}
    return {"skipped": False, "backup": backup_now("daily").name}
