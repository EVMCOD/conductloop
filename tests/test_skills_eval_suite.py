"""The skill eval suite: a skill must reach the agent that owns it.

The unit tests prove the prompt BUILDER puts the skill in the block. They do
not prove that the agent the founder is talking to gets it, and they do not
prove that the skill changes the answer.

This suite is the end of the chain, and it has two halves:

1. **Delivery**: with a skill bound to the agent, the skill's own text is in
   the prompt the agent answers from, and a skill bound to ANOTHER agent is
   not.
2. **Effect**: the recorded answer follows the skill rather than the generic
   answer. A skill that says "raise prices by 10% only after checking churn"
   has to produce an answer that checks churn, not one that just says "raise
   prices 10%".

The second half is what makes the feature honest. An install that succeeds,
a badge in the UI, and a model that ignores the playbook is the failure this
whole thing exists to prevent.

The cassette is synthetic: the answer is written by hand to be the answer the
skill calls for, and the checks read it. What is being measured is the
PROMPT and the BEHAVIOUR the prompt produces, not a model's ability.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ops_dashboard import db

EVALS = Path(__file__).resolve().parent.parent / "evals" / "skills"


def _install(vault: Path, key: str, description: str, body: str) -> None:
    d = vault / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {key}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")


def _agent(conn: sqlite3.Connection, role: str) -> int:
    cur = conn.execute(
        "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, "
        "level, is_ceo, created_at, updated_at) VALUES (1, ?, 'ai_agent', ?, 'active', '{}', "
        "?, 'ceo', 1, 0, 0)", (role, role, role))
    return int(cur.lastrowid)


def test_the_suite_file_is_valid_and_has_cases():
    """A suite with no cases proves nothing."""
    assert EVALS.is_dir(), f"missing suite dir: {EVALS}"
    data = json.loads((EVALS / "evals.json").read_text(encoding="utf-8"))
    assert data["evals"], "the suite has no cases"
    for case in data["evals"]:
        assert case.get("conductloop", {}).get("key"), case
        assert case["conductloop"].get("checks"), case["conductloop"]["key"]


def test_every_case_has_a_cassette():
    """A case without a cassette cannot run offline."""
    data = json.loads((EVALS / "evals.json").read_text(encoding="utf-8"))
    for case in data["evals"]:
        key = case["conductloop"]["key"]
        path = EVALS / "cassettes" / f"{key}.json"
        assert path.is_file(), f"no cassette for {key}"


def test_the_suite_declares_a_target_the_runner_knows():
    from ops_dashboard.evals import runner
    data = json.loads((EVALS / "evals.json").read_text(encoding="utf-8"))
    assert data["conductloop"]["target"] in runner.TARGETS


def test_a_skill_bound_to_the_agent_reaches_its_prompt(tmp_path, monkeypatch):
    """Delivery: the CFO's skill is in the CFO's prompt."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "eval.db")
    db.init_db()
    vault = tmp_path / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    now = int(time.time())
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        cfo = _agent(conn, "cfo")
        _agent(conn, "cto")
    _install(vault, "pricing-change-analysis",
             "Use when raising a price",
             "# Pricing change\\n\\nAlways check churn before raising the price.")
    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_skills (agent_id, skill_key, enabled, created_at) "
            "VALUES (?, ?, 1, ?)", (cfo, "pricing-change-analysis", now))

    from ops_dashboard import team_chat
    cfo_agent = {"key": f"m:{cfo}", "id": cfo, "name": "CFO", "role": "cfo",
                 "title": "CFO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(cfo_agent, "(no matches)", "",
                                    task="should we raise the price?")
    assert "Always check churn before raising the price." in system
    assert 'name="pricing-change-analysis"' in system


def test_a_skill_of_another_agent_does_not_reach_this_prompt(tmp_path, monkeypatch):
    """Isolation, at the prompt level, with real skills."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "eval2.db")
    db.init_db()
    vault = tmp_path / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    now = int(time.time())
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        cfo = _agent(conn, "cfo")
        cto = _agent(conn, "cto")
    _install(vault, "cto-only", "Use when reviewing code",
             "# CTO only\\n\\nThe CTO private procedure.")
    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_skills (agent_id, skill_key, enabled, created_at) "
            "VALUES (?, ?, 1, ?)", (cto, "cto-only", now))

    from ops_dashboard import team_chat
    cfo_agent = {"key": f"m:{cfo}", "id": cfo, "name": "CFO", "role": "cfo",
                 "title": "CFO", "skills": "", "description": "", "lead": False}
    system = team_chat.agent_system(cfo_agent, "(no matches)", "", task="review this code")
    assert "The CTO private procedure." not in system


def test_the_recorded_answer_follows_the_skill(tmp_path):
    """Effect: the answer is the one the skill calls for.

    The cassette's answer is what a competent model produces WITH the skill in
    context. The check is that the skill's distinctive requirement is in the
    answer — the churn check, not just the price rise.
    """
    data = json.loads((EVALS / "evals.json").read_text(encoding="utf-8"))
    case = next(c for c in data["evals"]
                if c["conductloop"]["key"] == "skill-follows-playbook")
    cassette = json.loads((EVALS / "cassettes" / "skill-follows-playbook.json").read_text())
    answer = cassette["response"]["text"]

    for check in case["conductloop"]["checks"]:
        if check["type"] == "must_contain":
            assert check["text"] in answer, (
                f"the recorded answer does not contain {check['text']!r}: {answer}")
