"""Discord bridge answers as the Boardroom's chosen Holding CEO (#116).

Same rule as Telegram (#115/#116): wake ``holding_ceo_id`` when it is an AI
agent; otherwise fall back to ``chat_with_holding_ceo``. No live Discord.
"""
from __future__ import annotations

import pytest

from ops_dashboard import db as dbmod
from ops_dashboard import discord_bridge as dc


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "d.db")
    dbmod.init_db()
    monkeypatch.setattr(dc, "WEBHOOK_URL", "")
    dc.LAST_DELIVERY.clear()
    with dbmod.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, "
            "notes, level, is_ceo, project_slug, created_at, updated_at) "
            "VALUES (1, 'Atlas', 'ai_agent', 'cfo', 'active', '{}', '', 'ceo', 1, "
            "'acme', 0, 0)")
        conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, "
            "notes, level, is_ceo, project_slug, created_at, updated_at) "
            "VALUES (1, 'Human Chair', 'human', 'chairman', 'active', '{}', '', "
            "'chairman', 0, NULL, 0, 0)")
        atlas = conn.execute("SELECT id FROM team_members WHERE name = 'Atlas'").fetchone()["id"]
        human = conn.execute("SELECT id FROM team_members WHERE name = 'Human Chair'").fetchone()["id"]
    yield {"atlas": atlas, "human": human}


def _set_holding_ceo(member_id: int | None) -> None:
    from ops_dashboard import boardroom
    boardroom.set_holding_ceo(member_id)


def test_fallback_when_none_chosen(env):
    sent: list[tuple] = []

    def send(content, *, username="ConductLoop Holding CEO"):
        sent.append((content, username))
        return {"ok": True}

    out = dc.handle_update(
        {"author": "enrique", "content": "status?", "channel_id": "1"},
        wake=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not wake")),
        holding_ceo_reply=lambda p: f"boardroom:{p}",
        send=send,
    )
    assert out["ok"] and out["how"] == "boardroom"
    assert sent == [("boardroom:status?", "ConductLoop Holding CEO")]


def test_wakes_chosen_ai_agent(env):
    _set_holding_ceo(env["atlas"])
    wakes: list[tuple] = []
    sent: list[tuple] = []

    def wake(aid, prompt):
        wakes.append((aid, prompt))
        return {"ok": True, "response": f"atlas-says:{prompt}"}

    def send(content, *, username="ConductLoop Holding CEO"):
        sent.append((content, username))
        return {"ok": True}

    out = dc.handle_update(
        {"author": "enrique", "content": "what next?", "channel_id": "9"},
        wake=wake,
        holding_ceo_reply=lambda p: (_ for _ in ()).throw(AssertionError("no fallback")),
        send=send,
    )
    assert out["how"] == f"agent:{env['atlas']}"
    assert wakes == [(env["atlas"], "what next?")]
    assert sent[0][0] == "atlas-says:what next?"
    assert "Atlas" in sent[0][1] and "Holding CEO" in sent[0][1]


def test_fallback_when_chosen_is_not_ai_agent(env):
    # Point the setting at a human (holding_ceo() filters them out).
    with dbmod.db() as conn:
        conn.execute(
            "INSERT INTO boardroom_settings (id, chairman_user_id, holding_ceo_id, updated_at) "
            "VALUES (1, NULL, ?, 1) "
            "ON CONFLICT(id) DO UPDATE SET holding_ceo_id = excluded.holding_ceo_id",
            (env["human"],))
    from ops_dashboard import boardroom
    assert boardroom.holding_ceo() is None

    sent: list = []
    out = dc.handle_update(
        {"author": "x", "content": "hi", "channel_id": "1"},
        wake=lambda *a, **k: {"ok": True, "response": "nope"},
        holding_ceo_reply=lambda p: "fallback-ok",
        send=lambda content, **kw: sent.append(content) or {"ok": True},
    )
    assert out["how"] == "boardroom"
    assert sent == ["fallback-ok"]


def test_wake_error_is_surfaced(env):
    _set_holding_ceo(env["atlas"])
    sent: list = []
    out = dc.handle_update(
        {"author": "x", "content": "go", "channel_id": "1"},
        wake=lambda *a, **k: {"ok": False, "error": "cooldown"},
        holding_ceo_reply=lambda p: "unused",
        send=lambda content, **kw: sent.append(content) or {"ok": True},
    )
    assert out["how"] == f"agent:{env['atlas']}:error"
    assert "cooldown" in sent[0]


def test_empty_content_skipped(env):
    assert dc.handle_update({"author": "x", "content": "  "})["skipped"] == "no content"


def test_answer_helper_direct(env):
    reply, how = dc.answer_as_holding_ceo(
        "q",
        chosen={"id": env["atlas"], "name": "Atlas"},
        wake=lambda aid, p: {"ok": True, "response": "woke"},
        holding_ceo_reply=lambda p: "fb",
    )
    assert (reply, how) == ("woke", f"agent:{env['atlas']}")
    reply, how = dc.answer_as_holding_ceo(
        "q", chosen=None,
        wake=lambda *a, **k: {"ok": True, "response": "no"},
        holding_ceo_reply=lambda p: "fb",
    )
    assert (reply, how) == ("fb", "boardroom")


def test_status_reports_holding_ceo(env):
    assert dc.status()["holding_ceo"] is None
    _set_holding_ceo(env["atlas"])
    assert dc.status()["holding_ceo"]["id"] == env["atlas"]


def test_help_mentions_chosen_agent(env):
    _set_holding_ceo(env["atlas"])
    sent: list = []
    dc.handle_update(
        {"author": "x", "content": "/help", "channel_id": "1"},
        send=lambda content, **kw: sent.append(content) or {"ok": True},
    )
    assert "Atlas" in sent[0]
