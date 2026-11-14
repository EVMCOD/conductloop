"""The organisation as a tree: default reporting lines, the branch a CEO
delegates to, and a manager change that refuses loops.

The starter C-Suite and the project CEOs were created with nobody reporting to
anybody, so the agent panel had nothing to draw under the Holding CEO.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import org_defaults, seed_agents


@pytest.fixture()
def org(tmp_path, monkeypatch):
    dbp = tmp_path / "org.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    return db


def _team(conn, key):
    conn.execute("INSERT INTO teams (key, name, type, created_at, updated_at) VALUES (?, ?, 'mixed', 0, 0)",
                 (key, key.title()))
    return conn.execute("SELECT id FROM teams WHERE key = ?", (key,)).fetchone()[0]


def _member(conn, team_id, name, role, level="agent", *, type_="ai_agent", manager=None):
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO team_members (team_id, name, type, role, status, level, manager_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)", (team_id, name, type_, role, level, manager, now, now))
    return cur.lastrowid


def _managers(db):
    with db.get_connection() as c:
        return {r[0]: r[1] for r in c.execute("SELECT name, manager_id FROM team_members")}


def _ids(db):
    with db.get_connection() as c:
        return {r[0]: r[1] for r in c.execute("SELECT name, id FROM team_members")}


def test_default_lines_draw_the_organisation(org):
    with org.get_connection() as c:
        corp, train = _team(c, "corp"), _team(c, "train")
        holding = _member(c, corp, "Holding CEO", "ceo", "ceo")
        _member(c, corp, "Atlas (CTO)", "cto", "ceo")
        _member(c, corp, "Mira (CMO)", "cmo", "ceo")
        _member(c, corp, "Ada (VP Mobile)", "vp_mobile", "vp")
        _member(c, corp, "Priya (Head of Product)", "head_product", "director")
        _member(c, train, "CEO TrainCurve", "CEO")
        _member(c, train, "Workout AI", "agent")
        _member(c, train, "Enrique", "founder", "ceo", type_="human")
        _member(c, corp, "Growth Lead", "manager", "manager")
        _member(c, corp, "SEO Bot", "agent", manager=holding)        # a line the person drew
        c.execute("INSERT OR REPLACE INTO boardroom_settings (id, holding_ceo_id, updated_at) VALUES (1, ?, 0)",
                  (holding,))
        written = org_defaults.fill_missing_managers(c)
        c.commit()
    ids, mgr = _ids(org), _managers(org)
    assert mgr["Atlas (CTO)"] == mgr["Mira (CMO)"] == mgr["Priya (Head of Product)"] == ids["Holding CEO"]
    assert mgr["CEO TrainCurve"] == ids["Holding CEO"]
    assert mgr["Ada (VP Mobile)"] == ids["Atlas (CTO)"]
    assert mgr["Workout AI"] == ids["CEO TrainCurve"]
    assert mgr["Holding CEO"] is None and mgr["Enrique"] is None        # the top, and a person: untouched
    assert mgr["Growth Lead"] is None                                     # no rule fits: left for the person
    assert mgr["SEO Bot"] == ids["Holding CEO"]
    assert len(written) == 6
    with org.get_connection() as c:
        assert org_defaults.fill_missing_managers(c) == []                 # nothing left to fill


def test_a_line_the_person_drew_is_never_changed_and_no_loop_is_written(org):
    with org.get_connection() as c:
        corp = _team(c, "corp")
        cto = _member(c, corp, "Atlas (CTO)", "cto", "ceo")
        holding = _member(c, corp, "Holding CEO", "ceo", "ceo", manager=cto)   # drawn: Holding under the CTO
        c.execute("INSERT OR REPLACE INTO boardroom_settings (id, holding_ceo_id, updated_at) VALUES (1, ?, 0)",
                  (holding,))
        org_defaults.fill_missing_managers(c)
        c.commit()
    mgr, ids = _managers(org), _ids(org)
    assert mgr["Holding CEO"] == ids["Atlas (CTO)"]
    assert mgr["Atlas (CTO)"] is None          # under the Holding CEO would close a loop


def test_the_starter_pack_arrives_with_its_reporting_lines(org):
    with org.get_connection() as c:
        seed_agents.seed_starter_pack(c, chairman_user_id=0)
        c.commit()
    mgr, ids = _managers(org), _ids(org)
    cto = ids["Atlas (CTO)"]
    assert all(mgr[n] == cto for n in mgr if "(VP " in n)
    assert mgr["Atlas (CTO)"] is None          # no Holding CEO on a fresh install: the C-level are the tops


def test_the_migration_fills_lines_for_an_existing_library(org):
    from ops_dashboard import migrations
    with org.get_connection() as c:
        corp = _team(c, "corp")
        holding = _member(c, corp, "Holding CEO", "ceo", "ceo")
        _member(c, corp, "Penelope (CFO)", "cfo", "ceo")
        migrations._mig_42_default_reporting_lines(c)
        c.commit()
    assert _managers(org)["Penelope (CFO)"] == holding
    assert any(v == 42 for v, *_ in migrations.MIGRATIONS)


def test_a_ceo_delegates_to_its_whole_branch():
    """With the VPs under the CTO, a direct-reports-only team would have cut
    them off from the Holding CEO."""
    from ops_dashboard import team_chat
    agents = [
        {"key": "m:1", "id": 1, "name": "Holding", "manager_id": None, "kind": "member"},
        {"key": "m:2", "id": 2, "name": "CTO", "manager_id": 1, "kind": "member"},
        {"key": "m:3", "id": 3, "name": "VP", "manager_id": 2, "kind": "member"},
        {"key": "m:4", "id": 4, "name": "Elsewhere", "manager_id": None, "kind": "member"},
    ]
    assert [a["name"] for a in team_chat.team_of(agents[0], agents)] == ["CTO", "VP"]
    assert [a["name"] for a in team_chat.team_of(agents[1], agents)] == ["VP"]
    # Nobody under it: everyone else, as before.
    assert [a["name"] for a in team_chat.team_of(agents[3], agents)] == ["Holding", "CTO", "VP"]


@pytest.fixture()
def server(org):
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
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _put(base, mid, body):
    req = urllib.request.Request(f"{base}/api/members/{mid}", data=json.dumps(body).encode(),
                                 method="PUT", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_changing_a_manager_refuses_a_loop(org, server):
    with org.get_connection() as c:
        corp = _team(c, "corp")
        boss = _member(c, corp, "Boss", "ceo", "ceo")
        lead = _member(c, corp, "Lead", "manager", "manager", manager=boss)
        bot = _member(c, corp, "Bot", "agent", manager=lead)
        c.commit()
    assert _put(server, boss, {"manager_id": bot}) == 409          # Boss under its own grandchild
    assert _managers(org)["Boss"] is None
    assert _put(server, bot, {"manager_id": boss}) == 200
    assert _managers(org)["Bot"] == boss
    assert _put(server, bot, {"manager_id": None, "notes": "loose"}) == 200
    assert _managers(org)["Bot"] is None
    assert _put(server, bot, {"manager_id": 99999}) == 400
    assert _put(server, 99999, {"manager_id": boss}) == 404
