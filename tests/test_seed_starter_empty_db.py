"""seed-starter on a brand-new database with no teams (used to 500 on
teams.key NOT NULL). Exercised directly and through the real local server."""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import seed_agents


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    dbp = tmp_path / "fresh.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    with db.get_connection() as c:
        assert c.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0
    return db


def _counts(db):
    with db.get_connection() as c:
        teams = [dict(r) for r in c.execute("SELECT key, name, type FROM teams")]
        ceos = c.execute("SELECT COUNT(*) FROM team_members WHERE type='ai_agent' "
                         "AND level IN ('ceo','vp','director')").fetchone()[0]
    return teams, ceos


def test_seed_on_empty_db_creates_a_holding_team(fresh_db):
    with fresh_db.get_connection() as c:
        dry = seed_agents.seed_starter_pack(c, chairman_user_id=0, dry_run=True)
        c.commit()
    assert len(dry["created"]) == len(seed_agents.STARTER_PACK)
    assert _counts(fresh_db) == ([], 0)               # dry run writes nothing, not even a team
    with fresh_db.get_connection() as c:
        out = seed_agents.seed_starter_pack(c, chairman_user_id=0)
        c.commit()
    assert len(out["created"]) == len(seed_agents.STARTER_PACK)
    teams, ceos = _counts(fresh_db)
    assert teams == [{"key": "holding", "name": "Holding", "type": "ai_squad"}]
    assert ceos == len(seed_agents.STARTER_PACK)
    with fresh_db.get_connection() as c:               # idempotent
        again = seed_agents.seed_starter_pack(c, chairman_user_id=0)
        c.commit()
    assert again["created"] == [] and len(again["skipped_existing"]) == len(seed_agents.STARTER_PACK)
    assert _counts(fresh_db) == (teams, ceos)


def test_seed_reuses_an_existing_team(fresh_db):
    fresh_db.seed_default_teams()
    with fresh_db.get_connection() as c:
        seed_agents.seed_starter_pack(c, chairman_user_id=0)
        c.commit()
    teams, _ = _counts(fresh_db)
    assert [t["key"] for t in teams] == ["ai-squad"]


def test_default_teams_seed_is_idempotent(fresh_db):
    # GET /api/teams calls it on every load of the Teams view
    first = fresh_db.seed_default_teams()
    assert first["members"] == 5
    for _ in range(3):
        assert fresh_db.seed_default_teams() == {"teams": 0, "members": 0}
    with fresh_db.get_connection() as c:
        n, distinct = c.execute("SELECT COUNT(*), COUNT(DISTINCT name) FROM team_members").fetchone()
    assert n == distinct == 5


def test_seed_starter_endpoint_on_empty_db(fresh_db):
    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/boardroom/seed-starter",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            assert r.status == 200
            body = json.loads(r.read())
    finally:
        srv.shutdown()
    assert len(body["created"]) == len(seed_agents.STARTER_PACK)
    assert _counts(fresh_db)[1] == len(seed_agents.STARTER_PACK)
