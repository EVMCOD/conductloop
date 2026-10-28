"""The conventions of the vault are configuration, not code.

Handoffs, the "ask" of a handoff, who an open item belongs to, which headings
block, the language of the daily note, the titles of the index sections:
all of them come from the ``vault_conventions`` object of ``config.json``,
with neutral defaults. These tests pin the defaults, every key, and the
promise that a bad value never breaks indexing.
"""
from __future__ import annotations

import json

import pytest

from ops_dashboard import vault_conventions as vc


@pytest.fixture()
def config(tmp_path, monkeypatch):
    """Write ``vault_conventions`` into a throwaway config.json."""
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("OPS_VAULT_LANGUAGE", raising=False)
    monkeypatch.delenv("OPS_DAILY_DIR", raising=False)
    monkeypatch.delenv("OPS_OWNER_NAME", raising=False)
    vc.reset_cache()

    def write(obj):
        (tmp_path / "config.json").write_text(
            json.dumps({"vault_conventions": obj} if obj is not None else {}), encoding="utf-8")
        vc.reset_cache()

    yield write
    vc.reset_cache()


# ── defaults ──────────────────────────────────────────────────────────────

def test_the_defaults_are_neutral(config):
    config(None)
    conv = vc.get()
    assert conv.language == "en"
    assert conv.handoff_types == ("handoff",)
    assert conv.handoff_folders == ("handoff", "handoffs")
    assert conv.ask_headings == ()
    # Nobody's own name is a default.
    assert set(conv.all_owners()) == {"Claude", "Codex", "Cursor", "ChatGPT"}


def test_a_missing_or_broken_config_gives_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "nope.json"))
    vc.reset_cache()
    assert vc.get() == vc.parse(None)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "bad.json"))
    vc.reset_cache()
    assert vc.get() == vc.parse(None)


def test_values_of_the_wrong_type_fall_back_without_raising():
    conv = vc.parse({"language": "fr", "handoff_types": 3, "owners": [1, 2],
                     "unplaced_section": ["x"], "counts_template": "no fields here",
                     "blocking_headings": "(unclosed"})
    assert conv.language == "en"
    assert conv.handoff_types == ("handoff",)
    assert conv.owners == ()
    assert conv.unplaced_section == vc.DEFAULT_UNPLACED_SECTION
    assert conv.counts_template == ""
    # An invalid regex is skipped; the built-in pattern still works.
    assert conv.blocking_re("blocking").search("## Blocking things")
    assert not vc.parse("nonsense").owners
    assert vc.parse(["a list"]).language == "en"


def test_the_config_is_reread_when_it_changes(config):
    config({"language": "es"})
    assert vc.get().language == "es"
    config({"language": "en", "owners": ["Ana"]})
    assert vc.get().language == "en"
    assert "Ana" in vc.get().all_owners()


# ── handoffs and the ask ──────────────────────────────────────────────────

def test_handoffs_are_found_by_type_or_folder_and_both_are_configurable(config):
    from ops_dashboard.vault_pages import _is_handoff

    note_a = {"path": "/v/notes/a.md", "frontmatter": {"type": "handoff"}}
    note_b = {"path": "/v/handoffs/b.md", "frontmatter": {}}
    note_c = {"path": "/v/relevo/c.md", "frontmatter": {"type": "relevo"}}
    config(None)
    assert _is_handoff(note_a) and _is_handoff(note_b) and not _is_handoff(note_c)
    config({"handoff_types": ["relevo"], "handoff_folders": ["relevo"]})
    assert _is_handoff(note_c)
    assert not _is_handoff(note_a) and not _is_handoff(note_b)


def test_the_ask_is_the_first_blockquote_by_default(config):
    from ops_dashboard.vault_pages import _ask

    config(None)
    note = "# T\n\n> what was asked\n\n## Ask\nnot this one\n"
    assert _ask(note) == "what was asked"


