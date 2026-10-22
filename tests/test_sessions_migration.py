"""Migration 8 (multi-agent sessions) — fresh DB and upgrade of an existing v7
DB built from the demo fixtures (synthetic data only, no real user data)."""
from __future__ import annotations

import sqlite3

import pytest

NEW_TABLES = {"session_repos", "agent_sessions", "session_tasks", "path_claims",
              "session_messages", "session_events", "model_prices"}


def _objects(conn):
    return {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def _counts(conn, tables):
    out = {}
    for t in tables:
        try:
            out[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.OperationalError:
            # A pre-existing FTS table in main's schema can't be scanned
            # (unrelated to this PR); its SQL is still compared byte-for-byte.
            out[t] = None
    return out


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(path))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


def test_fresh_db_gets_v8_and_all_tables(fresh_db):
    conn = sqlite3.connect(fresh_db)
    # >= 8: later migrations (e.g. learning = 9, #34) may land in either order.
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 8
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert NEW_TABLES <= names
    assert conn.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0] == 0  # no seed prices
    assert conn.execute("SELECT description FROM schema_migrations WHERE version = 8").fetchone()


def test_upgrade_existing_v7_db_is_additive(tmp_path, monkeypatch):
    from ops_dashboard import db, migrations
    from ops_dashboard.indexers import demo as demo_mod
    from ops_dashboard.indexers import live_urls

    # 1. Build a v7 DB the way an existing install has it, with demo fixtures.
    v7 = tmp_path / "v7.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(v7))
    monkeypatch.setenv("CONDUCTLOOP_DEMO", "1")
    monkeypatch.setattr(db, "DB_PATH", v7)
    full = list(migrations.MIGRATIONS)
    monkeypatch.setattr(migrations, "MIGRATIONS", [m for m in full if m[0] < 8])
    db.init_db()
    live_urls.ensure_schema()
    demo_mod.run()
    copy = tmp_path / "v7-copy.db"
    src, dst = sqlite3.connect(v7), sqlite3.connect(copy)
    src.backup(dst)  # WAL-safe copy (a plain file copy would miss the -wal pages)
    src.close()
    dst.close()

    before = sqlite3.connect(copy)
    assert before.execute("PRAGMA user_version").fetchone()[0] == 7
    objs_before = _objects(before)
    tables = [n for (t, n) in objs_before if t == "table"]
    counts_before = _counts(before, tables)
    assert sum(v or 0 for v in counts_before.values()) > 0  # the fixture really has data
    before.close()

    # 2. Upgrade the copy with the real migration list up to 8. Later
    # migrations (e.g. learning = 9, #34) add their own tables and have their
    # own tests; keeping them out makes the "only these new tables" check exact.
    monkeypatch.setattr(migrations, "MIGRATIONS", [m for m in full if m[0] <= 8])
    conn = sqlite3.connect(copy)
    conn.execute("PRAGMA foreign_keys = ON")
    applied = migrations.run_migrations(conn)
    conn.commit()
    # Robust to merge order with other migrations >= 9 (e.g. learning, #34).
    assert 8 in [v for v, _ in applied]
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 8

    # 3. Additive only: every pre-existing object is byte-identical, data intact.
    objs_after = _objects(conn)
    for key, sql in objs_before.items():
        assert objs_after.get(key) == sql, key
    new = {n for (t, n) in objs_after if t == "table"} - set(tables)
    assert new == NEW_TABLES
    assert _counts(conn, tables) == {**counts_before, "schema_migrations": counts_before["schema_migrations"] + 1}
    assert all(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0 for t in NEW_TABLES)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # 4. Idempotent: a second run is a no-op.
    assert migrations.run_migrations(conn) == []


def test_schema_checks_are_a_last_line_of_defence(fresh_db):
    conn = sqlite3.connect(fresh_db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO session_repos (repo_path, created_at, updated_at) VALUES ('/r', 0, 0)")
    base = ("INSERT INTO agent_sessions (name, role, provider, model, repo_path, base_ref, "
            "budget_cost_cents, max_iterations, key_ref, budget_currency, created_at, updated_at) "
            "VALUES ('n','r','openai','m','/r','main', ?, ?, ?, ?, 0, 0)")
    conn.execute(base, (100, 60, "byok.openai.default", "EUR"))  # valid
    conn.execute(base, (100, 500, None, "EUR"))  # no schema cap: the limit lives in code
    bad = [
        (None, 60, None, "EUR"),                       # budget mandatory
        (0, 60, None, "EUR"),                          # budget > 0
        (100, 0, None, "EUR"),                         # at least 1 iteration
        (100, 60, None, "USD"),                        # EUR only
        (100, 60, "sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "EUR"),  # a key value
        (100, 60, "byok.openai.default; DROP", "EUR"),
    ]
    for row in bad:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, row)
    with pytest.raises(sqlite3.IntegrityError):  # unmarked repo (FK)
        conn.execute(base.replace("'/r'", "'/other'"), (100, 60, None, "EUR"))
    with pytest.raises(sqlite3.IntegrityError):  # in-flight task without owner
        conn.execute("INSERT INTO session_tasks (title, prompt, status, created_at, updated_at) "
                     "VALUES ('t', 'p', 'claimed', 0, 0)")
    with pytest.raises(sqlite3.IntegrityError):  # unknown claim mode
        conn.execute("INSERT INTO path_claims (session_id, repo_path, pattern, mode, created_at) "
                     "VALUES (1, '/r', 'a', 'shared', 0)")
    with pytest.raises(sqlite3.IntegrityError):  # negative / non-EUR price
        conn.execute("INSERT INTO model_prices (provider, model, in_cents_per_mtok, "
                     "out_cents_per_mtok, updated_at) VALUES ('p', 'm', -1, 0, 0)")
