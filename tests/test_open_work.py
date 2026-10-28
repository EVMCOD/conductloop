"""What is still open, and where it was written down.

Open work is written as unchecked boxes under a heading that names the
project. The product was indexing the boxes and losing the two things that
make them useful: **the section they are in** (the author's own
classification) and **the project the section belongs to**.

These tests are about that handover: the item finds its project, keeps
the heading, and lands in the page where somebody will look for it.
"""
from __future__ import annotations

import pytest

from ops_dashboard import projects


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A vault with one note of open work, and two projects to attribute to."""
    from ops_dashboard import db

    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "open.db"))
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    db.DB_PATH = tmp_path / "open.db"  # type: ignore[attr-defined]
    db.init_db()
    monkeypatch.setattr(projects, "PROJECTS", [
        projects.Project(key="globex", name="Globex", aliases=["dw"]),
        projects.Project(key="acme", name="Acme", aliases=[]),
    ])
    return tmp_path


def _note_with_work(env, name="TODO.md", body="", project=None):
    """A note with open boxes, indexed the way the product indexes it."""
    from ops_dashboard import db, db_todos

    root = env / "vault"
    root.mkdir(exist_ok=True)
    # A vault is a folder with .obsidian in it: that is what makes the
    # links of the pages resolve.
    (root / ".obsidian").mkdir(exist_ok=True)
    path = root / name
    path.write_text(body, encoding="utf-8")
    db.upsert_document(source="obsidian", path=str(path), title=name, content=body,
                       metadata={}, mtime=1780000000, size=len(body))
    with db.db() as conn:
        row = conn.execute("SELECT id FROM documents WHERE path = ?", (str(path),)).fetchone()
    doc_id = int(row["id"])
    db_todos.replace_todos("obsidian", [
        {"doc_id": doc_id, "content": "algo abierto", "done": False, "mtime": 1780000000,
         "project_key": project},
    ])
    return path


def test_an_open_item_inherits_the_project_of_its_section(env):
    """The section names the project; the item is in that section."""
    from ops_dashboard import open_work

    _note_with_work(env, body=(
        "## 🤖 Globex — Plataforma y IA (estado real: 23-oct, 13:50)\n"
        "\n"
        "- [ ] Leer el script de demanda cuando haya volumen\n"
        "- [x] Algo ya hecho\n"
    ))
    data = open_work.scan()
    assert "globex" in data
    rows = data["globex"]
    assert len(rows) == 1, "a done box is not open work"
    assert rows[0]["text"].startswith("Leer el script")
    assert rows[0]["section"] == "🤖 Globex — Plataforma y IA (estado real: 23-oct, 13:50)"


def test_the_owner_at_the_end_of_the_line_is_read(env):
    """'- [ ] hacer X — Claude' says whose it is."""
    from ops_dashboard import open_work

    _note_with_work(env, body=(
        "## 🤖 Globex — Plataforma y IA\n"
        "\n"
        "- [ ] Leer el script de demanda — Claude\n"
    ))
    rows = open_work.scan()["globex"]
    assert rows[0]["owner"] == "Claude"
    assert rows[0]["text"] == "Leer el script de demanda"


def test_a_section_that_says_it_blocks_is_flagged(env):
    from ops_dashboard import open_work

    _note_with_work(env, body=(
        "## 🤖 Globex — Decisiones pendientes (bloqueantes)\n"
        "\n"
        "- [ ] Decidir si se ocultan las funciones sin uso\n"
    ))
    rows = open_work.scan()["globex"]
    assert rows[0]["blocking"] is True


def test_the_date_of_the_section_is_understood(env):
    from ops_dashboard import open_work

    _note_with_work(env, body=(
        "## 🤖 Globex — Retención (estado real: 24-oct, 22:45)\n"
        "\n"
        "- [ ] Probar a mano en el navegador\n"
    ))
    rows = open_work.scan()["globex"]
    assert rows[0]["date"] == "2026-10-24"


def test_what_blocks_comes_first(env):
    from ops_dashboard import open_work

    _note_with_work(env, body=(
        "## 🤖 Globex — Retención (estado real: 24-oct)\n"
        "\n"
        "- [ ] Algo normal\n"
        "\n"
        "## 🤖 Globex — Decisiones pendientes (bloqueantes)\n"
        "\n"
        "- [ ] Algo que bloquea\n"
    ))
    rows = open_work.scan()["globex"]
    assert rows[0]["text"] == "Algo que bloquea"
    assert rows[0]["blocking"] is True


def test_the_page_shows_it_and_links_to_the_exact_place(env, tmp_path):
    """The item lands in the page of its project, pointing at the heading."""
    from ops_dashboard import db, vault_pages

    _note_with_work(env, body=(
        "## 🤖 Globex — Plataforma y IA (estado real: 23-oct, 13:50)\n"
        "\n"
        "- [ ] Leer el script de demanda — Claude\n"
    ))
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='globex'")
        conn.commit()

    # The map is written inside the vault, where it belongs.
    export = env / "vault" / "ConductLoop"
    export.mkdir(exist_ok=True)
    vault_pages.export_pages(destinations=[export], data={
        "projects": [{"key": "globex", "name": "Globex", "parent": None, "notes": 1,
                      "ids": [1], "recent": [], "repos": [], "known": True}],
        "ideas": [], "drift": {"missing_from_index": [], "links_to_nothing": []},
        "unlinked": [], "suggestions": [],
        "totals": {"documents": 1, "attributed": 1, "loose": 0},
    })
    text = (export / "pages" / "globex.md").read_text(encoding="utf-8")
    assert "## What remains open" in text
    assert "Leer el script de demanda" in text
    assert "Claude" in text
    # The link points at the heading, and the heading is really there.
    assert "#🤖 Globex — Plataforma y IA (estado real: 23-oct, 13:50)" in text
    note = (env / "vault" / "TODO.md").read_text(encoding="utf-8")
    assert "## 🤖 Globex — Plataforma y IA (estado real: 23-oct, 13:50)" in note


def test_the_waiting_page_puts_the_blocking_work_of_every_project_together(env):
    """The question that does not belong to any single project."""
    from ops_dashboard import db, vault_pages

    _note_with_work(env, body=(
        "## 🤖 Globex — Decisiones pendientes (bloqueantes)\n"
        "\n"
        "- [ ] Decidir si se ocultan las funciones sin uso\n"
    ))
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='globex'")
        conn.commit()

    export = env / "vault" / "ConductLoop"
    export.mkdir(exist_ok=True)
    vault_pages.export_pages(destinations=[export], data={
        "projects": [{"key": "globex", "name": "Globex", "parent": None, "notes": 1,
                      "ids": [1], "recent": [], "repos": [], "known": True}],
        "ideas": [], "drift": {"missing_from_index": [], "links_to_nothing": []},
        "unlinked": [], "suggestions": [],
        "totals": {"documents": 1, "attributed": 1, "loose": 0},
    })
    page = export / "pages" / "Waiting.md"
    assert page.exists()
    text = page.read_text(encoding="utf-8")
    assert "## It blocks" in text
    assert "Decidir si se ocultan las funciones sin uso" in text
    assert "**Globex**" in text
    # And the index points at it.
    index = (export / "pages" / "Index.md").read_text(encoding="utf-8")
    assert "What is waiting for you" in index


def test_the_same_item_written_twice_is_one_item(env):
    """TODO.md repeats its own first half: every pending shows up twice."""
    from ops_dashboard import open_work

    _note_with_work(env, body=(
        "## 🤖 Globex — IA\n"
        "\n"
        "- [ ] Una cosa\n"
        "\n"
        "## 🤖 Globex — IA\n"
        "\n"
        "- [ ] Una cosa\n"
    ))
    rows = open_work.scan()["globex"]
    assert len(rows) == 1


def test_an_item_whose_text_names_the_project_finds_it(env):
    """The section may say nothing: the line itself says which project."""
    from ops_dashboard import open_work

    _note_with_work(env, body=(
        "## Pendiente\n"
        "\n"
        "- [ ] Globex: probar el webhook en producción\n"
    ))
    data = open_work.scan()
    assert "globex" in data, "the line names the project"
    assert len(data["globex"]) == 1


def test_the_agent_gets_the_waiting_page_in_its_own_folder(env, tmp_path):
    """The agent's first read is a page, not a 39 KB map."""
    from ops_dashboard import db, notes_structure

    _note_with_work(env, body=(
        "## 🤖 Globex — Decisiones pendientes (bloqueantes)\n"
        "\n"
        "- [ ] Decidir si se ocultan las funciones sin uso\n"
    ))
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='globex'")
        conn.commit()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    export = env / "vault" / "ConductLoop"
    export.mkdir(exist_ok=True)
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='globex'")
        conn.commit()

    import ops_dashboard.library as lib

    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        notes_structure.write_agent_map(filename="PROJECTS.md", pointer=True, also_to=[export])
    finally:
        lib.memory_workspace = original
    waiting = workspace / "WAITING.md"
    assert waiting.exists(), "the agent must find it without touching the vault"
    text = waiting.read_text(encoding="utf-8")
    assert "Decidir si se ocultan las funciones sin uso" in text
    # The paths are whole, because a wikilink means nothing outside Obsidian.
    assert "TODO.md" in text

    # And the pointer sends the agent there first.
    pointer = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "WAITING.md" in pointer
    assert pointer.index("WAITING.md") < pointer.index("PROJECTS.md")


