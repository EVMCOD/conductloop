"""Gap 3 step 1: import a local agentskills.io folder into the vault.

Strict validation, existing storage, no network, scripts never run.
Import by URL is out of scope (step 2) and must not appear as an implemented route.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from ops_dashboard.skills.catalog import SkillError


def _write_skill(folder: Path, name: str, description: str = "What it does and when to use it.",
                 body: str = "# Hello\n\nDo the thing.\n", extra_fm: str = "",
                 helpers: dict[str, bytes] | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    fm = f"name: {name}\ndescription: {description}\n{extra_fm}"
    (folder / "SKILL.md").write_text(f"---\n{fm}---\n\n{body}", encoding="utf-8")
    for rel, data in (helpers or {}).items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return folder


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "s.db")
    db.init_db()
    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    return {"home": home, "vault": vault, "tmp": tmp_path}


def _import(path):
    from ops_dashboard.skills.import_folder import import_from_folder
    return import_from_folder(path)


def test_imports_a_valid_folder_into_the_vault(env):
    src = _write_skill(
        env["tmp"] / "pdf-summarise",
        "pdf-summarise",
        helpers={"references/notes.md": b"# notes\n", "assets/blank.txt": b"x"},
    )
    result = _import(str(src))
    dest = Path(result["path"])
    assert result["ok"] and result["key"] == "pdf-summarise"
    assert dest == env["vault"] / "pdf-summarise"
    assert dest.is_dir() and not dest.is_symlink()
    assert (dest / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: pdf-summarise")
    assert (dest / "references" / "notes.md").read_text(encoding="utf-8") == "# notes\n"
    assert (dest / "assets" / "blank.txt").read_bytes() == b"x"
    assert "SKILL.md" in result["files"] and "references/notes.md" in result["files"]


def test_imported_skill_is_indexed_and_activable(env):
    from ops_dashboard.indexers import skills as idx
    from ops_dashboard.skills import catalog
    src = _write_skill(env["tmp"] / "pdf-summarise", "pdf-summarise")
    _import(str(src))
    counters = idx.run()
    assert counters["indexed"] >= 1
    keys = [s["key"] for s in catalog.list_indexed()]
    assert "pdf-summarise" in keys
    got = catalog.activate("pdf-summarise")
    assert got["ok"] and "Do the thing." in got["result"]


def test_rejects_missing_and_invalid_frontmatter(env, tmp_path):
    missing = tmp_path / "no-skill"
    missing.mkdir()
    with pytest.raises(SkillError) as ei:
        _import(str(missing))
    assert ei.value.code == "missing_skill_md"

    no_fm = tmp_path / "plain-notes"
    no_fm.mkdir()
    (no_fm / "SKILL.md").write_text("# just markdown\n", encoding="utf-8")
    with pytest.raises(SkillError) as ei:
        _import(str(no_fm))
    assert ei.value.code == "no_frontmatter"

    bad = tmp_path / "bad-yaml"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: [unterminated\n---\n\nbody\n", encoding="utf-8")
    with pytest.raises(SkillError) as ei:
        _import(str(bad))
    assert ei.value.code == "yaml_unparseable"


def test_rejects_name_format_and_directory_mismatch(env, tmp_path):
    upper = _write_skill(tmp_path / "PDF-Tools", "PDF-Tools")
    with pytest.raises(SkillError) as ei:
        _import(str(upper))
    assert ei.value.code == "name_format"

    mismatch = _write_skill(tmp_path / "other-folder", "pdf-tools")
    with pytest.raises(SkillError) as ei:
        _import(str(mismatch))
    assert ei.value.code == "name_dir_mismatch"

    missing = tmp_path / "no-name"
    missing.mkdir()
    (missing / "SKILL.md").write_text("---\ndescription: only a description\n---\n\nbody\n", encoding="utf-8")
    with pytest.raises(SkillError) as ei:
        _import(str(missing))
    assert ei.value.code == "name_missing"


def test_rejects_description_length(env, tmp_path):
    empty = tmp_path / "no-desc"
    empty.mkdir()
    (empty / "SKILL.md").write_text("---\nname: no-desc\n---\n\nbody\n", encoding="utf-8")
    with pytest.raises(SkillError) as ei:
        _import(str(empty))
    assert ei.value.code == "description_missing"

    long_desc = "x" * 1025
    too_long = _write_skill(tmp_path / "long-desc", "long-desc", description=long_desc)
    with pytest.raises(SkillError) as ei:
        _import(str(too_long))
    assert ei.value.code == "description_too_long"
    assert "1024" in ei.value.message


def test_rejects_size_limits(env, tmp_path, monkeypatch):
    from ops_dashboard.skills import import_folder as imp
    big_md = tmp_path / "too-big"
    big_md.mkdir()
    (big_md / "SKILL.md").write_text("---\nname: too-big\ndescription: d\n---\n" + ("x" * (256 * 1024 + 1)))
    with pytest.raises(SkillError) as ei:
        _import(str(big_md))
    assert ei.value.code == "too_large"

    many = _write_skill(tmp_path / "many-files", "many-files")
    monkeypatch.setattr(imp, "MAX_IMPORT_FILES", 2)
    (many / "a.txt").write_text("a")
    (many / "b.txt").write_text("b")
    with pytest.raises(SkillError) as ei:
        _import(str(many))
    assert ei.value.code == "too_large"


def _symlink(src: Path, dest: Path, *, target_is_directory: bool = False) -> None:
    try:
        dest.symlink_to(src, target_is_directory=target_is_directory)
    except OSError:
        pytest.skip("symlinks are not allowed on this system")


def test_rejects_symlinks_and_path_traversal(env, tmp_path):
    src = _write_skill(tmp_path / "has-link", "has-link")
    outside = tmp_path / "outside_target"
    outside.mkdir()
    _symlink(outside, src / "outside", target_is_directory=True)
    with pytest.raises(SkillError) as ei:
        _import(str(src))
    assert ei.value.code == "symlink"
    assert not (env["vault"] / "has-link").exists()

    linked_md = tmp_path / "link-md"
    linked_md.mkdir()
    real = tmp_path / "other.md"
    real.write_text('---\nname: link-md\ndescription: d\n---\n\nbody\n')
    _symlink(real, linked_md / "SKILL.md")
    with pytest.raises(SkillError) as ei:
        _import(str(linked_md))
    assert ei.value.code == "symlink"

    linked_dir = tmp_path / "link-dir"
    real_dir = _write_skill(tmp_path / "real-skill", "real-skill")
    _symlink(real_dir, linked_dir, target_is_directory=True)
    with pytest.raises(SkillError) as ei:
        _import(str(linked_dir))
    assert ei.value.code == "symlink"

    rel = tmp_path / "not-absolute" / "some-skill"
    rel.mkdir(parents=True)
    with pytest.raises(SkillError) as ei:
        _import("not-absolute/some-skill")
    assert ei.value.code == "bad_path"


def test_dotfiles_are_skipped_and_existing_name_is_left_alone(env, tmp_path):
    src = _write_skill(
        tmp_path / "keep-dots",
        "keep-dots",
        helpers={".secret": b"nope", "ok.txt": b"yes"},
    )
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text("x")
    result = _import(str(src))
    dest = Path(result["path"])
    assert not (dest / ".secret").exists()
    assert not (dest / ".git").exists()
    assert (dest / "ok.txt").read_bytes() == b"yes"

    with pytest.raises(SkillError) as ei:
        _import(str(src))
    assert ei.value.code == "exists" and ei.value.status == 409
    # original copy still intact
    assert (dest / "SKILL.md").is_file()


def test_scripts_are_copied_but_never_run(env, tmp_path, monkeypatch):
    src = _write_skill(tmp_path / "with-script", "with-script")
    script = src / "scripts" / "hello.sh"
    script.parent.mkdir()
    marker = tmp_path / "was-run"
    script.write_text(f"#!/bin/sh\necho ran > '{marker}'\n")

    ran = []

    def boom(*_a, **_k):
        ran.append("called")
        raise AssertionError("a script must not be started during import")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "call", boom)
    monkeypatch.setattr(os, "system", boom)
    if hasattr(os, "execv"):
        monkeypatch.setattr(os, "execv", boom)

    result = _import(str(src))
    dest_script = Path(result["path"]) / "scripts" / "hello.sh"
    assert dest_script.is_file()
    assert dest_script.read_text() == script.read_text()
    assert not marker.exists()
    assert ran == []


@pytest.mark.posix_perms
def test_imported_scripts_lose_the_execute_bit(env, tmp_path):
    src = _write_skill(tmp_path / "exec-script", "exec-script")
    script = src / "scripts" / "hello.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    dest_script = Path(_import(str(src))["path"]) / "scripts" / "hello.sh"
    assert dest_script.is_file()
    mode = dest_script.stat().st_mode
    assert not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def test_source_does_not_start_processes_or_talk_to_the_network():
    src = Path(__file__).resolve().parent.parent / "ops_dashboard" / "skills" / "import_folder.py"
    text = src.read_text()
    for needle in ("subprocess", "os.system", "Popen", "os.exec", "httpx", "urllib.request",
                   "requests", "http://", "https://", "socket", "urlopen"):
        assert needle not in text, needle


def test_no_import_by_url_route_yet():
    routes = (Path(__file__).resolve().parent.parent / "ops_dashboard" / "routes_skills.py").read_text()
    assert "/api/learning/skills/import" in routes
    assert "import-url" not in routes and "import_url" not in routes
    assert "http://" not in routes and "https://" not in routes


def test_route_imports_and_is_local_only(env):
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
        r = urllib.request.Request(
            base + path, data=data, method="POST" if data else "GET",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(r, timeout=8) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    src = _write_skill(env["tmp"] / "route-skill", "route-skill")
    try:
        st, d = req("/api/learning/skills/import", {"path": str(src)})
        assert st == 200 and d["ok"] is True and d["key"] == "route-skill"
        assert "route-skill" in [s["key"] for s in d["skills"]]
        assert (env["vault"] / "route-skill" / "SKILL.md").is_file()

        st, d = req("/api/learning/skills/import", {"path": str(src)})
        assert st == 409 and d["error"] == "exists"

        st, d = req("/api/learning/skills/import", {"path": str(env["tmp"] / "missing-folder")})
        assert st == 404 and d["error"] == "not_found"

        st, d = req("/api/learning/skills/import", {})
        assert st == 400 and d["error"] == "bad_path"

        st, _ = req("/api/learning/skills/import", {"path": str(src)},
                    headers={"X-Forwarded-For": "203.0.113.9"})
        assert st in (401, 403)
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_wires_the_import_entry():
    web = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
    html = (web / "learning.html").read_text()
    js = (web / "learning.js").read_text()
    assert 'id="skills-import"' in html and 'id="skills-import-path"' in html
    assert "/api/learning/skills/import" in js
    assert "/api/learning/skills/import-url" in js
    assert "Importar por URL vendrá después" not in js
    assert "Importing by URL comes later" not in js
