"""Migration 26 retires the leftover billing tables (entitlements, founders,
payment_events) and nothing else.

The migration DELETES local rows of those three tables, so these tests pin
exactly what it may and may not touch: a fresh database never creates them, an
old database loses them, every other table keeps its rows and the database
stays consistent, and running it again is a no-op.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from ops_dashboard import migrations

BILLING = ("entitlements", "founders", "payment_events")

# The schema these tables had before they were retired (kept here so the test can
# rebuild a database from before the change).
LEGACY_SCHEMA = """
CREATE TABLE entitlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE,
    plan TEXT NOT NULL DEFAULT 'free', source TEXT, stripe_customer_id TEXT,
    stripe_subscription_id TEXT, stripe_payment_intent_id TEXT, amount_cents INTEGER,
    currency TEXT DEFAULT 'USD', activated_at INTEGER, expires_at INTEGER,
    refunded_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE INDEX idx_entitlements_plan ON entitlements(plan);
CREATE INDEX idx_entitlements_stripe ON entitlements(stripe_customer_id);
CREATE TABLE founders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE,
    original_plan TEXT NOT NULL, original_cents INTEGER NOT NULL,
    badge_color TEXT DEFAULT 'gold', note TEXT, joined_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE INDEX idx_founders_user ON founders(user_id);
CREATE TABLE payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT NOT NULL,
    stripe_event_id TEXT, payload TEXT, created_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL);
CREATE INDEX idx_payment_events_user ON payment_events(user_id);
CREATE INDEX idx_payment_events_stripe ON payment_events(stripe_event_id);
"""


@pytest.fixture()
def dbmod(tmp_path, monkeypatch):
    dbp = tmp_path / "t.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    return db


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _counts(conn: sqlite3.Connection, tables) -> dict[str, int]:
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(tables)}


def test_fresh_database_never_creates_the_billing_tables(dbmod):
    dbmod.init_db()
    with dbmod.get_connection() as conn:
        assert not (set(BILLING) & _tables(conn))
        assert not [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND "
            "(name LIKE '%entitlements%' OR name LIKE '%founders%' OR name LIKE '%payment_events%')")]
        assert migrations.current_version(conn) >= 26
        assert 26 in {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_database_with_the_tables_and_rows_loses_only_those_three(dbmod):
    dbmod.init_db()
    now = 1_700_000_000
    with dbmod.get_connection() as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(LEGACY_SCHEMA)
        for i in (1, 2):
            conn.execute("INSERT INTO users (email, name, created_at) VALUES (?,?,?)",
                         (f"u{i}@example.com", f"U{i}", now))
        conn.execute("INSERT INTO entitlements (user_id, plan, source, stripe_customer_id, created_at, updated_at)"
                     " VALUES (1,'lifetime','stripe_lifetime','cus_x',?,?)", (now, now))
        conn.execute("INSERT INTO founders (user_id, original_plan, original_cents, joined_at)"
                     " VALUES (2,'lifetime',29900,?)", (now,))
        conn.execute("INSERT INTO payment_events (user_id, kind, stripe_event_id, payload, created_at)"
                     " VALUES (1,'lifetime_purchase','evt_x','{}',?)", (now,))
        # Put the database back to just before migration 26, as an upgrade would find it.
        conn.execute("DELETE FROM schema_migrations WHERE version = 26")
        conn.execute("PRAGMA user_version = 25")
        conn.commit()
        before_tables = _tables(conn) - set(BILLING)
        before = _counts(conn, before_tables)
        assert before["users"] == 2
        schema_before = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index') "
            "AND tbl_name NOT IN ('entitlements','founders','payment_events')"))

    dbmod.init_db()  # the app start-up path

    with dbmod.get_connection() as conn:
        assert not (set(BILLING) & _tables(conn))
        assert _tables(conn) == before_tables
        after = _counts(conn, before_tables)
        # sqlite_sequence is SQLite's own AUTOINCREMENT bookkeeping (the three tables'
        # counters leave with them); schema_migrations gains exactly the audit row for 26.
        for t in ("sqlite_sequence", "schema_migrations"):
            before.pop(t), after.pop(t)
        assert after == before                                  # no other table lost a row
        assert 26 in {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        schema_after = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index') "
            "AND tbl_name NOT IN ('entitlements','founders','payment_events')"))
        assert schema_after == schema_before                    # no other table was altered
        assert migrations.current_version(conn) >= 26
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_is_idempotent_and_safe_without_the_tables(dbmod):
    dbmod.init_db()
    with dbmod.get_connection() as conn:
        # Already applied: nothing to do, and calling it directly is harmless.
        assert migrations.run_migrations(conn) == []
        migrations._mig_26_drop_billing_tables(conn)
        migrations._mig_26_drop_billing_tables(conn)
        assert not (set(BILLING) & _tables(conn))


def test_gap_fill_applies_26_when_25_was_missing(dbmod):
    """A database already at 26 that never ran 25 (the two ship in either order)
    still gets 25 afterwards, and 26 does not disturb it."""
    dbmod.init_db()
    with dbmod.get_connection() as conn:
        conn.execute("PRAGMA user_version = 26")
        conn.execute("DELETE FROM schema_migrations WHERE version = 25")
        conn.commit()
        applied = [v for v, _ in migrations.run_migrations(conn)]
        assert applied in ([25], [])  # 25 exists only once the live-evals change is in
        assert migrations.current_version(conn) >= 26


def test_signup_and_me_work_without_entitlements(dbmod, monkeypatch):
    pytest.importorskip("bcrypt")
    import socket
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    dbmod.init_db()
    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/auth/signup", method="POST",
            data=json.dumps({"email": "new@example.com", "password": "correct horse 9"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read())
            cookie = r.headers.get("Set-Cookie", "")
        assert body["ok"] is True and body["user"]["email"] == "new@example.com"
        me = urllib.request.Request(f"http://127.0.0.1:{port}/api/auth/me",
                                    headers={"Cookie": cookie.split(";")[0]})
        with urllib.request.urlopen(me, timeout=20) as r:
            js = json.loads(r.read())
        assert js["user"]["email"] == "new@example.com"
        assert "entitlement" not in js
    finally:
        server.shutdown()
