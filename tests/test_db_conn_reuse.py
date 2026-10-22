"""Guards the opt-in connection-reuse mode (OPS_DB_CONN_REUSE): commit,
re-entrancy (nested db() = one transaction), actual reuse, and rollback."""
from __future__ import annotations

import threading

import pytest

_INS = ("INSERT INTO costs (provider, amount_cents, period, billed_at, created_at) "
        "VALUES (?, ?, '2026-07', 0, 0)")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "reuse.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "reuse.db"  # type: ignore[attr-defined]
    _db.init_db()
    # Turn reuse ON for this test and hand out a clean thread-local.
    monkeypatch.setattr(_db, "_conn_reuse", True)
    monkeypatch.setattr(_db, "_local", threading.local())
    return _db


def test_reuse_commits_and_reuses_connection(db):
    with db.db() as c1:
        c1.execute(_INS, ("x", 1))
        first = c1
    # A later call reuses the same connection object and sees the committed row.
    with db.db() as c2:
        assert c2 is first
        n = c2.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
    assert n == 1


def test_nested_db_is_single_transaction(db):
    with db.db() as outer:
        outer.execute(_INS, ("a", 1))
        with db.db() as inner:
            assert inner is outer
            inner.execute(_INS, ("b", 2))
        # inner exit must NOT close/commit early — outer still usable
        outer.execute(_INS, ("c", 3))
    with db.db() as c:
        assert c.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 3


def test_exception_rolls_back(db):
    with pytest.raises(RuntimeError):
        with db.db() as c:
            c.execute(_INS, ("z", 9))
            raise RuntimeError("boom")
    with db.db() as c:
        assert c.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0


def test_reuse_is_thread_safe(db):
    """Under reuse, concurrent threads each get their own connection (thread-
    local) and their writes don't interfere — validates it's safe to enable
    with ThreadingHTTPServer."""
    import threading

    conns = {}
    errors = []

    def worker(i):
        try:
            with db.db() as conn:
                conns[i] = id(conn)
                conn.execute(_INS, (f"t{i}", i))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # each thread saw a distinct connection object
    assert len(set(conns.values())) == len(conns) == 8
    # all 8 writes committed
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 8
