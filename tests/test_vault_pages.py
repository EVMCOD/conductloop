"""The map as pages: one per project, linked the way a vault resolves them.

A flat ``PROJECTS.md`` is invisible to Obsidian: no links, no graph, no
backlinks, and an agent that has to read all 39 KB to learn about one
project. These pages are the same map in the shape a vault can follow —
so the test that matters is not "the file exists" but "every link in it
points at a note that is really there".
"""
from __future__ import annotations

import re

import pytest

WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """A vault with two projects, a loose note, and a place for the map."""
    from ops_dashboard import db, library, notes_structure

    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "projects").mkdir()
    (root / "research").mkdir()
    export = root / "ConductLoop"
    export.mkdir()

    (root / "projects" / "acme.md").write_text(
        "---\ntype: project\n---\n# Acme\n", encoding="utf-8")
    (root / "projects" / "globex.md").write_text(
        "---\ntype: project\n---\n# Globex\n", encoding="utf-8")
    (root / "research" / "growth.md").write_text("# Growth\n", encoding="utf-8")

    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "pages.db"))
    db.DB_PATH = tmp_path / "pages.db"  # type: ignore[attr-defined]
    db.init_db()

    for path, title, key in [
        (root / "projects" / "acme.md", "Acme", "acme"),
        (root / "projects" / "globex.md", "Globex", "globex"),
        (root / "research" / "growth.md", "Growth", ""),
    ]:
        db.upsert_document(source="obsidian", path=str(path), title=title,
                           content="x", metadata={}, mtime=1780000000, size=1)
        if key:
            with db.db() as conn:
                conn.execute("UPDATE documents SET project_key=? WHERE path=?", (key, str(path)))
                conn.commit()

    monkeypatch.setattr(notes_structure, "_vault_roots", lambda: [root])
    monkeypatch.setattr(library, "map_export_destinations", lambda: [export])

    data = {
        "projects": [
            {"key": "acme", "name": "Acme", "parent": None, "notes": 1,
             "ids": [1], "recent": [], "repos": ["/tmp/acme"], "known": True},
            {"key": "globex", "name": "Globex", "parent": None, "notes": 1,
             "ids": [2], "recent": [], "repos": [], "known": True},
        ],
        "ideas": [],
        "drift": {"missing_from_index": ["acme"], "links_to_nothing": []},
        "unlinked": [],
        "suggestions": [],
        "totals": {"documents": 3, "attributed": 2, "loose": 1},
    }
    return {"root": root, "export": export, "data": data}


def _export(vault):
    from ops_dashboard import vault_pages

    out = vault_pages.export_pages(destinations=[vault["export"]], data=vault["data"])
    return vault_pages, out


def test_the_map_becomes_one_page_per_project(vault):
    """An index, a page per project, and a page per area of loose notes."""
    _export(vault)
    pages = vault["export"] / "pages"
    assert (pages / "Index.md").exists()
    assert (pages / "acme.md").exists()
    assert (pages / "globex.md").exists()
    assert (pages / "areas" / "research.md").exists()


def test_every_link_points_at_a_note_that_is_really_there(vault):
    """The whole point of the pages: the links resolve.

    The quoted text of a handoff is the user's own and is left as it is —
    a dead link in his note stays a dead link in the quote. What the
    product writes itself has to resolve.
    """
    _export(vault)
    pages = vault["export"] / "pages"
    checked = 0
    for page in sorted(pages.rglob("*.md")):
        for line in page.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(">"):
                continue          # quoted text: his words, his links
            for target in WIKILINK.findall(line):
                checked += 1
                note, _, anchor = target.partition("#")
                path = vault["root"] / f"{note}.md"
                assert path.exists(), f"{target} in {page.name}"
                if anchor:
                    # An anchor has to be a heading that is really there.
                    assert f"## {anchor}" in path.read_text(encoding="utf-8"), \
                        f"the heading '{anchor}' is not in {path.name}"
    assert checked >= 4


def test_the_notes_of_a_project_are_linked_from_its_page(vault):
    _export(vault)
    text = (vault["export"] / "pages" / "acme.md").read_text(encoding="utf-8")
    assert "[[projects/acme|Acme]]" in text
    assert "/tmp/acme" in text


def test_a_note_outside_the_vault_is_a_path_and_not_a_link(vault):
    """A link to nowhere is worse than a path that works everywhere."""
    from ops_dashboard import db

    outside = vault["root"].parent / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    db.upsert_document(source="obsidian", path=str(outside), title="Outside",
                       content="x", metadata={}, mtime=1780000000, size=1)
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='acme' WHERE path=?", (str(outside),))
        conn.commit()

    _export(vault)
    text = (vault["export"] / "pages" / "acme.md").read_text(encoding="utf-8")
    assert "Outside" in text
    assert f"[[{outside}|" not in text
    assert str(outside) in text


def test_the_pages_are_ours_and_say_so(vault):
    _export(vault)
    for page in (vault["export"] / "pages").rglob("*.md"):
        assert vault_pages_marker(page.read_text(encoding="utf-8"))


def vault_pages_marker(text: str) -> bool:
    from ops_dashboard.vault_pages import PAGE_MARKER

    return PAGE_MARKER in text


