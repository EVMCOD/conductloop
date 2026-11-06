"""Choosing the Holding CEO (Boardroom, 2026-11-06).

``boardroom_settings.holding_ceo_id`` is set from the Boardroom page through
``/api/boardroom/holding-ceo``: only an existing AI agent, or nothing (the
Boardroom's own Holding CEO reply answers). Migration 34 lets the singleton
row exist on a local install with no user accounts.
"""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from ops_dashboard import boardroom, migrations
from ops_dashboard import db as dbmod
from ops_dashboard.routing import router

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "oc.db")
    dbmod.init_db()
    team = dbmod.create_team("hceo-team", "Holding CEO test team")
    agent = dbmod.add_member(team, "Atlas", type="ai_agent", role="ceo")
    other = dbmod.add_member(team, "Scout", type="ai_agent", role="research")
    human = dbmod.add_member(team, "Ana", type="human", role="ops")
    return {"agent": agent, "other": other, "human": human}


class _H:
    def __init__(self, body=None, *, local=True, user=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.local, self.user = local, user
        self.code, self.out = None, {}

    def _is_local(self):
        return self.local

    def _current_user(self):
        return self.user

    def _send(self, code, raw, ctype):
        self.code, self.out = code, json.loads(raw)


def _call(method, body=None, **kw) -> _H:
    from ops_dashboard import routes_meta  # noqa: F401  (registers the routes)

    h = _H(body, **kw)
    assert router.dispatch(h, method, "/api/boardroom/holding-ceo") is True
    return h


def test_nothing_chosen_at_first(env):
    h = _call("GET")
    assert h.code == 200 and h.out["holding_ceo"] is None
    ids = [c["id"] for c in h.out["candidates"]]
    assert env["agent"] in ids and env["other"] in ids and env["human"] not in ids
    assert ids[0] == env["agent"]                         # CEOs first


def test_choose_an_agent_then_clear_it(env):
    h = _call("POST", {"member_id": env["agent"]})
    assert h.code == 200 and h.out["holding_ceo"] == {"id": env["agent"], "name": "Atlas", "role": "ceo"}
    assert boardroom.holding_ceo()["id"] == env["agent"]
    with dbmod.db() as conn:
        row = conn.execute("SELECT * FROM boardroom_settings WHERE id = 1").fetchone()
    assert row["holding_ceo_id"] == env["agent"] and row["chairman_user_id"] is None
    h = _call("POST", {"member_id": str(env["other"])})   # a numeric string is fine
    assert h.out["holding_ceo"]["id"] == env["other"]
    h = _call("POST", {"member_id": None})
    assert h.code == 200 and h.out["holding_ceo"] is None
    assert boardroom.holding_ceo() is None


@pytest.mark.parametrize("bad", ["human", 999999, "abc", True, 1.5, [1], {"id": 1}])
def test_only_an_existing_ai_agent(env, bad):
    _call("POST", {"member_id": env["agent"]})
    value = env["human"] if bad == "human" else bad
    h = _call("POST", {"member_id": value})
    assert h.code == 400 and h.out["error"] == "not_an_ai_agent"
    assert boardroom.holding_ceo()["id"] == env["agent"]  # unchanged


def test_a_body_without_member_id_is_refused(env):
    assert _call("POST", {}).code == 400
    assert _call("POST", ["x"]).code == 400


def test_the_chairman_only(env):
    assert _call("GET", local=False).code == 403
    assert _call("POST", {"member_id": env["agent"]}, local=False,
                 user={"id": 5, "is_admin": 0, "is_chairman": 0}).code == 403
    assert boardroom.holding_ceo() is None


def test_a_remote_chairman_is_logged_in_the_decisions(env):
    with dbmod.db() as conn:
        uid = conn.execute("INSERT INTO users (email, password_hash, name, is_admin, created_at, "
                           "is_chairman) VALUES ('c@example.com', 'x', 'C', 0, 0, 1)").lastrowid
    h = _call("POST", {"member_id": env["agent"]}, local=False,
              user={"id": uid, "is_admin": 0, "is_chairman": 1})
    assert h.code == 200
    with dbmod.db() as conn:
        row = conn.execute("SELECT * FROM boardroom_settings WHERE id = 1").fetchone()
        dec = conn.execute("SELECT kind, title, member_id FROM boardroom_decisions "
                           "WHERE kind = 'holding_ceo_changed'").fetchall()
    assert row["chairman_user_id"] == uid
    assert [tuple(d) for d in dec] == [("holding_ceo_changed", "Holding CEO: Atlas", env["agent"])]


def test_deleting_the_member_clears_it(env):
    boardroom.set_holding_ceo(env["agent"])
    with dbmod.db() as conn:
        conn.execute("DELETE FROM team_members WHERE id = ?", (env["agent"],))
    assert boardroom.holding_ceo() is None


def test_a_member_that_stops_being_an_agent_is_not_the_holding_ceo(env):
    boardroom.set_holding_ceo(env["agent"])
    with dbmod.db() as conn:
        conn.execute("UPDATE team_members SET type = 'human' WHERE id = ?", (env["agent"],))
    assert boardroom.holding_ceo() is None


def test_migration_34_makes_the_chairman_optional_and_keeps_the_row(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY);"
        "CREATE TABLE team_members (id INTEGER PRIMARY KEY);"
        "INSERT INTO users (id) VALUES (3); INSERT INTO team_members (id) VALUES (9);"
        "CREATE TABLE boardroom_settings (id INTEGER PRIMARY KEY CHECK (id = 1),"
        " chairman_user_id INTEGER NOT NULL, holding_ceo_id INTEGER, last_recap_at INTEGER,"
        " updated_at INTEGER NOT NULL,"
        " FOREIGN KEY (chairman_user_id) REFERENCES users(id) ON DELETE CASCADE,"
        " FOREIGN KEY (holding_ceo_id) REFERENCES team_members(id) ON DELETE SET NULL);"
        "INSERT INTO boardroom_settings VALUES (1, 3, 9, 100, 200);")
    fn = {v: f for v, _d, f in migrations.MIGRATIONS}[34]
    for _ in range(2):                                    # idempotent
        fn(conn)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(boardroom_settings)")}
    assert cols["chairman_user_id"][3] == 0               # nullable now
    assert tuple(conn.execute("SELECT * FROM boardroom_settings").fetchone()) == (1, 3, 9, 100, 200)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "boardroom_settings_new" not in names
    conn.commit()                                         # (the pragma is a no-op inside a transaction)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM team_members WHERE id = 9")  # the foreign keys came along
    assert conn.execute("SELECT holding_ceo_id FROM boardroom_settings").fetchone()[0] is None


def test_a_fresh_database_allows_no_chairman(env):
    with dbmod.db() as conn:
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(boardroom_settings)")}
    assert cols["chairman_user_id"][3] == 0


def test_the_boardroom_page_has_the_picker_without_inline_code():
    page = (WEB / "boardroom.html").read_text()
    assert 'id="hceo-select"' in page and '<script src="/boardroom-holding-ceo.js" defer>' in page
    section = page.split('id="hceo"')[1].split("</section>")[0]
    assert "style=" not in section and "onclick" not in section.lower()
    js = (WEB / "boardroom-holding-ceo.js").read_text()
    assert "/api/boardroom/holding-ceo" in js
    assert "innerHTML" not in js and "eval(" not in js
