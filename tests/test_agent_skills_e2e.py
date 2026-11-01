"""L7c: the end-to-end path, from the shipped catalog to a CEO's prompt.

The unit tests cover each step. This one covers the seam: a real pack from
the shipped catalog, installed for a real seeded agent, reaching that
agent's prompt — and NOT reaching a different agent's.

It is the test that would catch "the install succeeded, the UI shows the
skill, and the model never sees it".
"""
from __future__ import annotations

import pytest

from ops_dashboard import boardroom, db, seed_agents, team_chat
from ops_dashboard.skills import packs


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "e2e.db")
    db.init_db()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    return {"vault": vault, "db": db}


def _seeded_agents() -> dict[str, int]:
    """The 12 C-Suite roles, seeded for real, keyed by role."""
    with db.db() as conn:
        res = seed_agents.seed_starter_pack(conn, chairman_user_id=1)
        assert res["ran"]
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, role FROM team_members WHERE type = 'ai_agent' AND is_ceo = 1").fetchall()
    return {r["role"]: r["id"] for r in rows}


def test_the_catalog_ships_one_pack_per_c_suite_role(env):
    found = {p["id"] for p in packs.discover_packs()}
    seeded = set(_seeded_agents())
    assert seeded <= found, sorted(seeded - found)


def test_installing_a_shipped_pack_for_a_seeded_ceo_lands_in_its_prompt(env):
    ids = _seeded_agents()
    cto_id = ids["cto"]
    res = packs.install_pack("cto", agent_id=cto_id)
    # The pack may already be there (the seeded agent got it automatically),
    # in which case the copy is kept rather than installed. Either way the
    # skills have to be bound.
    assert res["installed"] or res["kept"], res

    # The skill really is in the vault, in its own folder, with a SKILL.md.
    keys = [s["key"] for s in packs.bound_skills(cto_id)]
    assert keys, "the install bound nothing"
    for key in keys:
        assert (env["vault"] / key / "SKILL.md").is_file()

    # ...and it really is in that CEO's prompt.
    agent = {"key": f"m:{cto_id}", "id": cto_id, "name": "CTO", "role": "cto",
             "title": "CTO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(agent, "(no matches)", "")
    assert "YOUR INSTALLED SKILLS" in system
    assert key in system


def test_the_ceo_pack_does_not_leak_into_another_seeded_ceo(env):
    """The CTO's playbook must not reach the CFO.

    The CFO now has its OWN pack (the seeded agents get their role's pack
    automatically), so the check is not "the CFO has no skills" but "the CFO
    does not have the CTO's skills".
    """
    ids = _seeded_agents()
    cto_id, cfo_id = ids["cto"], ids["cfo"]
    packs.install_pack("cto", agent_id=cto_id)

    cto_keys = {s["key"] for s in packs.bound_skills(cto_id)}
    cfo_keys = {s["key"] for s in packs.bound_skills(cfo_id)}
    assert cto_keys and cfo_keys
    assert not (cto_keys & cfo_keys), "the two agents share a skill"

    cfo_agent = {"key": f"m:{cfo_id}", "id": cfo_id, "name": "CFO", "role": "cfo",
                 "title": "CFO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(cfo_agent, "(no matches)", "", task="review this code")
    for key in cto_keys:
        assert key not in system, f"the CTO's skill {key} reached the CFO"


def test_two_agents_can_hold_different_packs_at_the_same_time(env):
    ids = _seeded_agents()
    cto_id, cfo_id = ids["cto"], ids["cfo"]
    packs.install_pack("cto", agent_id=cto_id)
    packs.install_pack("cfo", agent_id=cfo_id)

    cto_keys = {s["key"] for s in packs.bound_skills(cto_id)}
    cfo_keys = {s["key"] for s in packs.bound_skills(cfo_id)}
    assert cto_keys and cfo_keys
    assert not (cto_keys & cfo_keys), "the two agents share a skill"


def test_the_boardroom_ceo_reply_carries_its_installed_pack(env):
    ids = _seeded_agents()
    cto_id = ids["cto"]
    packs.install_pack("cto", agent_id=cto_id)
    key = packs.bound_skills(cto_id)[0]["key"]
    # A distinctive phrase from the shipped skill, to prove the body travels.
    from ops_dashboard.skills import parse
    parsed = parse.parse_skill_file(env["vault"] / key / "SKILL.md")
    body_lines = [ln.strip() for ln in parsed.body.splitlines() if ln.strip()]
    assert body_lines, f"the shipped skill {key} has an empty body"
    marker = body_lines[0].lstrip("# ").strip()
    assert len(marker) >= 12, marker

    seen: dict = {}

    class FakeProvider:
        name = "fake"
        model = "fake-model"

        def ask(self, system, messages, **kwargs):
            seen["system"] = system
            return "reply"

    from ops_dashboard import brain as brain_mod
    orig = brain_mod.resolve_provider
    brain_mod.resolve_provider = lambda *a, **k: FakeProvider()  # type: ignore[assignment]
    try:
        boardroom.generate_ceo_reply(cto_id)
    finally:
        brain_mod.resolve_provider = orig  # type: ignore[assignment]

    assert marker.lower() in seen["system"].lower()
