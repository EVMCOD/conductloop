"""Guards the versioned migration runner (migrations.py)."""
from __future__ import annotations

import sqlite3

from ops_dashboard import migrations


def _fresh_conn():
    return sqlite3.connect(":memory:")


def test_migrations_apply_once_and_bump_version():
    conn = _fresh_conn()
    assert migrations.current_version(conn) == 0
    applied = migrations.run_migrations(conn)
    # baseline + at least the audit-table migration
    assert (2, "schema_migrations audit table") in applied
    assert migrations.current_version(conn) >= 2
    # audit table exists and recorded the migration
    rows = conn.execute("SELECT version, description FROM schema_migrations").fetchall()
    assert (2, "schema_migrations audit table") in rows


def test_migrations_are_idempotent():
    conn = _fresh_conn()
    migrations.run_migrations(conn)
    v1 = migrations.current_version(conn)
    # second run applies nothing
    assert migrations.run_migrations(conn) == []
    assert migrations.current_version(conn) == v1


def test_init_db_runs_migrations(tmp_path, monkeypatch):
    """A real init_db() leaves the DB at the latest migration version."""
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "m.db"))
    from ops_dashboard import db
    db.DB_PATH = tmp_path / "m.db"  # type: ignore[attr-defined]
    db.init_db()
    with db.get_connection() as conn:
        assert migrations.current_version(conn) >= 2
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0] >= 1