def test_the_ask_can_come_from_a_heading_instead(config):
    from ops_dashboard.vault_pages import _ask

    note = "# T\n\n> a quote that is not the ask\n\n## 📌 Encargo\n\nlo que se pidió,\nen dos líneas\n\n## Más\nx\n"
    config({"ask_headings": ["Encargo", "Brief"]})
    assert _ask(note) == "lo que se pidió, en dos líneas"
    # No such heading: the blockquote still works.
    assert _ask("# T\n\n> quoted\n") == "quoted"
    # Not configured: the heading is not looked at.
    config(None)
    assert _ask(note) == "a quote that is not the ask"


# ── open work: owners and blocking headings ───────────────────────────────

@pytest.fixture()
def open_env(tmp_path, monkeypatch, config):
    from ops_dashboard import db, db_todos, projects

    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "open.db"))
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    db.DB_PATH = tmp_path / "open.db"  # type: ignore[attr-defined]
    db.init_db()
    monkeypatch.setattr(projects, "PROJECTS", [projects.Project(key="acme", name="Acme", aliases=[])])

    def add(body):
        path = tmp_path / "vault" / "TODO.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(body, encoding="utf-8")
        db.upsert_document(source="obsidian", path=str(path), title="TODO", content=body,
                           metadata={}, mtime=1780000000, size=len(body))
        with db.db() as conn:
            doc_id = int(conn.execute("SELECT id FROM documents WHERE path = ?",
                                      (str(path),)).fetchone()["id"])
        db_todos.replace_todos("obsidian", [
            {"doc_id": doc_id, "content": "x", "done": False, "mtime": 1780000000, "project_key": None}])

    return add


def test_owners_come_from_the_config_and_the_owner_name(open_env, config, monkeypatch):
    from ops_dashboard import open_work

    open_env("## Acme\n\n- [ ] ship it — Ana\n- [ ] review it — Claude\n- [ ] plan it — Bo\n")
    config(None)
    rows = {r["text"]: r["owner"] for r in open_work.scan()["acme"]}
    # Nobody's name is built in: "Ana" is only part of the text until configured.
    assert rows["review it"] == "Claude"
    assert "ship it — Ana" in rows
    config({"owners": ["Ana"]})
    rows = {r["text"]: r["owner"] for r in open_work.scan()["acme"]}
    assert rows["ship it"] == "Ana"
    config(None)
    monkeypatch.setenv("OPS_OWNER_NAME", "Bo")
    rows = {r["text"]: r["owner"] for r in open_work.scan()["acme"]}
    assert rows["plan it"] == "Bo"


def test_blocking_headings_can_be_extended(open_env, config):
    from ops_dashboard import open_work

    open_env("## Acme — Frenado\n\n- [ ] one\n\n## Acme — Blocking\n\n- [ ] two\n")
    config(None)
    flags = {r["text"]: r["blocking"] for r in open_work.scan()["acme"]}
    assert flags == {"one": False, "two": True}
    config({"blocking_headings": ["frenad"]})
    flags = {r["text"]: r["blocking"] for r in open_work.scan()["acme"]}
    assert flags == {"one": True, "two": True}


def test_english_month_names_in_a_heading_are_dates(open_env):
    from ops_dashboard import open_work

    assert open_work._section_date("Status 24-oct", 2026) == "2026-10-24"
    assert open_work._section_date("Status 24-Dec", 2026) == "2026-12-24"
    assert open_work._section_date("Status 3-aug", 2026) == "2026-08-03"


# ── the daily note ────────────────────────────────────────────────────────

def _summary():
    return {"notes": [{"title": "Acme", "path": "/v/acme.md", "project": "acme", "source": "x"}],
            "projects": [("acme", 2)], "ideas": [{"title": "Topic", "notes": 3}],
            "handoffs": {"acme": {"ask": "do the thing"}}}


def test_the_daily_note_is_english_by_default(config):
    from ops_dashboard import daily

    config(None)
    text = "\n".join(daily.render_vault(_summary()))
    assert "## 🗺️ The vault" in text
    assert "Projects that moved" in text and "2 note(s)" in text
    assert "> do the thing" in text
    assert "El vault" not in text


def test_the_daily_note_can_stay_spanish(config):
    from ops_dashboard import daily

    config({"language": "es"})
    text = "\n".join(daily.render_vault(_summary()))
    assert "## 🗺️ El vault" in text
    assert "Proyectos que se movieron" in text and "2 nota(s)" in text
    assert "Temas que se repiten" in text and "3 notas" in text


