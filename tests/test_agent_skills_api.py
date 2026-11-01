"""L7: the agent-skill-pack HTTP API.

Registered routes (ops_dashboard/routes_agent_skills.py):

* GET  /api/skills/packs
* GET  /api/agents/<id>/skills
* POST /api/agents/<id>/skills/install
* POST /api/agents/<id>/skills/uninstall
* POST /api/agents/<id>/skills/toggle

Local-only: these describe the owner's own agent configuration.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import db
from ops_dashboard.skills import packs


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.db")
    db.init_db()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    packs_root = tmp_path / "packs"
    pack_dir = packs_root / "cto"
    (pack_dir / "code-review").mkdir(parents=True)
    (pack_dir / "code-review" / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Review code carefully\n---\n\n# cr\n\nBody.\n",
        encoding="utf-8")
    (pack_dir / "pack.json").write_text(
        json.dumps({"id": "cto", "title": "CTO", "role": "cto", "description": "d",
                    "skills": ["code-review"]}), encoding="utf-8")
    monkeypatch.setattr(packs, "PACKS_DIR", packs_root)

    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        cur = conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, level, "
            "is_ceo, created_at, updated_at) VALUES (1, 'Atlas (CTO)', 'ai_agent', 'cto', 'active', "
            "'{}', 'CTO', 'ceo', 1, 0, 0)")
        agent_id = cur.lastrowid

    import ops_dashboard.server as server_mod
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.DashboardHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"port": port, "agent_id": agent_id, "vault": vault}
    httpd.shutdown()
    httpd.server_close()


def _get(port: int, path: str):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _post(port: int, path: str, body: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_packs_endpoint_lists_the_shipped_packs(server):
    status, data = _get(server["port"], "/api/skills/packs")
    assert status == 200 and data["ok"]
    ids = {p["id"] for p in data["packs"]}
    assert "cto" in ids


def test_agent_skills_endpoint_starts_empty(server):
    status, data = _get(server["port"], f"/api/agents/{server['agent_id']}/skills")
    assert status == 200 and data["ok"]
    assert data["skills"] == [] and data["packs"] == []


def test_install_then_list_then_uninstall(server):
    port, agent_id = server["port"], server["agent_id"]
    status, data = _post(port, f"/api/agents/{agent_id}/skills/install", {"pack": "cto"})
    assert status == 200 and data["ok"]
    assert data["installed"] == ["code-review"]

    status, data = _get(port, f"/api/agents/{agent_id}/skills")
    assert [s["key"] for s in data["skills"]] == ["code-review"]
    assert data["packs"] == ["cto"]

    status, data = _post(port, f"/api/agents/{agent_id}/skills/uninstall", {"pack": "cto"})
    assert status == 200 and data["removed"] == ["code-review"]
    status, data = _get(port, f"/api/agents/{agent_id}/skills")
    assert data["skills"] == []


def test_toggle_disables_a_skill(server):
    port, agent_id = server["port"], server["agent_id"]
    _post(port, f"/api/agents/{agent_id}/skills/install", {"pack": "cto"})
    status, data = _post(port, f"/api/agents/{agent_id}/skills/toggle",
                         {"skill": "code-review", "enabled": False})
    assert status == 200 and data["enabled"] is False
    _status, data = _get(port, f"/api/agents/{agent_id}/skills")
    assert data["skills"][0]["enabled"] is False


def test_install_unknown_pack_is_404(server):
    status, data = _post(server["port"], f"/api/agents/{server['agent_id']}/skills/install",
                         {"pack": "nope"})
    assert status == 404 and data["error"] == "not_found"


def test_install_without_pack_is_400(server):
    status, data = _post(server["port"], f"/api/agents/{server['agent_id']}/skills/install", {})
    assert status == 400 and data["error"] == "bad_request"


def test_toggle_a_skill_that_is_not_bound_is_404(server):
    status, data = _post(server["port"], f"/api/agents/{server['agent_id']}/skills/toggle",
                         {"skill": "ghost", "enabled": True})
    assert status == 404 and data["error"] == "not_found"


def test_bad_agent_id_is_400(server):
    status, data = _get(server["port"], "/api/agents/abc/skills")
    assert status == 400 and data["error"] == "bad_id"
