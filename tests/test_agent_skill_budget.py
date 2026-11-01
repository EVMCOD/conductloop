"""Every installed skill must reach the agent's prompt — or the agent must be
told which ones did not make it.

The catalog grew: a pack now carries up to seven skills, and the bodies total
more than the prompt budget. The old code walked the bound skills in
alphabetical order and stopped at the budget, so the skills past the cut were
silently left out — while the UI still showed them as installed and enabled.

That is the failure this feature exists to prevent: an install that succeeds,
a badge in the UI, and a model that never sees the skill. Here it reappears
one level up, in the budget.

So the rule is: when a skill does not fit, the agent is told which ones were
left out and why. The user can then disable one, or the agent can ask for it.
Silently dropping is not an option.
"""
from __future__ import annotations

import time

import pytest

from ops_dashboard.skills import packs


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "budget.db")
    db.init_db()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    now = int(time.time())
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        cur = conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, "
            "level, is_ceo, created_at, updated_at) VALUES (1, 'Atlas', 'ai_agent', 'cto', "
            "'active', '{}', 'CTO', 'ceo', 1, ?, ?)", (now, now))
        agent_id = cur.lastrowid
    return {"vault": vault, "agent_id": agent_id, "db": db}


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


def test_a_skill_that_does_not_fit_is_still_listed(env):
    """The agent must be able to see that a skill exists and did not fit."""
    big = "\n".join(f"line {i}" for i in range(6000))
    for i in range(6):
        _install(env["vault"], f"skill-{i}", f"# Skill {i}\n\n{big}")
        _bind(env["db"], env["agent_id"], f"skill-{i}")

    block = packs.agent_skills_block(env["agent_id"])
    assert block, "the block is empty"
    # Every installed skill is named, full text or not.
    for i in range(6):
        assert f"skill-{i}" in block, f"skill-{i} is invisible"


def test_the_block_stays_within_the_budget(env):
    big = "\n".join(f"line {i}" for i in range(6000))
    for i in range(6):
        _install(env["vault"], f"skill-{i}", f"# Skill {i}\n\n{big}")
        _bind(env["db"], env["agent_id"], f"skill-{i}")
    block = packs.agent_skills_block(env["agent_id"])
    assert len(block) <= packs.MAX_PROMPT_CHARS + 2000, len(block)


def test_every_skill_fits_when_the_set_is_small(env):
    for i in range(3):
        _install(env["vault"], f"small-{i}", f"# Small {i}\n\nshort body")
        _bind(env["db"], env["agent_id"], f"small-{i}")
    block = packs.agent_skills_block(env["agent_id"])
    for i in range(3):
        assert f"small-{i}" in block
    assert "did not fit" not in block.lower()


def test_the_skills_that_did_not_fit_are_named(env):
    """Not just a count: the agent must know WHICH ones, by name."""
    big = "\n".join(f"line {i}" for i in range(6000))
    for i in range(6):
        _install(env["vault"], f"skill-{i}", f"# Skill {i}\n\n{big}")
        _bind(env["db"], env["agent_id"], f"skill-{i}")
    block = packs.agent_skills_block(env["agent_id"])
    included = {k for k in (f"skill-{i}" for i in range(6)) if f'name="{k}"' in block}
    missing = {f"skill-{i}" for i in range(6)} - included
    assert missing, "nothing was left out, so the test proves nothing"
    for key in missing:
        assert key in block, f"{key} was left out but is not named"


def test_the_task_decides_which_skill_keeps_its_full_text(env):
    """With the budget tight, the skill that matches the task is the one that
    gets the full body."""
    big = "\n".join(f"line {i}" for i in range(6000))
    _install(env["vault"], "pricing-review", f"# Pricing\n\n{big}")
    _install(env["vault"], "unrelated-thing", f"# Unrelated\n\n{big}")
    for k in ("pricing-review", "unrelated-thing"):
        _bind(env["db"], env["agent_id"], k)

    block = packs.agent_skills_block(env["agent_id"], task="please review our pricing")
    assert 'name="pricing-review"' in block
    assert 'name="unrelated-thing"' not in block
    # ...and the one that did not fit is still discoverable.
    assert "unrelated-thing" in block


def test_the_reported_set_matches_what_was_actually_dropped(env):
    """The report must be true: a skill named as left out is really absent
    from the block, and one present in the block is not named as left out."""
    big = "\n".join(f"line {i}" for i in range(6000))
    for i in range(6):
        _install(env["vault"], f"skill-{i}", f"# Skill {i}\n\n{big}")
        _bind(env["db"], env["agent_id"], f"skill-{i}")
    block = packs.agent_skills_block(env["agent_id"])
    included = {f"skill-{i}" for i in range(6) if f'name="skill-{i}"' in block}
    dropped = {f"skill-{i}" for i in range(6) if f"skill-{i}" in block
               and f'name="skill-{i}"' not in block}
    assert included, "nothing fit"
    assert dropped, "nothing was dropped"
    # Every dropped skill is named; every included skill is not named as dropped.
    for key in dropped:
        assert key in block
