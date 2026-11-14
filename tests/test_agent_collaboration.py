"""L7b: how the C-Suite works together.

Three gaps this suite pins:

1. The LEAD's own installed skills must shape the plan it makes and the
   synthesis it writes. A CEO with a "portfolio-prioritization" playbook that
   ignores it when delegating is a badge with no effect.
2. A worker must know what its PEERS are working on, or two agents duplicate
   the same work or contradict each other.
3. In the Boardroom each CEO has its own thread, so the C-Suite cannot
   collaborate: the chairman relays by hand. A CEO must see the OTHER CEOs'
   recent answers, or they answer the same question three different ways.
"""
from __future__ import annotations

import time

import pytest

from ops_dashboard import boardroom, team_chat


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "collab.db")
    db.init_db()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    now = int(time.time())
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        ids = {}
        for name, role, project in (("Atlas", "cto", "app-a"), ("Vega", "cfo", "app-a"),
                                    ("Nova", "cmo", "app-b")):
            cur = conn.execute(
                "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, "
                "level, is_ceo, project_slug, created_at, updated_at) VALUES (1, ?, 'ai_agent', ?, "
                "'active', '{}', ?, 'ceo', 1, ?, ?, ?)",
                (name, role, role.upper(), project, now, now))
            ids[role] = cur.lastrowid
    return {"vault": vault, "ids": ids, "db": db}


def _install(vault, key: str, body: str) -> None:
    d = vault / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {key}\ndescription: Use when the situation applies\n---\n\n{body}\n",
        encoding="utf-8")


def _bind(db, agent_id: int, key: str) -> None:
    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_skills (agent_id, skill_key, enabled, created_at) "
            "VALUES (?, ?, 1, ?)", (agent_id, key, int(time.time())))


def _agent(agent_id: int, name: str, role: str) -> dict:
    return {"key": f"m:{agent_id}", "id": agent_id, "name": name, "role": role,
            "title": role.upper(), "skills": "", "description": "", "lead": False,
            "kind": "member", "project": ""}


# ---------- 1. the lead's own skills shape its plan and its synthesis ----------

def test_the_leads_own_skills_reach_the_plan_prompt(env):
    _install(env["vault"], "ceo-prioritization",
             "# Prioritization\n\nAlways rank by revenue per hour, never by fun.")
    _bind(env["db"], env["ids"]["cto"], "ceo-prioritization")
    lead = _agent(env["ids"]["cto"], "Atlas", "cto")
    plan = team_chat.plan_system(lead, [lead])
    assert "revenue per hour" in plan.lower()


def test_the_leads_own_skills_reach_the_synthesis_prompt(env):
    _install(env["vault"], "ceo-prioritization",
             "# Prioritization\n\nAlways rank by revenue per hour, never by fun.")
    _bind(env["db"], env["ids"]["cto"], "ceo-prioritization")
    lead = _agent(env["ids"]["cto"], "Atlas", "cto")
    synth = team_chat.synthesis_system(lead, "(no matches)", "")
    assert "revenue per hour" in synth.lower()


def test_a_lead_without_skills_gets_no_block(env):
    lead = _agent(env["ids"]["cto"], "Atlas", "cto")
    plan = team_chat.plan_system(lead, [lead])
    assert "YOUR INSTALLED SKILLS" not in plan


# ---------- 2. a worker knows what its peers are doing ----------

def test_a_worker_sees_the_tasks_of_its_peers(env):
    lead = _agent(env["ids"]["cto"], "Atlas", "cto")
    worker = _agent(env["ids"]["cfo"], "Vega", "cfo")
    peers = [{"name": "Nova", "title": "CMO", "task": "Draft the launch plan"}]
    system = team_chat.agent_system(worker, "(no matches)", "", lead=lead, peers=peers)
    assert "draft the launch plan" in system.lower()
    assert "nova" in system.lower()


def test_a_worker_without_peers_gets_no_peer_block(env):
    worker = _agent(env["ids"]["cfo"], "Vega", "cfo")
    system = team_chat.agent_system(worker, "(no matches)", "")
    assert "YOUR TEAMMATES" not in system


