"""The founder must be able to see what the agent will actually read.

The panel lists skill names. That does not answer the question that matters
when a pack has seven skills and the prompt has room for three: which ones
make it, and which ones were left out.

So the API answers that directly: given a task, which skills keep their full
body, which are listed by name only, and what the prompt block looks like.

This is the piece that turns "installed" into "I can see what that does".
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_dashboard import db


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "preview.db")
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
    # A skill that fits and one that does not.
    d = vault / "small-skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: small-skill\ndescription: Use when reviewing code\n---\n\n"
        "# Small\n\nRead the contract first.\n", encoding="utf-8")
    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_skills (agent_id, skill_key, enabled, created_at) "
            "VALUES (?, ?, 1, ?)", (agent_id, "small-skill", now))

    import ops_dashboard.server as server_mod
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.DashboardHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"port": port, "agent_id": agent_id}
    httpd.shutdown()
    httpd.server_close()


def _get(port: int, path: str):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_the_preview_lists_the_skills_that_reach_the_prompt(server):
    status, data = _get(server["port"], f"/api/agents/{server['agent_id']}/skills/preview?task=review")
    assert status == 200 and data["ok"]
    assert [s["name"] for s in data["included"]] == ["small-skill"]
    assert data["included"][0]["description"]


def test_the_preview_shows_the_prompt_block(server):
    status, data = _get(server["port"], f"/api/agents/{server['agent_id']}/skills/preview?task=review")
    assert status == 200
    assert "Read the contract first." in data["prompt"]


def test_the_preview_reports_what_did_not_fit(server):
    """The point of the preview: knowing what was left out."""
    # Add a skill too big to fit.
    big = vault_dir(server)
    d = big / "huge-skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: huge-skill\ndescription: Use when doing something big\n---\n\n"
        + ("x" * 40_000) + "\n", encoding="utf-8")
    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_skills (agent_id, skill_key, enabled, created_at) "
            "VALUES (?, ?, 1, 0)", (server["agent_id"], "huge-skill"))

    status, data = _get(server["port"], f"/api/agents/{server['agent_id']}/skills/preview?task=review")
    assert status == 200
    names = {s["name"] for s in data["included"]}
    assert "huge-skill" not in names
    omitted = {s["name"]: s for s in data["omitted"]}
    assert "huge-skill" in omitted
    # A body cut in half is worse than none: the agent cannot tell it is
    # missing the rest, so it is listed, not handed over in pieces.
    assert omitted["huge-skill"]["reason"] == "too_large"


def test_the_preview_works_without_a_task(server):
    status, data = _get(server["port"], f"/api/agents/{server['agent_id']}/skills/preview")
    assert status == 200 and data["ok"]
    assert data["included"]


def test_the_preview_of_an_agent_without_skills_is_empty(server):
    status, data = _get(server["port"], "/api/agents/99999/skills/preview")
    assert status == 200 and data["ok"]
    assert data["included"] == []


def test_a_bad_agent_id_is_400(server):
    status, data = _get(server["port"], "/api/agents/abc/skills/preview")
    assert status == 400 and data["error"] == "bad_id"


def vault_dir(server) -> Path:
    from ops_dashboard.skills import catalog
    return catalog.vault_dir()
