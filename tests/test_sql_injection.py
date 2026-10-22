"""Regression guard for the audited S608 sites: the dynamic-UPDATE builders
whitelist column names, so a malicious field key can neither inject SQL nor
touch an unintended column. Values always go through `?` params."""
from __future__ import annotations

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "inj.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "inj.db"  # type: ignore[attr-defined]
    _db.init_db()
    return _db


def test_update_team_ignores_non_whitelisted_keys(db):
    tid = db.create_team("dept-a", "T", type="dept")
    # A malicious key that is not in the allowed set must be dropped, not
    # spliced into the SQL. With no valid fields the update is a no-op.
    assert db.update_team(tid, **{"name = 'x'; DROP TABLE teams; --": "boom"}) is False
    # Table still there and usable.
    assert any(t["id"] == tid for t in db.list_teams())
    # A whitelisted field still updates normally + values are parameterized.
    assert db.update_team(tid, name="Renamed") is True
    assert db.get_team(tid)["name"] == "Renamed"


def test_update_user_todo_rejects_injected_column(db):
    tid = db.add_user_todo("task", project_key="p")
    # Non-whitelisted key ignored; a real field with an injection-y value is
    # stored verbatim (parameterized), not executed.
    db.update_user_todo(tid, **{"evil; DROP TABLE user_todos": 1})
    db.update_user_todo(tid, content="'; DROP TABLE user_todos; --")
    rows = db.list_user_todos()
    assert any(r["id"] == tid for r in rows)  # table intact
