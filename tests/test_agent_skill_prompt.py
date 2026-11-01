"""L7: an agent's installed skills reach that agent's prompt.

The mechanism only matters if the skill body actually reaches the model that
answers as that agent. Two surfaces answer as a Boardroom member:

* ``team_chat.agent_system`` — the /chat team mode (each agent answers a task)
* ``boardroom.generate_ceo_reply`` — the CEO's own thread reply

Both must carry the agent's own skills, and only those: a skill installed for
the CTO must not appear in the CMO's prompt.
"""
from __future__ import annotations

import json
import time

import pytest

from ops_dashboard import boardroom, team_chat
from ops_dashboard.skills import packs


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "prompt.db")
    db.init_db()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    now = int(time.time())
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)"
        )
        for name, role in (("Atlas (CTO)", "cto"), ("Mira (CMO)", "cmo")):
            conn.execute(
                "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, "
                "level, is_ceo, created_at, updated_at) VALUES (1, ?, 'ai_agent', ?, 'active', ?, ?, "
                "'ceo', 1, ?, ?)",
                (name, role, json.dumps({"skills": "n/a", "description": f"{role} agent"}),
                 role.title(), now, now),
            )
        rows = conn.execute("SELECT id, role FROM team_members ORDER BY id").fetchall()
    ids = {r["role"]: r["id"] for r in rows}
    return {"vault": vault, "cto": ids["cto"], "cmo": ids["cmo"], "db": db}


def _install(vault, key: str, body: str, description: str) -> None:
    d = vault / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {key}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")


def _bind(db, agent_id: int, key: str) -> None:
    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_skills (agent_id, skill_key, enabled, created_at) VALUES (?, ?, 1, ?)",
            (agent_id, key, int(time.time())),
        )


def test_agent_prompt_carries_the_agent_own_skill(env):
    _install(env["vault"], "cto-code-review",
             "# CTO code review\n\nAlways check the data-loss path first.", "Review code for the CTO")
    _bind(env["db"], env["cto"], "cto-code-review")
    agent = {"key": f"m:{env['cto']}", "id": env["cto"], "name": "Atlas (CTO)",
             "role": "cto", "title": "CTO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(agent, "(no matches)", "")
    assert "always check the data-loss path first" in system.lower()


def test_agent_prompt_does_not_carry_another_agents_skill(env):
    _install(env["vault"], "cmo-launch",
             "# CMO launch\n\nThe launch gate is a converted trial.", "Plan a launch for the CMO")
    _bind(env["db"], env["cmo"], "cmo-launch")
    agent = {"key": f"m:{env['cto']}", "id": env["cto"], "name": "Atlas (CTO)",
             "role": "cto", "title": "CTO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(agent, "(no matches)", "")
    assert "converted trial" not in system.lower()


def test_agent_prompt_without_skills_is_unchanged(env):
    agent = {"key": f"m:{env['cto']}", "id": env["cto"], "name": "Atlas (CTO)",
             "role": "cto", "title": "CTO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(agent, "(no matches)", "")
    assert "skill" not in system.lower().split("rules:")[0]


def test_a_disabled_skill_is_not_offered(env):
    _install(env["vault"], "cto-code-review", "# CTO\n\nBody.", "Review code")
    _bind(env["db"], env["cto"], "cto-code-review")
    packs.set_skill_enabled(env["cto"], "cto-code-review", False)
    agent = {"key": f"m:{env['cto']}", "id": env["cto"], "name": "Atlas (CTO)",
             "role": "cto", "title": "CTO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(agent, "(no matches)", "")
    assert "cto-code-review" not in system


def test_boardroom_ceo_reply_carries_the_ceo_own_skill(env, monkeypatch):
    _install(env["vault"], "cto-arch", "# CTO\n\nWrite the ADR before the code.", "Architecture decisions")
    _bind(env["db"], env["cto"], "cto-arch")
    seen: dict = {}

    class FakeProvider:
        name = "fake"
        model = "fake-model"

        def ask(self, system, messages, **kwargs):
            seen["system"] = system
            return "reply"

    # boardroom imports brain lazily inside the function, so patch the module.
    from ops_dashboard import brain as brain_mod
    monkeypatch.setattr(brain_mod, "resolve_provider", lambda *a, **k: FakeProvider())
    boardroom.generate_ceo_reply(env["cto"])
    assert "write the adr before the code" in seen["system"].lower()


def test_an_skill_that_no_longer_validates_is_not_offered(env):
    """A user edits the installed file into something invalid: the agent must
    not be handed a broken body."""
    _install(env["vault"], "cto-broken", "# CTO\n\nBody.", "Review code")
    _bind(env["db"], env["cto"], "cto-broken")
    (env["vault"] / "cto-broken" / "SKILL.md").write_text("no frontmatter at all", encoding="utf-8")
    agent = {"key": f"m:{env['cto']}", "id": env["cto"], "name": "Atlas (CTO)",
             "role": "cto", "title": "CTO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(agent, "(no matches)", "")
    assert "cto-broken" not in system


def test_prompt_block_is_bounded(env):
    """A pack of large skills must not blow the prompt up."""
    big = "\n".join(f"line {i} of a very long skill" for i in range(4000))
    _install(env["vault"], "cto-huge", f"# CTO\n\n{big}", "A very long skill")
    _bind(env["db"], env["cto"], "cto-huge")
    agent = {"key": f"m:{env['cto']}", "id": env["cto"], "name": "Atlas (CTO)",
             "role": "cto", "title": "CTO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(agent, "(no matches)", "")
    assert len(system) < 40_000
