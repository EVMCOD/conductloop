"""`with db.get_connection() as conn` must close the connection.

sqlite3's own `with` only commits or rolls back. The app has 42 such blocks
(Boardroom, captures, onboarding...): each left the database and its WAL open
until the garbage collector freed them, which under load it did not. The
demo server held 1,099 descriptors after two passes through the UI and the
packaged app (limit 256) logged "Too many open files".
"""
import sqlite3

import pytest

from ops_dashboard import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ops.db")
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    return tmp_path / "ops.db"


def test_the_block_commits_and_closes(fresh_db):
    with db.get_connection() as conn:
        conn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    check = sqlite3.connect(fresh_db)
    assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    check.close()


def test_an_error_rolls_back_and_closes(fresh_db):
    with pytest.raises(RuntimeError):
        with db.get_connection() as conn:
            conn.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    check = sqlite3.connect(fresh_db)
    assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    check.close()


def test_a_kept_connection_stays_open_until_closed(fresh_db):
    conn = db.get_connection()          # callers that keep it close it themselves
    conn.execute("SELECT 1")
    conn.close()


def test_many_blocks_leave_no_connection_open(fresh_db):
    seen = []
    for _ in range(300):                # more than the packaged app's 256 descriptors
        with db.get_connection() as conn:
            conn.execute("SELECT COUNT(*) FROM t").fetchone()
            seen.append(conn)
    for conn in seen:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
