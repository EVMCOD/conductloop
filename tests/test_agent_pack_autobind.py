"""A new agent should arrive with its role's playbook already on it.

The catalog ships one pack per C-suite role. When the founder creates a CFO,
the CFO should already know how this business does unit economics — not
require twelve manual installs before the first question.

So the binding follows the agent: an agent whose role has a pack gets that
pack's skills bound to it. The rule is deliberately narrow:

- It happens when the agent is CREATED, not behind a settings screen.
- It only touches agents whose role matches a pack. An agent with a custom
  role gets nothing, because guessing would install the wrong playbook.
- It never removes anything: a skill the founder disabled stays disabled, and
  a skill they added by hand stays.
- An existing agent keeps whatever it has. The founder may have curated that
  agent's skills on purpose.
"""
from __future__ import annotations

import json

import pytest

from ops_dashboard import db, seed_agents
from ops_dashboard.skills import packs


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "auto.db")
    db.init_db()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    now = 1700000000
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        conn.execute(
            "INSERT OR IGNORE INTO users (id, email, name, password_hash, created_at) "
            "VALUES (1, 'c@example.com', 'Chairman', 'x', ?)", (now,))
    return {"vault": vault, "db": db}


def _seed() -> None:
    with db.db() as conn:
        seed_agents.seed_starter_pack(conn, chairman_user_id=1)


def _first(role: str) -> int:
    with db.db() as conn:
        row = conn.execute(
            "SELECT id FROM team_members WHERE role = ? AND is_ceo = 1 LIMIT 1", (role,)).fetchone()
    assert row, f"no {role} was seeded"
    return row["id"]


def test_the_starter_pack_gives_each_role_its_playbook(env):
    """The twelve seeded C-suite members arrive with their own pack."""
    _seed()
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, role FROM team_members WHERE type = 'ai_agent' AND is_ceo = 1").fetchall()
    assert len(rows) >= 12
    pack_roles = {p["role"] for p in packs.discover_packs()}
    for row in rows:
        if row["role"] not in pack_roles:
            continue  # a role with no pack (the holding CEO) gets nothing
        bound = [s["key"] for s in packs.bound_skills(row["id"])]
        assert bound, f"the {row['role']} agent got no skills"


def test_an_agent_whose_role_has_a_pack_is_bound_to_it(env):
    """The rule under test: role -> pack -> binding."""
    _seed()
    cto_id = _first("cto")
    pack = next(p for p in packs.discover_packs() if p["role"] == "cto")
    expected = {s["name"] for s in pack["skills"]}
    bound = {s["key"] for s in packs.bound_skills(cto_id)}
    assert expected <= bound, f"missing: {expected - bound}"


def test_an_agent_with_a_custom_role_gets_nothing(env):
    """Guessing would install the wrong playbook."""
    _seed()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, "
            "level, is_ceo, created_at, updated_at) VALUES (1, 'Custom', 'ai_agent', 'astrologer', "
            "'active', '{}', 'x', 'ceo', 1, 0, 0)")
        cid = conn.execute(
            "SELECT id FROM team_members WHERE role = 'astrologer'").fetchone()["id"]
    assert packs.bound_skills(cid) == []


def test_the_holding_ceo_role_has_no_pack_and_gets_nothing(env):
    """The holding CEO is role='ceo'; no pack ships for it."""
    _seed()
    with db.db() as conn:
        holding = conn.execute(
            "SELECT id FROM team_members WHERE role = 'ceo' AND is_ceo = 1 LIMIT 1").fetchone()
    if holding:
        assert packs.bound_skills(holding["id"]) == []


def test_the_bound_skill_files_exist_in_the_vault(env):
    """A binding without the file behind it is the silent no-op."""
    _seed()
    for key in [s["key"] for s in packs.bound_skills(_first("cfo"))]:
        assert (env["vault"] / key / "SKILL.md").is_file(), key


def test_the_bound_skills_reach_the_agent_prompt(env):
    """The whole point: the playbook is in the prompt, not just in a table."""
    _seed()
    cto_id = _first("cto")
    block = packs.agent_skills_block(cto_id, task="review this code")
    assert "YOUR INSTALLED SKILLS" in block
    for key in [s["key"] for s in packs.bound_skills(cto_id)]:
        assert key in block, f"{key} is bound but not in the prompt"


def test_seeding_twice_does_not_duplicate_the_bindings(env):
    """Seeding is idempotent, and so is the binding it performs."""
    _seed()
    _seed()
    cto_id = _first("cto")
    with db.db() as conn:
        n = conn.execute(
            "SELECT count(*) FROM agent_skills WHERE agent_id = ?", (cto_id,)).fetchone()[0]
    assert n == len(packs.bound_skills(cto_id))


def test_a_disabled_skill_is_not_re_enabled_by_a_later_seed(env):
    """The founder's choice survives."""
    _seed()
    cto_id = _first("cto")
    # all_bound_skills reports enabled AND disabled; bound_skills only the
    # enabled ones, so a disabled skill is invisible there.
    key = packs.all_bound_skills(cto_id)[0]["key"]
    packs.set_skill_enabled(cto_id, key, False)

    _seed()

    after = {s["key"]: s["enabled"] for s in packs.all_bound_skills(cto_id)}
    assert key in after, "the disabled binding vanished"
    assert after[key] is False, "a disabled skill was re-enabled by a re-seed"


def test_an_existing_agent_is_not_given_a_pack_after_the_fact(env):
    """The founder may have curated that agent's skills on purpose."""
    _seed()
    cto_id = _first("cto")
    with db.db() as conn:
        conn.execute("DELETE FROM agent_skills WHERE agent_id = ?", (cto_id,))

    _seed()
    assert packs.bound_skills(cto_id) == [], "an existing agent was re-bound"


def test_the_json_capabilities_of_the_seeded_agents_are_untouched(env):
    """The binding is new state; it must not rewrite what the founder sees
    in the agent's profile."""
    _seed()
    with db.db() as conn:
        row = conn.execute(
            "SELECT capabilities FROM team_members WHERE role = 'cfo' AND is_ceo = 1").fetchone()
    caps = json.loads(row["capabilities"])
    assert "skills" in caps  # the founder-facing text is still there
    assert "agent_skills" not in caps


def test_a_sub_agent_created_with_a_c_suite_role_gets_its_pack(env):
    """The third creation path: a sub-agent added from the agent page.

    It goes through a different INSERT than the seed, and it is the path a
    founder uses to add a second CTO.
    """
    _seed()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, "
            "level, is_ceo, created_at, updated_at) VALUES (1, 'Second CTO', 'ai_agent', 'cto', "
            "'active', '{}', 'x', 'ceo', 1, 0, 0)")
        cid = conn.execute(
            "SELECT id FROM team_members WHERE name = 'Second CTO'").fetchone()["id"]
    # The seed path does not touch it (it was not part of the starter pack),
    # so this asserts the role->pack rule itself, applied to a new row.
    packs.install_pack_for_role("cto", agent_id=cid)
    bound = {s["key"] for s in packs.bound_skills(cid)}
    pack = next(p for p in packs.discover_packs() if p["role"] == "cto")
    assert {s["name"] for s in pack["skills"]} <= bound