def test_a_worker_does_not_see_a_peers_skill_body(env):
    """Peers are visible by name and task; their playbooks stay private."""
    _install(env["vault"], "cfo-secret", "# CFO\n\nThe margin floor is 71 percent.")
    _bind(env["db"], env["ids"]["cfo"], "cfo-secret")
    lead = _agent(env["ids"]["cto"], "Atlas", "cto")
    worker = _agent(env["ids"]["cmo"], "Nova", "cmo")
    system = team_chat.agent_system(worker, "(no matches)", "", lead=lead,
                                    peers=[{"name": "Vega", "title": "CFO", "task": "Check the unit economics"}])
    assert "check the unit economics" in system.lower()
    assert "margin floor" not in system.lower()


# ---------- 3. a Boardroom CEO sees what the other CEOs answered ----------

def test_a_ceo_sees_the_other_ceos_recent_answers(env):
    ids = env["ids"]
    with env["db"].db() as conn:
        conn.execute(
            "INSERT INTO ceo_messages (member_id, user_id, role, content, created_at) "
            "VALUES (?, NULL, 'ceo', ?, ?)",
            (ids["cfo"], "The CFO says churn is 3 percent this month.", int(time.time())))
        conn.execute(
            "INSERT INTO ceo_messages (member_id, user_id, role, content, created_at) "
            "VALUES (?, NULL, 'chairman', ?, ?)",
            (ids["cto"], "What is the churn rate?", int(time.time())))

    seen: dict = {}

    class FakeProvider:
        name = "fake"
        model = "fake-model"

        def ask(self, system, messages, **kwargs):
            seen["prompt"] = messages[0]["content"]
            return "reply"


    from ops_dashboard import brain as brain_mod
    orig = brain_mod.resolve_provider
    brain_mod.resolve_provider = lambda *a, **k: FakeProvider()  # type: ignore[assignment]
    try:
        boardroom.generate_ceo_reply(ids["cto"])
    finally:
        brain_mod.resolve_provider = orig  # type: ignore[assignment]

    assert "the cfo says churn is 3 percent" in seen["prompt"].lower()


def test_a_ceo_does_not_see_the_chairmans_own_message_from_another_ceo(env):
    """The peer block carries PEER answers, not the chairman's words: the
    chairman's message arrives once, in this thread."""
    ids = env["ids"]
    with env["db"].db() as conn:
        conn.execute(
            "INSERT INTO ceo_messages (member_id, user_id, role, content, created_at) "
            "VALUES (?, NULL, 'chairman', ?, ?)",
            (ids["cfo"], "A private question for the CFO.", int(time.time())))
        conn.execute(
            "INSERT INTO ceo_messages (member_id, user_id, role, content, created_at) "
            "VALUES (?, NULL, 'chairman', ?, ?)",
            (ids["cto"], "What is the churn rate?", int(time.time())))

    seen: dict = {}

    class FakeProvider:
        name = "fake"
        model = "fake-model"

        def ask(self, system, messages, **kwargs):
            seen["prompt"] = messages[0]["content"]
            return "reply"

    from ops_dashboard import brain as brain_mod
    orig = brain_mod.resolve_provider
    brain_mod.resolve_provider = lambda *a, **k: FakeProvider()  # type: ignore[assignment]
    try:
        boardroom.generate_ceo_reply(ids["cto"])
    finally:
        brain_mod.resolve_provider = orig  # type: ignore[assignment]

    assert "private question for the cfo" not in seen["prompt"].lower()


def test_a_ceo_with_no_peers_gets_no_peer_block(env):
    seen: dict = {}

    class FakeProvider:
        name = "fake"
        model = "fake-model"

        def ask(self, system, messages, **kwargs):
            seen["prompt"] = messages[0]["content"]
            return "reply"

    from ops_dashboard import brain as brain_mod
    orig = brain_mod.resolve_provider
    brain_mod.resolve_provider = lambda *a, **k: FakeProvider()  # type: ignore[assignment]
    try:
        boardroom.generate_ceo_reply(env["ids"]["cto"])
    finally:
        brain_mod.resolve_provider = orig  # type: ignore[assignment]

    assert "OTHER CEOs" not in seen["prompt"]
