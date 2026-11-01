"""Existing agents must get their role's pack when the feature ships.

The auto-bind only covers agents created from now on. The agents already in
the database were created before the packs existed, and the seed skips them
("already exists by name"). So the feature would ship inert for exactly the
people who already use the product: the fourteen CEOs are on disk with zero
skills, and nothing would ever change that.

A migration is the right place for this: it runs once, it is idempotent, and
it does not depend on the user pressing anything. The rules are the same ones
the creation path obeys, so the two cannot disagree:

- Only an agent whose role has a pack gets one.
- A skill the user disabled stays disabled. The migration reads the current
  `enabled` state and writes it back.
- An agent that already has skills bound is left alone: it was curated.
- An agent whose pack cannot be copied is skipped, not fatal.
"""
from __future__ import annotations

import time

import pytest

from ops_dashboard import db, migrations
from ops_dashboard.skills import packs


def _agent(conn, role: str, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, "
        "level, is_ceo, created_at, updated_at) VALUES (1, ?, 'ai_agent', ?, 'active', '{}', "
        "?, 'ceo', 1, 0, 0)", (name, role, role))
    return int(cur.lastrowid or 0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mig.db")
    db.init_db()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    monkeypatch.setattr(packs, "PACKS_DIR", packs.PACKS_DIR)  # real shipped packs
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
    return tmp_path


def test_the_migration_binds_the_pack_of_an_existing_agent(env):
    """The CEO that was already in the database gets its playbook."""
    with db.db() as conn:
        cfo = _agent(conn, "cfo", "CFO Existente")

    migrations.bind_role_packs_to_existing_agents()

    with db.db() as conn:
        rows = conn.execute(
            "SELECT skill_key FROM agent_skills WHERE agent_id = ? AND enabled = 1",
            (cfo,)).fetchall()
    assert rows, "the existing CFO got no skills"


def test_the_migration_is_idempotent(env):
    with db.db() as conn:
        cfo = _agent(conn, "cfo", "CFO Idempotente")
    migrations.bind_role_packs_to_existing_agents()
    with db.db() as conn:
        first = conn.execute(
            "SELECT COUNT(*) FROM agent_skills WHERE agent_id = ?", (cfo,)).fetchone()[0]
    migrations.bind_role_packs_to_existing_agents()
    migrations.bind_role_packs_to_existing_agents()
    with db.db() as conn:
        second = conn.execute(
            "SELECT COUNT(*) FROM agent_skills WHERE agent_id = ?", (cfo,)).fetchone()[0]
    assert first == second, "running it twice added rows"


def test_the_migration_does_not_disable_a_skill_the_user_turned_off(env):
    with db.db() as conn:
        cfo = _agent(conn, "cfo", "CFO Con Off")
    migrations.bind_role_packs_to_existing_agents()
    with db.db() as conn:
        key = conn.execute(
            "SELECT skill_key FROM agent_skills WHERE agent_id = ? LIMIT 1", (cfo,)).fetchone()[0]
        conn.execute("UPDATE agent_skills SET enabled = 0 WHERE agent_id = ? AND skill_key = ?",
                     (cfo, key))

    migrations.bind_role_packs_to_existing_agents()

    with db.db() as conn:
        enabled = conn.execute(
            "SELECT enabled FROM agent_skills WHERE agent_id = ? AND skill_key = ?",
            (cfo, key)).fetchone()[0]
    assert enabled == 0, "the migration re-enabled a skill the user turned off"


def test_the_migration_leaves_an_agent_with_its_own_skills_alone(env):
    """A curated agent is not touched."""
    with db.db() as conn:
        cfo = _agent(conn, "cfo", "CFO Curado")
    with db.db() as conn:
        conn.execute(
            "INSERT INTO agent_skills (agent_id, skill_key, enabled, created_at) "
            "VALUES (?, 'mi-skill-propia', 1, ?)", (cfo, int(time.time())))
    before = packs.bound_skills(cfo)

    migrations.bind_role_packs_to_existing_agents()

    after = packs.bound_skills(cfo)
    assert {s["key"] for s in after} == {s["key"] for s in before}, (
        "the migration added skills to an agent that already had its own")


def test_the_migration_skips_an_agent_whose_role_has_no_pack(env):
    with db.db() as conn:
        weird = _agent(conn, "role_que_no_existe", "Agente Raro")
    migrations.bind_role_packs_to_existing_agents()
    with db.db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM agent_skills WHERE agent_id = ?", (weird,)).fetchone()[0]
    assert n == 0


def test_the_migration_survives_a_broken_pack(env, monkeypatch):
    """One agent failing must not stop the others."""
    with db.db() as conn:
        _agent(conn, "cfo", "CFO Bueno")
        other = _agent(conn, "cto", "CTO Bueno")

    real = packs.install_pack_for_role
    calls = {"n": 0}

    def flaky(role, agent_id=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the vault is read-only")
        return real(role, agent_id=agent_id, **kw)

    monkeypatch.setattr(packs, "install_pack_for_role", flaky)
    migrations.bind_role_packs_to_existing_agents()

    with db.db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM agent_skills WHERE agent_id = ?", (other,)).fetchone()[0]
    assert n, "one broken agent stopped the rest"


def test_the_migration_runs_as_part_of_the_migration_chain(env):
    """It has to actually be wired into the migration list, not just defined."""
    versions = {v for v, _d, _f in migrations.MIGRATIONS}
    assert 28 in versions, "the backfill is not a registered migration"
