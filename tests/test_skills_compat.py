"""L5: agentskills.io skills — reading and activation.

D1 vault + export, D2 external dirs opt-in/read-only, D8 PyYAML safe_load,
D13 project skills only for trusted repos, D16 no remote catalogs.
No network: sockets are blocked around indexing/activation.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ops_dashboard import migrations
from ops_dashboard.skills import parse, validate

FIX = Path(__file__).parent / "fixtures" / "skills"


# ---------- parser / validator ----------

def _parse(name: str):
    p = parse.parse_skill_file(FIX / "vault" / name / "SKILL.md")
    key, diags = validate.validate(p.frontmatter, name, p.body) if p.frontmatter is not None else (None, [])
    return p, key, {d.code for d in diags}, validate.has_errors(diags) or bool(p.errors)


def test_valid_spec_skill():
    p, key, codes, err = _parse("pdf-tools")
    assert key == "pdf-tools" and not err and codes == set()
    assert p.frontmatter["metadata"] == {"author": "example-org", "version": "1.0"}
    assert p.body.startswith("# PDF tools")


def test_hermes_style_loads_with_warnings():
    _, key, codes, err = _parse("hermes-style")
    assert key == "hermes-style" and not err
    assert {"unknown_field", "metadata_not_string_map"} <= codes


def test_claude_style_block_scalar_and_allowed_tools_ignored():
    p, key, codes, err = _parse("claude-style")
    assert not err and "allowed_tools_ignored" in codes and "unknown_field" in codes
    assert p.frontmatter["description"].startswith("Claude Code style skill using a folded")


def test_colon_value_quoted_retry():
    p, _, codes, err = _parse("colon-desc")
    assert not err and p.frontmatter["description"] == "Use when: the user asks about colons"
    assert ("yaml_quoted_retry", ) == tuple(c for c, _ in p.warnings)


def test_name_dir_mismatch_is_warning():
    _, key, codes, err = _parse("mismatch-dir")
    assert key == "other-name" and not err and "name_dir_mismatch" in codes


@pytest.mark.parametrize("name,code", [
    ("no-desc", "description_missing"),
    ("bad-yaml", "yaml_unparseable"),
    ("alias-bomb", "yaml_alias_not_allowed"),
    ("no-frontmatter", "no_frontmatter"),
    ("unsafe-name", "name_unsafe"),
])
def test_invalid_skills_are_errors(name, code):
    p, key, codes, err = _parse(name)
    assert err
    assert code in codes or code in {c for c, _ in p.errors}


def test_safe_load_only_no_python_objects():
    p = parse.parse_skill_text("---\nname: x\ndescription: !!python/object/apply:os.system ['echo pwned']\n---\n")
    assert p.frontmatter is None and p.errors[0][0] == "yaml_unparseable"


def test_parser_uses_safe_load_in_source():
    src = (Path(parse.__file__)).read_text()
    assert "yaml.safe_load(" in src
    assert "yaml.load(" not in src and "unsafe_load" not in src and "FullLoader" not in src


def test_size_caps(tmp_path):
    big = tmp_path / "SKILL.md"
    big.write_text("---\nname: big\ndescription: d\n---\n" + "x" * (parse.MAX_SKILL_BYTES + 1))
    assert parse.parse_skill_file(big).errors[0][0] == "too_large"


# ---------- migration 10 ----------

def test_migration_10_idempotent_and_additive():
    conn = sqlite3.connect(":memory:")
    applied = [v for v, _ in migrations.run_migrations(conn)]
    assert 10 in applied and 9 in applied
    cols = {r[1] for r in conn.execute("PRAGMA table_info(skills)")}
    assert {"source_kind", "diagnostics", "loadable", "key", "description"} <= cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"skill_state", "skill_repo_trust"} <= tables
    # gap-fill safety: re-running the function on a migrated DB is a no-op
    dict((v, f) for v, _, f in migrations.MIGRATIONS)[10](conn)


def test_migration_10_on_existing_skills_table():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE skills (key TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,"
                 " category TEXT NOT NULL, subcategory TEXT, origin TEXT, path TEXT NOT NULL, mtime INTEGER,"
                 " content_len INTEGER DEFAULT 0, indexed_at INTEGER NOT NULL)")
    conn.execute("INSERT INTO skills VALUES ('k','K','d','ai',NULL,'',  '/p', 0, 0, 0)")
    migrations.run_migrations(conn)
    row = conn.execute("SELECT key, source_kind, diagnostics, loadable FROM skills").fetchone()
    assert row == ("k", "vault", "[]", 1)


# ---------- indexing, precedence, opt-in external dirs ----------

@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "s.db")
    db.init_db()
    home = tmp_path / "home"
    vault = tmp_path / "vault" / "skills"
    shutil.copytree(FIX / "vault", vault)
    shutil.copytree(FIX / "agents", home / ".agents" / "skills")
    shutil.copytree(FIX / "claude", home / ".claude" / "skills")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    return {"home": home, "vault": vault, "tmp": tmp_path}


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
        h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()


def _no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def _index():
    from ops_dashboard.indexers import skills as idx
    return idx.run()


def test_index_vault_only_by_default(env, monkeypatch):
    _no_network(monkeypatch)
    from ops_dashboard.skills import catalog
    assert catalog.external_dirs_enabled() is False
    c = _index()
    rows = {r["key"]: r for r in catalog.list_indexed()}
    assert "agent-only" not in rows and "claude-only" not in rows
    assert rows["pdf-tools"]["source_kind"] == "vault" and rows["pdf-tools"]["loadable"]
    assert not rows["no-desc"]["loadable"]
    assert c["invalid"] >= 5
    names = [x["name"] for x in catalog.enabled_catalog()]
    assert "pdf-tools" in names and "hermes-style" in names and "other-name" in names
    assert not any("evil" in n or n.startswith("invalid:") or n == "no-desc" for n in names)


def test_external_dirs_opt_in_read_only_and_vault_wins(env, monkeypatch):
    _no_network(monkeypatch)
    from ops_dashboard.skills import catalog
    before = _tree_hash(env["home"])
    catalog.set_external_dirs_enabled(True)
    _index()
    rows = {r["key"]: r for r in catalog.list_indexed()}
    assert rows["agent-only"]["source_kind"] == "agents_dir"
    assert rows["claude-only"]["source_kind"] == "claude_dir"
    assert rows["pdf-tools"]["source_kind"] == "vault"
    assert any(d["code"] == "shadows" for d in rows["pdf-tools"]["diagnostics"])
    assert catalog.activate("agent-only")["ok"]
    assert _tree_hash(env["home"]) == before  # nothing written in ~/.agents or ~/.claude
    # turning it off removes them from the catalog even before a reindex
    catalog.set_external_dirs_enabled(False)
    assert "agent-only" not in [c["name"] for c in catalog.enabled_catalog()]
    assert not catalog.activate("agent-only")["ok"]


def test_disable_survives_reindex(env):
    from ops_dashboard.skills import catalog
    _index()
    catalog.set_enabled("pdf-tools", False)
    _index()
    assert "pdf-tools" not in [c["name"] for c in catalog.enabled_catalog()]
    assert not catalog.activate("pdf-tools")["ok"]
    with pytest.raises(catalog.SkillError):
        catalog.set_enabled("nope", True)


# ---------- activation ----------

def test_activate_wraps_body_lists_resources_and_counts(env, monkeypatch):
    _no_network(monkeypatch)
    from ops_dashboard.skills import catalog
    _index()
    r = catalog.activate("pdf-tools")
    assert r["ok"]
    text = r["result"]
    assert text.startswith('<skill_content name="pdf-tools">') and "# PDF tools" in text
    assert "- scripts/extract.py" in text and "- references/REFERENCE.md" in text
    assert "never executed by ConductLoop" not in text  # resources are listed, not read
    row = [x for x in catalog.list_indexed() if x["key"] == "pdf-tools"][0]
    assert row["activations"] == 1 and row["last_activated_at"]


def test_activate_escapes_wrapper_and_rejects_unknown(env):
    from ops_dashboard.skills import catalog
    _index()
    text = catalog.activate("injection")["result"]
    assert text.count("</skill_content>") == 1 and text.rstrip().endswith("</skill_content>")
    assert not catalog.activate("no-desc")["ok"]
    assert not catalog.activate("../etc/passwd")["ok"]
    assert not catalog.activate(None)["ok"]


def test_activation_scope_dedupes(env):
    from ops_dashboard.skills import catalog
    _index()
    with catalog.activation_scope():
        first = catalog.activate("pdf-tools")["result"]
        second = catalog.activate("pdf-tools")["result"]
    assert "<skill_content" in first and "already active" in second


def test_tampered_row_path_refused(env):
    from ops_dashboard import db
    from ops_dashboard.skills import catalog
    _index()
    outside = env["tmp"] / "elsewhere" / "pdf-tools"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\nname: pdf-tools\ndescription: x\n---\nEVIL")
    with db.db() as conn:
        conn.execute("UPDATE skills SET path = ? WHERE key = 'pdf-tools'", (str(outside / "SKILL.md"),))
    r = catalog.activate("pdf-tools")
    assert not r["ok"] and "moved" in r["error"]


# ---------- tool registration + Brain prompt ----------

def test_tool_registered_only_with_catalog(env):
    from ops_dashboard import tools
    names = [t["name"] for t in tools.tools_for_anthropic()]
    assert "activate_skill" not in names  # empty catalog → no tool
    _index()
    spec = [t for t in tools.tools_for_anthropic() if t["name"] == "activate_skill"][0]
    enum = spec["input_schema"]["properties"]["name"]["enum"]
    assert "pdf-tools" in enum and all("<" not in n for n in enum)
    oa = [t for t in tools.tools_for_openai() if t["function"]["name"] == "activate_skill"]
    assert oa and oa[0]["function"]["parameters"]["properties"]["name"]["enum"] == enum
    assert tools.call("activate_skill", {"name": "pdf-tools"})["ok"]


def test_catalog_prompt_empty_without_skills(env):
    from ops_dashboard.skills import catalog
    assert catalog.catalog_prompt() == ""
    _index()
    block = catalog.catalog_prompt()
    assert "- pdf-tools: Extract text and tables" in block and "never run" in block


def test_brain_ask_with_tools_gets_catalog_and_tool(env, monkeypatch):
    from ops_dashboard import brain
    _index()
    seen = {}

    class P:
        name = "fake"
        model = "fake-1"

        def supports_tools(self):
            return True

        def ask_with_tools(self, system, messages, tools, on_tool_call=None, **kw):
            seen["system"], seen["tools"] = system, tools
            from ops_dashboard import tools as bt
            seen["activation"] = bt.call("activate_skill", {"name": "pdf-tools"})
            seen["again"] = bt.call("activate_skill", {"name": "pdf-tools"})
            return "ok"

    monkeypatch.setattr(brain, "resolve_provider", lambda _n=None: P())
    brain.ask("como extraigo tablas de un pdf?", use_tools=True)
    assert "Skills available" in seen["system"] and "pdf-tools" in seen["system"]
    assert any(t.get("function", t).get("name") == "activate_skill" for t in seen["tools"])
    assert seen["activation"]["ok"] and "already active" in seen["again"]["result"]


# ---------- project skills (D13) ----------

def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    shutil.copytree(FIX / "project", path / ".agents" / "skills")
    return path


def test_project_skills_only_for_trusted_repos(env):
    from ops_dashboard.skills import catalog
    repo = _git_repo(env["tmp"] / "repo")
    _index()
    assert catalog.project_skills(repo) == []
    assert "proj-skill" not in [r.key for r in catalog.catalog_for_repo(repo)]
    stored = catalog.set_repo_trust(str(repo), True)
    assert stored == str(repo.resolve())
    assert [r.key for r in catalog.project_skills(repo)] == ["proj-skill"]
    assert "proj-skill" in [r.key for r in catalog.catalog_for_repo(repo)]
    catalog.set_repo_trust(str(repo), False)
    assert catalog.project_skills(repo) == []
    # project skills never enter the global (Brain) catalog
    assert "proj-skill" not in [c["name"] for c in catalog.enabled_catalog()]


def test_trust_requires_existing_git_repo(env):
    from ops_dashboard.skills import catalog
    plain = env["tmp"] / "plain"
    plain.mkdir()
    for bad in (str(plain), "relative/path", "", None, str(env["tmp"] / "missing")):
        with pytest.raises(catalog.SkillError):
            catalog.set_repo_trust(bad, True)


# ---------- export (D1) ----------

def test_export_copies_vault_skill_never_overwrites(env):
    from ops_dashboard.skills import catalog
    _index()
    shutil.rmtree(env["home"] / ".agents" / "skills" / "pdf-tools")  # fixture's shadow copy
    (env["vault"] / "pdf-tools" / "link").symlink_to("/etc/hosts")
    dest = catalog.export_skill("pdf-tools")
    assert dest == env["home"] / ".agents" / "skills" / "pdf-tools"
    assert (dest / "SKILL.md").read_text() == (env["vault"] / "pdf-tools" / "SKILL.md").read_text()
    assert (dest / "scripts" / "extract.py").is_file() and not (dest / "link").exists()
    with pytest.raises(catalog.SkillError) as e:
        catalog.export_skill("pdf-tools")
    assert e.value.code == "exists"
    for bad in ("no-desc", "missing"):
        with pytest.raises(catalog.SkillError):
            catalog.export_skill(bad)
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".")]  # no temp dirs left


# ---------- routes ----------

def test_routes(env):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def req(path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(base + path, data=data, method="POST" if data else "GET",
                                   headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=8) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        st, d = req("/api/learning/skills/reindex", {})
        assert st == 200 and d["external_dirs_enabled"] is False and d["counters"]["indexed"] > 0
        assert "pdf-tools" in [s["key"] for s in d["skills"]]
        st, d = req("/api/learning/skills/settings", {"external_dirs": True})
        assert st == 200 and "agent-only" in [s["key"] for s in d["skills"]]
        assert req("/api/learning/skills/settings", {"external_dirs": "1"})[0] == 400
        assert req("/api/learning/skills/enable", {"key": "pdf-tools", "enabled": False})[0] == 200
        assert req("/api/learning/skills/enable", {"key": "zzz", "enabled": False})[0] == 404
        assert req("/api/learning/skills/export", {"key": "agent-only"})[1]["error"] == "not_vault"
        repo = _git_repo(env["tmp"] / "r2")
        st, d = req("/api/learning/skills/project?repo=" + str(repo))
        assert st == 200 and d["trusted"] is False and d["skills"] == []
        assert req("/api/learning/skills/trust", {"repo_path": str(repo), "trusted": True})[0] == 200
        st, d = req("/api/learning/skills/project?repo=" + str(repo))
        assert d["trusted"] and [s["key"] for s in d["skills"]] == ["proj-skill"]
        st, _ = req("/api/learning/skills", headers={"X-Forwarded-For": "203.0.113.9"})
        assert st in (401, 403)
        st, _ = req("/api/learning/skills/settings", {"external_dirs": True}, headers={"X-Forwarded-For": "1.2.3.4"})
        assert st in (401, 403)
    finally:
        srv.shutdown()
        srv.server_close()


def test_no_remote_catalog_code():
    root = Path(__file__).resolve().parent.parent / "ops_dashboard"
    for f in list((root / "skills").glob("*.py")) + [root / "routes_skills.py"]:
        src = f.read_text()
        for needle in ("httpx", "urllib.request", "requests", "http://", "https://", "socket"):
            assert needle not in src, (f.name, needle)
    assert os.path.exists(root / "skills" / "catalog.py")


def test_trusted_project_skills_reach_code_sessions(env):
    # Trusting a repo must have an effect: its .agents/skills are offered to
    # code sessions on that repo (Codex on #35).
    from ops_dashboard.sessions import runner
    from ops_dashboard.skills import catalog
    repo = _git_repo(env["tmp"] / "repo2")
    _index()
    assert "proj-skill" not in runner._skills_prompt(str(repo))
    assert not catalog.activate_for_repo(str(repo), "proj-skill")["ok"]
    catalog.set_repo_trust(str(repo), True)
    prompt = runner._skills_prompt(str(repo))
    assert "activate_skill" in prompt and "proj-skill" in prompt
    got = catalog.activate_for_repo(str(repo), "proj-skill")
    assert got["ok"] and '<skill_content name="proj-skill">' in got["result"]


def test_an_edited_skill_must_still_validate_under_its_name(env):
    from ops_dashboard.skills import catalog
    repo = _git_repo(env["tmp"] / "repo3")
    catalog.set_repo_trust(str(repo), True)
    skill_md = next((repo / ".agents" / "skills").glob("*/SKILL.md"))
    text = skill_md.read_text()
    skill_md.write_text(text.replace("name: proj-skill", "name: something-else", 1))
    assert not catalog._load("proj-skill", skill_md)["ok"]