def test_the_language_can_come_from_the_environment(config, monkeypatch):
    config({"language": "en"})
    monkeypatch.setenv("OPS_VAULT_LANGUAGE", "es")
    vc.reset_cache()
    assert vc.get().language == "es"


def test_the_daily_folder(config, tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "myvault"))
    monkeypatch.setattr(vc, "_LEGACY_DAILY", tmp_path / "does-not-exist")
    config(None)
    assert vc.daily_dir() == tmp_path / "myvault" / "daily"
    config({"daily_dir": str(tmp_path / "journal")})
    assert vc.daily_dir() == tmp_path / "journal"
    monkeypatch.setenv("OPS_DAILY_DIR", str(tmp_path / "env-journal"))
    assert vc.daily_dir() == tmp_path / "env-journal"


def test_the_old_daily_folder_is_kept_when_it_exists(config, tmp_path, monkeypatch):
    legacy = tmp_path / "old" / "daily"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(vc, "_LEGACY_DAILY", legacy)
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "myvault"))
    config(None)
    assert vc.daily_dir() == legacy


# ── the index sections and the counts line ────────────────────────────────

def test_the_counts_line_default_is_what_existing_indexes_have(config):
    config(None)
    conv = vc.get()
    line = conv.counts_line(total=42, active=33, ideas=9)
    assert line == "42 proyectos (33 notas activas + 9 ideas)"
    assert conv.counts_re().search(line)


def test_a_counts_template_builds_its_own_pattern(config):
    config({"counts_template": "{total} projects ({active} active + {ideas} ideas)"})
    conv = vc.get()
    assert conv.counts_line(total=5, active=3, ideas=2) == "5 projects (3 active + 2 ideas)"
    found = conv.counts_re().search("intro. 42 projects (33 active + 9 ideas), flat. outro")
    assert found and found.group(0) == "42 projects (33 active + 9 ideas)"
    assert not conv.counts_re().search("42 proyectos (33 notas activas + 9 ideas)")


@pytest.fixture()
def mini_vault(tmp_path, monkeypatch):
    import ops_dashboard.library as lib
    from ops_dashboard import db

    root = tmp_path / "vault"
    (root / "projects").mkdir(parents=True)
    (root / "projects" / "_index.md").write_text(
        "---\ntitle: Projects\ntype: hub\n---\n\n# Projects\n\n"
        "7 projects (1 active + 0 ideas), flat.\n\n## Active\n- [[alpha]] — first\n",
        encoding="utf-8")
    for name in ("alpha", "gamma"):
        (root / "projects" / f"{name}.md").write_text(
            f"---\ntitle: {name.title()}\ntype: project\nstatus: active\n---\n\n# {name.title()}\n",
            encoding="utf-8")
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "mini.db"))
    db.DB_PATH = tmp_path / "mini.db"  # type: ignore[attr-defined]
    db.init_db()
    monkeypatch.setattr(lib, "index_folders", lambda: [root])
    monkeypatch.setattr(lib, "memory_workspace", lambda: tmp_path / "workspace")
    return [root]


def test_the_sections_and_counts_of_the_index_follow_the_config(mini_vault, config):
    from ops_dashboard import notes_structure

    config({"unplaced_section": "📥 Unsorted", "ideas_section": "💡 Ideas to try",
            "counts_template": "{total} projects ({active} active + {ideas} ideas)"})
    proposal = notes_structure.propose_index_lines(roots=mini_vault)
    assert proposal["unplaced_section"] == "📥 Unsorted"
    assert proposal["ideas_section"] == "💡 Ideas to try"
    result = notes_structure.apply_index_lines(["gamma"], dry_run=True, roots=mini_vault)
    assert "2 projects (2 active + 0 ideas)" in result["preview"]
    assert "7 projects" not in result["preview"]


def test_the_default_sections_do_not_change(mini_vault, config):
    from ops_dashboard import notes_structure

    config(None)
    proposal = notes_structure.propose_index_lines(roots=mini_vault)
    assert proposal["unplaced_section"] == "📥 Sin clasificar"
    assert proposal["ideas_section"] == "💡 Ideas"