def test_the_orphans_of_a_project_land_on_its_page(vault):
    """The vault calls the project one thing and the registry another: the
    notes that stopped being linked are the bridge between both names."""
    from ops_dashboard import db

    folder = vault["root"] / "projects" / "acme"
    folder.mkdir()
    orphan = folder / "contract.md"
    orphan.write_text("# Contract\n", encoding="utf-8")
    db.upsert_document(source="obsidian", path=str(orphan), title="Contract",
                       content="x", metadata={}, mtime=1780000000, size=1)
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='acme' WHERE path=?", (str(orphan),))
        conn.commit()

    vault["data"]["unlinked"] = [{
        "project": "acme", "title": "Acme", "path": str(vault["root"] / "projects" / "acme.md"),
        "folder": str(folder), "orphans": ["contract"], "count": 1,
    }]
    _export(vault)
    text = (vault["export"] / "pages" / "acme.md").read_text(encoding="utf-8")
    assert "## Notes this page does not link" in text
    assert "contract" in text


def test_only_our_own_pages_are_removed(vault):
    """The folder is inside the vault of the user: the rest is theirs."""
    from ops_dashboard.vault_pages import PAGE_MARKER

    _export(vault)
    pages = vault["export"] / "pages"
    stale = pages / "gone.md"
    stale.write_text(f"---\ntype: conductloop-map\n---\n{PAGE_MARKER}\n# Gone\n",
                     encoding="utf-8")
    theirs = pages / "my-notes.md"
    theirs.write_text("# Mine\n", encoding="utf-8")

    vault["data"]["projects"] = [
        p for p in vault["data"]["projects"] if p["key"] != "globex"]
    _export(vault)

    assert not stale.exists()
    assert not (pages / "globex.md").exists()
    assert theirs.exists()
    assert theirs.read_text(encoding="utf-8") == "# Mine\n"


def test_writing_it_twice_does_not_touch_the_vault(vault):
    """A vault that changes every hour is a vault nobody reads."""
    _export(vault)
    page = vault["export"] / "pages" / "acme.md"
    before = page.read_text(encoding="utf-8"), page.stat().st_mtime_ns

    _vault_pages, out = _export(vault)
    assert out["written"] == 0
    assert before == (page.read_text(encoding="utf-8"), page.stat().st_mtime_ns)


def test_the_product_does_not_index_its_own_pages(vault):
    """The map names every project: indexing it would make the map part of
    the map, and every page of it another synthetic note."""
    from ops_dashboard import db
    from ops_dashboard.indexers import obsidian

    _export(vault)
    obsidian._run_one(vault["root"])

    with db.db() as conn:
        rows = conn.execute("SELECT path FROM documents").fetchall()
    paths = [str(r["path"]) for r in rows]
    assert paths, "the notes of the user must still be indexed"
    assert not [p for p in paths if "/pages/" in p]


# ── where each project stands ──────────────────────────────────────────────

def _handoff(root, name, project, date, ask):
    """A note that says 'here is where I left off'.

    Returns the path and the body the index would keep (no frontmatter).
    """
    folder = root / "handoff"
    folder.mkdir(exist_ok=True)
    body = ("\n"
            f"# {name}\n"
            "\n"
            "> [!success] The ask\n"
            f"> {ask}\n"
            "\n"
            "## What I found\n"
            "Detail that does not fit in the ask.\n")
    (folder / name).write_text(
        "---\n"
        f"title: {name}\n"
        "type: handoff\n"
        f"date: {date}\n"
        f"project: {project}\n"
        "---\n"
        + body,
        encoding="utf-8")
    return folder / name, body


def test_the_project_page_says_where_the_work_stands(vault):
    """The point of departure: what the last person wrote down."""
    from ops_dashboard import db

    path, body = _handoff(vault["root"], "2026-10-23-initech-to-globex", "acme", "2026-10-23",
                          "que todo pase al plan maximo")
    db.upsert_document(source="obsidian", path=str(path), title="Initech to Globex",
                       content=body, metadata={"frontmatter": {"type": "handoff",
                                                              "date": "2026-10-23",
                                                              "title": "Initech → Globex"}},
                       mtime=1780000000, size=1)
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='acme' WHERE path=?", (str(path),))
        conn.commit()

    _export(vault)
    text = (vault["export"] / "pages" / "acme.md").read_text(encoding="utf-8")
    assert "## Where this stands" in text
    assert "2026-10-23" in text
    assert "que todo pase al plan maximo" in text
    assert "[[handoff/2026-10-23-initech-to-globex|Initech → Globex]]" in text


def test_the_index_says_where_to_start(vault):
    """One line per project that somebody left a handoff about."""
    from ops_dashboard import db

    path, body = _handoff(vault["root"], "2026-10-23-h", "acme", "2026-10-23", "the ask")
    db.upsert_document(source="obsidian", path=str(path), title="H",
                       content=body, metadata={"frontmatter": {"type": "handoff",
                                                             "date": "2026-10-23"}},
                       mtime=1780000000, size=1)
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='acme' WHERE path=?", (str(path),))
        conn.commit()

    _export(vault)
    text = (vault["export"] / "pages" / "Index.md").read_text(encoding="utf-8")
    assert "## Where to start" in text
    assert "acme" in text


def test_a_project_with_no_handoff_says_nothing(vault):
    """A section that is always there and always empty is noise."""
    _export(vault)
    text = (vault["export"] / "pages" / "acme.md").read_text(encoding="utf-8")
    assert "## Where this stands" not in text


def test_the_ask_is_the_blockquote_and_not_the_label(vault):
    """The callout label is a label, not the ask."""
    from ops_dashboard.vault_pages import _ask

    content = ("\n# Title\n\n> [!success] The ask\n"
               "> what has to be done\n> and why\n\n## Detail\n")
    assert _ask(content) == "what has to be done and why"
    # And a note without a blockquote has no ask.
    assert _ask("# Title\n\nPlain text\n") == ""
