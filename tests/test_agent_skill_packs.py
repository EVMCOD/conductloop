"""L7: per-agent skill packs — the Boardroom C-Suite gets real skills.

The gap this closes: ConductLoop reads agentskills.io skills into one global
catalog, but the 12 Boardroom CEOs have no skills of their own. Their
``capabilities`` field carries a free-text skill list ("architecture; code
review") that is only ever pasted into a prompt. Nothing installs a skill for
one agent without installing it for the Brain.

Grok Bot's model (read from its own binary) is per-agent: each Bot has its own
skill set (``AddGrokBotAgentSkill`` / ``RemoveGrokBotAgentSkill`` /
``UpdateGrokBotAgentSkill`` / ``ListGrokBotAgentSkills``), a skill is a folder
with a SKILL.md, and a Bot runs a skill by opening it.

This module implements that shape for ConductLoop:

* A **pack** is a folder of skills shipped with the app (one per C-suite role).
* Installing a pack for an agent copies its skills into the vault's skills dir
  and records the binding in ``agent_skills``.
* The agent's prompt then offers only the skills bound to it.
* Uninstalling removes the binding (the user's own skill files are never
  deleted without an explicit request).

No network, no remote catalogs (D16): packs ship inside the package.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ops_dashboard import migrations
from ops_dashboard.skills import packs

# ---------- migration 27 ----------

def test_migration_27_creates_agent_skills_table():
    conn = sqlite3.connect(":memory:")
    applied = [v for v, _ in migrations.run_migrations(conn)]
    assert 27 in applied
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_skills)")}
    assert {"agent_id", "skill_key", "enabled", "created_at"} <= cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "agent_skills" in tables


def test_migration_27_is_idempotent():
    conn = sqlite3.connect(":memory:")
    migrations.run_migrations(conn)
    migrations.run_migrations(conn)
    rows = conn.execute("SELECT COUNT(*) FROM agent_skills").fetchone()
    assert rows[0] == 0


# ---------- pack discovery ----------

def _pack(packs_root: Path, name: str, skills: dict[str, str]) -> Path:
    """A pack folder under ``packs_root``: <name>/<skill>/SKILL.md plus a manifest."""
    root = packs_root / name
    root.mkdir(parents=True)
    for skill, desc in skills.items():
        d = root / skill
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: {desc}\n---\n\n# {skill}\n\nBody.\n",
            encoding="utf-8",
        )
    (root / "pack.json").write_text(
        json.dumps({"id": name, "title": name.title(), "role": name,
                    "description": f"{name} pack", "skills": list(skills)}),
        encoding="utf-8",
    )
    return root


def test_discover_packs_reads_manifests(tmp_path):
    packs_root = tmp_path / "packs"
    packs_root.mkdir()
    _pack(packs_root, "cto", {"code-review": "Review code carefully"})
    _pack(packs_root, "cfo", {"pricing-review": "Review pricing"})
    found = packs.discover_packs(packs_root)
    ids = {p["id"] for p in found}
    assert ids == {"cto", "cfo"}
    cto = next(p for p in found if p["id"] == "cto")
    assert cto["title"] == "Cto" and cto["role"] == "cto"
    assert [s["name"] for s in cto["skills"]] == ["code-review"]
    assert cto["skills"][0]["description"] == "Review code carefully"


def test_discover_packs_skips_pack_without_manifest(tmp_path):
    packs_root = tmp_path / "packs"
    bad = packs_root / "broken"
    (bad / "x").mkdir(parents=True)
    (bad / "x" / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\n", encoding="utf-8")
    assert packs.discover_packs(packs_root) == []


def test_discover_packs_ignores_pack_whose_manifest_lists_missing_skill(tmp_path):
    packs_root = tmp_path / "packs"
    packs_root.mkdir()
    root = _pack(packs_root, "cto", {"code-review": "d"})
    manifest = json.loads((root / "pack.json").read_text())
    manifest["skills"] = ["code-review", "ghost"]
    (root / "pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    found = packs.discover_packs(packs_root)
    assert [s["name"] for s in found[0]["skills"]] == ["code-review"]


# ---------- installing a pack for an agent ----------

@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "packs.db")
    db.init_db()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    packs_root = tmp_path / "packs"
    packs_root.mkdir()
    _pack(packs_root, "cto", {"code-review": "Review code carefully"})
    monkeypatch.setattr(packs, "PACKS_DIR", packs_root)
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)"
        )
        cur = conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, level, "
            "is_ceo, created_at, updated_at) VALUES (1, 'Atlas (CTO)', 'ai_agent', 'cto', 'active', "
            "'{}', 'CTO', 'ceo', 1, 0, 0)"
        )
        agent_id = cur.lastrowid
    return {"packs": packs_root, "vault": vault, "agent_id": agent_id, "tmp": tmp_path}


def test_install_pack_binds_skills_to_that_agent_only(env):
    res = packs.install_pack("cto", agent_id=env["agent_id"])
    assert res["ok"]
    assert res["installed"] == ["code-review"]
    bound = packs.bound_skills(env["agent_id"])
    assert [s["key"] for s in bound] == ["code-review"]
    # The skill file is in the vault, so the normal indexer picks it up.
    assert (env["vault"] / "code-review" / "SKILL.md").is_file()


def test_install_pack_is_idempotent(env):
    first = packs.install_pack("cto", agent_id=env["agent_id"])
    second = packs.install_pack("cto", agent_id=env["agent_id"])
    assert first["ok"] and second["ok"]
    assert len(packs.bound_skills(env["agent_id"])) == 1


def test_install_unknown_pack_is_an_error(env):
    with pytest.raises(packs.PackError):
        packs.install_pack("nope", agent_id=env["agent_id"])


def test_install_pack_refuses_unknown_agent(env):
    with pytest.raises(packs.PackError):
        packs.install_pack("cto", agent_id=999999)


def test_install_pack_does_not_overwrite_a_user_skill(env):
    user_dir = env["vault"] / "code-review"
    user_dir.mkdir()
    (user_dir / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: MY OWN VERSION\n---\nmine\n", encoding="utf-8")
    res = packs.install_pack("cto", agent_id=env["agent_id"])
    assert res["ok"]
    assert res["kept"] == ["code-review"] and res["installed"] == []
    assert "MY OWN VERSION" in (user_dir / "SKILL.md").read_text(encoding="utf-8")


def test_uninstall_removes_the_binding_but_not_the_file(env):
    packs.install_pack("cto", agent_id=env["agent_id"])
    res = packs.uninstall_pack("cto", agent_id=env["agent_id"])
    assert res["ok"] and res["removed"] == ["code-review"]
    assert packs.bound_skills(env["agent_id"]) == []
    # The skill file stays: it may be the user's own, and we never delete
    # files the user owns without asking.
    assert (env["vault"] / "code-review" / "SKILL.md").is_file()


def test_enable_disable_a_bound_skill(env):
    packs.install_pack("cto", agent_id=env["agent_id"])
    assert packs.set_skill_enabled(env["agent_id"], "code-review", False) is True
    assert packs.bound_skills(env["agent_id"]) == []
    assert packs.set_skill_enabled(env["agent_id"], "code-review", True) is True
    assert len(packs.bound_skills(env["agent_id"])) == 1


def test_bound_skills_only_returns_enabled_rows(env):
    packs.install_pack("cto", agent_id=env["agent_id"])
    packs.set_skill_enabled(env["agent_id"], "code-review", False)
    assert packs.bound_skills(env["agent_id"]) == []
    assert [s["key"] for s in packs.all_bound_skills(env["agent_id"])] == ["code-review"]


def test_a_skill_is_bound_to_one_agent_not_to_all(env):
    """The point of per-agent binding: installing for the CTO must not give
    the skill to another agent."""
    packs.install_pack("cto", agent_id=env["agent_id"])
    assert packs.bound_skills(999) == []


# ---------- the shipped packs ----------

def test_shipped_packs_cover_every_c_suite_role():
    shipped = packs.discover_packs(packs.PACKS_DIR)
    ids = {p["id"] for p in shipped}
    assert {"cso", "cfo", "cto", "cmo", "vp_design", "vp_frontend", "vp_mobile",
            "vp_devops", "head_product", "head_sales", "general_counsel", "head_cs"} <= ids


def test_every_shipped_skill_validates():
    """A shipped skill that does not validate is a broken install for the user."""
    from ops_dashboard.skills import parse, validate
    for pack in packs.discover_packs(packs.PACKS_DIR):
        for skill in pack["skills"]:
            parsed = parse.parse_skill_file(Path(skill["path"]))
            assert parsed.frontmatter is not None, f"{skill['name']}: no frontmatter"
            key, diags = validate.validate(parsed.frontmatter, skill["name"], parsed.body)
            assert not validate.has_errors(diags), f"{skill['name']}: {diags}"
            assert key == skill["name"]


def test_every_shipped_skill_passes_the_linter():
    """Same gate agent-proposed skills must pass: no secrets, no user paths."""
    from ops_dashboard.skills import lint
    for pack in packs.discover_packs(packs.PACKS_DIR):
        for skill in pack["skills"]:
            text = Path(skill["path"]).read_text(encoding="utf-8")
            findings = lint.scan_text(text, skill["name"])
            blocking = lint.blocking(findings)
            assert not blocking, f"{skill['name']}: {blocking}"


def test_shipped_packs_are_read_only_source():
    """Packs live in the package, never in the user's vault."""
    assert packs.PACKS_DIR != packs.catalog.vault_dir()
    assert Path(packs.PACKS_DIR).is_dir()
