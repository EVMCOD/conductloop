"""Team chat (/chat): roster, the CEO's plan, parallel agents, persistence, route."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import brain, team_chat


class FakeProvider(brain.Provider):
    """Plans with JSON, streams two chunks, fails for agents named in ``fail``."""
    name = "fake"
    calls: list = []
    fail: set = set()
    plan = ('Sure:\n```json\n{"note": "Atlas and Mira will look at it.", "assignments": ['
            '{"agent": "AGENT_CTO", "task": "Check the stack"}, '
            '{"agent": "nobody", "task": "dropped"}, '
            '{"agent": "Mira (CMO)", "task": "Check the message"}]}\n```')

    def ask(self, system, messages, **kwargs):
        FakeProvider.calls.append(("ask", system, messages))
        return FakeProvider.plan if "Reply with JSON only" in system else "plain answer"

    def stream(self, system, messages, **kwargs):
        FakeProvider.calls.append(("stream", system, messages))
        if any(f"You are {n}," in system for n in FakeProvider.fail):
            raise RuntimeError("upstream exploded")
        yield "Hello "
        yield "world"


@pytest.fixture()
def chat_db(tmp_path, monkeypatch):
    import ops_dashboard.db as _db
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "chat.db")
    _db.init_db()
    monkeypatch.setattr(team_chat.brain, "resolve_provider", lambda *a, **k: FakeProvider())
    monkeypatch.setattr(team_chat.brain, "_retrieve", lambda *a, **k: [])
    FakeProvider.calls = []
    FakeProvider.fail = set()
    return _db


def _member(conn, name, role, level="ceo", *, project="", caps="", notes="", manager=None):
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, level, is_ceo, "
        "project_slug, manager_id, created_at, updated_at) VALUES (1, ?, 'ai_agent', ?, 'active', ?, ?, ?, 1, ?, ?, ?, ?)",
        (name, role, caps, notes, level, project, manager, now, now))
    return cur.lastrowid


@pytest.fixture()
def team(chat_db):
    with chat_db.db() as conn:
        conn.execute("INSERT INTO teams (key, name, type, created_at, updated_at) VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        ceo = _member(conn, "Holding CEO", "ceo", caps="Portfolio view.")
        _member(conn, "Project CEO", "ceo", project="alpha")
        cto = _member(conn, "Atlas (CTO)", "cto", caps=json.dumps({"skills": "architecture; code-review", "avatar": "🛠️"}),
                      notes="Chief Technology Officer")
        _member(conn, "Atlas (CTO)", "cto")                     # duplicate copy from an old seed
        cmo = _member(conn, "Mira (CMO)", "cmo", caps=json.dumps({"skills": "marketing; seo"}), notes="Chief Marketing Officer")
    FakeProvider.plan = FakeProvider.plan.replace("AGENT_CTO", f"m:{cto}")
    yield {"ceo": f"m:{ceo}", "cto": f"m:{cto}", "cmo": f"m:{cmo}"}
    FakeProvider.plan = FakeProvider.plan.replace(f"m:{cto}", "AGENT_CTO")


def test_builtin_team_when_the_library_has_no_executives(chat_db):
    info = team_chat.roster()
    keys = [a["key"] for a in info["agents"]]
    assert keys[0] == "brain" and info["lead"] == "builtin:ceo" and info["builtin_team"] is True
    assert {"builtin:cto", "builtin:cmo"} <= set(keys)


def test_roster_dedupes_and_picks_the_holding_ceo(team):
    info = team_chat.roster()
    names = [a["name"] for a in info["agents"]]
    assert info["lead"] == team["ceo"] and names.count("Atlas (CTO)") == 1
    cto = next(a for a in info["agents"] if a["key"] == team["cto"])
    assert cto["avatar"] == "🛠️" and cto["title"] == "Chief Technology Officer" and "architecture" in cto["skills"]
    lead = next(a for a in info["agents"] if a["lead"])
    assert team_chat.team_of(lead, info["agents"])[0]["name"] == "Project CEO"


def test_parse_plan_keeps_only_real_members(team):
    agents = team_chat.roster()["agents"]
    members = [a for a in agents if a["kind"] == "member" and not a["lead"]]
    note, plan = team_chat.parse_plan(FakeProvider.plan, members)
    assert note == "Atlas and Mira will look at it."
    assert [p["agent"] for p in plan] == [team["cto"], team["cmo"]]       # unknown dropped, name resolved
    assert team_chat.parse_plan("no json here", members) is None
    many = json.dumps({"assignments": [{"agent": a["key"], "task": "t"} for a in members] * 3})
    assert len(team_chat.parse_plan(many, members)[1]) == min(len(members), team_chat.MAX_ASSIGNMENTS)


def test_heuristic_plan_matches_skills(team):
    members = [a for a in team_chat.roster()["agents"] if a["kind"] == "member" and not a["lead"]]
    _, plan = team_chat.heuristic_plan("our seo and marketing are weak", members)
    assert plan[0]["agent"] == team["cmo"]


def test_team_turn_streams_plan_agents_and_answer(team):
    turn = team_chat.ChatTurn(team["ceo"], "How do we launch?")
    events = list(turn.events())
    kinds = [e["type"] for e in events]
    assert kinds[:3] == ["start", "context", "plan"] and kinds[-1] == "done"
    plan = events[2]
    assert [a["agent"] for a in plan["assignments"]] == [team["cto"], team["cmo"]]
    assert kinds.count("agent_start") == 2 and kinds.count("agent_done") == 2
    assert kinds.index("synthesis") > max(i for i, k in enumerate(kinds) if k == "agent_done")
    assert "".join(e["content"] for e in events if e["type"] == "delta") == "Hello world"
    synthesis_call = FakeProvider.calls[-1]
    assert "Your team's answers" in synthesis_call[2][0]["content"] and "Atlas (CTO)" in synthesis_call[2][0]["content"]

    thread = team_chat.get_thread(events[0]["thread_id"])
    roles = [m["role"] for m in thread["messages"]]
    assert roles[0] == "user" and roles[1] == "plan" and roles[-1] == "assistant"
    assert sorted(r for r in roles if r.startswith("agent:")) == sorted([f"agent:{team['cto']}", f"agent:{team['cmo']}"])
    assert team_chat.list_threads(team["ceo"])[0]["id"] == thread["id"]


def test_a_failing_agent_does_not_stop_the_answer(team):
    FakeProvider.fail = {"Atlas (CTO)"}
    events = list(team_chat.ChatTurn(team["ceo"], "Status?").events())
    errors = [e for e in events if e["type"] == "agent_error"]
    # the message goes through brain._translate_provider_error, like every other provider failure
    assert [e["agent"] for e in errors] == [team["cto"]] and "fake" in errors[0]["message"]
    assert any(e["type"] == "synthesis" for e in events) and events[-1]["type"] == "done"


def test_direct_chat_uses_history_and_stays_with_its_agent(team):
    first = list(team_chat.ChatTurn(team["cto"], "Which database?").events())
    tid = first[0]["thread_id"]
    assert first[0]["mode"] == "direct" and not any(e["type"] == "plan" for e in first)
    list(team_chat.ChatTurn(team["cto"], "And backups?", thread_id=tid).events())
    sent = FakeProvider.calls[-1][2]
    assert [m["content"] for m in sent] == ["Which database?", "Hello world", "And backups?"]
    with pytest.raises(LookupError):
        team_chat.ChatTurn(team["cmo"], "hi", thread_id=tid)
    with pytest.raises(LookupError):
        team_chat.ChatTurn("m:999999", "hi")
    with pytest.raises(ValueError):
        team_chat.ChatTurn(team["cto"], "   ")
    assert team_chat.delete_thread(tid) and team_chat.get_thread(tid) is None


def test_team_mode_off_talks_to_the_ceo_alone(team):
    events = list(team_chat.ChatTurn(team["ceo"], "hi", team=False).events())
    assert events[0]["mode"] == "direct" and not any(e["type"] == "plan" for e in events)


def test_send_route_streams_sse(team):
    from ops_dashboard.server import DashboardHandler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/chat", timeout=10)
        assert "script-src 'self'" in page.headers["Content-Security-Policy"]
        agents = json.loads(urllib.request.urlopen(base + "/api/chat/agents", timeout=10).read())
        assert agents["lead"] == team["ceo"]
        req = urllib.request.Request(base + "/api/chat/send", method="POST",
                                     data=json.dumps({"agent": team["ceo"], "message": "Plan the week"}).encode(),
                                     headers={"Content-Type": "application/json"})
        body = urllib.request.urlopen(req, timeout=30).read().decode()
        frames = [json.loads(line[6:]) for line in body.split("\n\n") if line.startswith("data: ")]
        assert frames[0]["type"] == "start" and frames[-1]["type"] == "done"
        assert any(f["type"] == "agent_delta" for f in frames)
        bad = urllib.request.Request(base + "/api/chat/send", method="POST",
                                     data=json.dumps({"agent": "m:424242", "message": "x"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(bad, timeout=10)
        assert err.value.code == 404
    finally:
        srv.shutdown()


def test_small_talk_gets_no_agents(team):
    members = [a for a in team_chat.roster()["agents"] if a["kind"] == "member" and not a["lead"]]
    assert team_chat.heuristic_plan("hye", members) == ("", [])
    saved_plan = FakeProvider.plan
    FakeProvider.plan = "hi there, no json"            # the model gives no usable plan
    try:
        events = list(team_chat.ChatTurn(team["ceo"], "hola").events())
    finally:
        FakeProvider.plan = saved_plan
    plan = next(e for e in events if e["type"] == "plan")
    assert plan["assignments"] == [] and not any(e["type"] == "agent_start" for e in events)
    assert "".join(e["content"] for e in events if e["type"] == "delta") == "Hello world"


class RetiredModel(FakeProvider):
    def ask(self, system, messages, **kwargs):
        raise RuntimeError("Error code: 404 - {'type': 'error', 'error': {'type': 'not_found_error', "
                           "'message': 'The model \"MiniMax-M2.7\" is no longer available. (2069)', 'http_code': '404'}}")


def test_a_provider_that_cannot_answer_stops_the_turn_once(team, monkeypatch):
    monkeypatch.setattr(team_chat.brain, "resolve_provider", lambda *a, **k: RetiredModel())
    events = list(team_chat.ChatTurn(team["ceo"], "How do we launch?").events())
    assert [e["type"] for e in events] == ["start", "context", "error", "done"]
    assert events[2]["code"] == "model_unavailable" and "MiniMax-M2.7" in events[2]["message"]
    assert not any(kind == "stream" for kind, *_ in FakeProvider.calls)   # no agent, no synthesis


def test_retired_model_error_is_translated():
    raw = ("Error code: 404 - {'type': 'error', 'error': {'type': 'not_found_error', 'message': "
           "'The model \"MiniMax-M2.7\" is no longer available. (2069)', 'http_code': '404'}}")
    err = brain._classify_provider_error(RuntimeError(raw), "minimax")
    assert err.code == "model_unavailable" and "«MiniMax-M2.7»" in err.message and "{" not in err.message
    assert brain._classify_provider_error(RuntimeError("Error code: 402 - insufficient_balance"), "minimax").code \
        == "insufficient_credit"
    assert brain.DEFAULT_MODELS["minimax"] == "MiniMax-M3"