def test_the_notes_you_never_wrote_are_listed(env, tmp_path):
    """A link to a note that does not exist is an idea you had enough of."""
    from ops_dashboard import db, notes_structure, vault_pages

    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    note = vault / "ideas.md"
    note.write_text("# Ideas\n\n[[bing_indexnow_setup]]\n[[paper-2019]]\n[[data.csv]]\n",
                    encoding="utf-8")
    db.upsert_document(source="obsidian", path=str(note), title="Ideas", content="x",
                       metadata={}, mtime=1780000000, size=1)

    drift = notes_structure.index_drift(roots=[vault])
    wanted = vault_pages.wanted_notes({"drift": drift})
    stems = [row["stem"] for row in wanted]
    assert "bing_indexnow_setup" in stems
    # A citation and a file are not notes to write.
    assert not any(s.endswith("2019") for s in stems)
    assert not any(s.endswith(".csv") for s in stems)
    # And it says where the link came from.
    row = next(r for r in wanted if r["stem"] == "bing_indexnow_setup")
    assert "ideas" in row["from"]


def test_a_blocking_heading_with_an_inline_flag_does_not_break_the_scan(env, monkeypatch):
    import json

    from ops_dashboard import open_work, vault_conventions, vault_pages

    cfg = env / "config.json"
    cfg.write_text(json.dumps({"vault_conventions": {
        "blocking_headings": ["(?i)esperando a legal", "(unclosed"]}}), encoding="utf-8")
    monkeypatch.setenv("OPS_CONFIG_PATH", str(cfg))
    vault_conventions.reset_cache()
    _note_with_work(env, body=(
        "## Globex — Esperando A Legal\n"
        "\n"
        "- [ ] Firmar el contrato\n"
    ))
    rows = open_work.scan()["globex"]          # no exception
    assert rows[0]["blocking"] is True
    assert vault_pages.open_work()["globex"]    # and the wrapper does not hide everything
    vault_conventions.reset_cache()
