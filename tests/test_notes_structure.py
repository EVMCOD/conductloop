"""The map of your work: notes that say which project they belong to.

The point of this is continuity. When you change agents, the notes are
what stays, so the notes have to carry the structure: which project each
one belongs to, and which ideas belong to nobody yet.
"""
from __future__ import annotations

import json

import pytest

from ops_dashboard import db, notes_structure, projects


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "notes.db"))
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    db.DB_PATH = tmp_path / "notes.db"  # type: ignore[attr-defined]
    db.init_db()
    monkeypatch.setattr(projects, "PROJECTS", [
        projects.Project(key="deepwick", name="DeepWick", aliases=["dw"]),
        projects.Project(key="rentcla", name="Rentcla", aliases=[]),
    ])
    monkeypatch.setattr(projects, "backfill_document_project_keys", lambda: 0)
    return tmp_path


def _project_of() -> str | None:
    with db.db() as conn:
        row = conn.execute("SELECT project_key FROM documents").fetchone()
    return row["project_key"] if row else None


def _note(path: str, content: str, *, title: str = "", tags=None,
          wikilinks=None, frontmatter=None, source: str = "obsidian") -> None:
    from ops_dashboard.indexers import obsidian as _obsidian

    metadata = {"tags": tags or [], "wikilinks": wikilinks or []}
    # The links are extracted from the body, like the real indexer does.
    if wikilinks is None:
        metadata["wikilinks"] = _obsidian._extract_wikilinks(content)
    if frontmatter:
        metadata["frontmatter"] = frontmatter
    db.upsert_document(source=source, path=path, title=title or path.split("/")[-1],
                        content=content, metadata=metadata, mtime=1, size=len(content))


# ── attribution: what the note says ───────────────────────────────────────

def test_a_tag_says_which_project_it_is(env):
    _note("/vault/deepwick.md", "some notes", tags=["deepwick"])
    notes_structure.attribute_documents()
    assert _project_of() == "deepwick"


def test_the_frontmatter_says_it_outright(env):
    _note("/vault/notes/idea.md", "content", frontmatter={"project": "rentcla"})
    notes_structure.attribute_documents()
    assert _project_of() == "rentcla"


def test_a_link_points_at_the_project(env):
    _note("/vault/notes/idea.md", "see [[DeepWick]] for the rest")
    notes_structure.attribute_documents()
    assert _project_of() == "deepwick"


def test_the_title_names_the_project(env):
    _note("/vault/notes/rentcla-launch.md", "content")
    notes_structure.attribute_documents()
    assert _project_of() == "rentcla"


def test_one_word_in_passing_is_not_a_project(env):
    """A mention has to repeat before it counts."""
    _note("/vault/notes/random.md", "I thought about deepwick once.")
    notes_structure.attribute_documents()
    assert _project_of() is None


def test_a_note_that_repeats_it_does_belong(env):
    _note("/vault/notes/random.md",
          "deepwick deepwick deepwick — three times and it is about deepwick")
    notes_structure.attribute_documents()
    assert _project_of() == "deepwick"


def test_an_already_assigned_note_is_left_alone(env):
    """What the user (or the path) decided wins over what the text says."""
    _note("/vault/deepwick.md", "content", tags=["deepwick"])
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='rentcla'")
        conn.commit()
    notes_structure.attribute_documents()
    assert _project_of() == "rentcla"


def test_a_short_word_does_not_match_everything(env):
    """A needle of two letters would match half the vault."""
    monkey = projects.Project(key="ab", name="AB", aliases=[])
    projects.PROJECTS.insert(0, monkey)
    _note("/vault/notes/abc.md", "ab ab ab ab", tags=["ab"])
    notes_structure.attribute_documents()
    assert _project_of() is None


# ── the map ───────────────────────────────────────────────────────────────

def test_the_map_groups_the_notes_and_leaves_the_ideas(env):
    _note("/vault/deepwick/a.md", "deepwick deepwick deepwick")
    _note("/vault/deepwick/b.md", "more", tags=["deepwick"])
    _note("/vault/inbox/c.md", "a loose idea")
    _note("/vault/inbox/d.md", "another loose idea")

    data = notes_structure.structure()
    deepwick = next(p for p in data["projects"] if p["key"] == "deepwick")
    assert deepwick["notes"] == 2
    assert data["totals"]["attributed"] == 2
    assert data["totals"]["loose"] == 2
    ideas = {g["area"]: g["count"] for g in data["ideas"]}
    assert ideas.get("inbox") == 2


def test_a_folder_full_of_notes_suggests_a_project(env):
    for i in range(4):
        _note(f"/vault/polymarketea/note{i}.md", "some idea")
    data = notes_structure.structure()
    names = [s["name"] for s in data["suggestions"]]
    assert "polymarketea" in names


def test_a_tag_used_often_is_a_topic_not_a_project(env):
    """A tag says what a note is about; a folder says what the work is."""
    for i in range(3):
        _note(f"/vault/notes/n{i}.md", "idea", tags=["trading"])
    data = notes_structure.structure()
    assert "trading" not in [s["name"] for s in data["suggestions"]]
    assert "trading" in [t["name"] for t in data["topics"]]


def test_a_suggestion_never_repeats_a_registered_project(env):
    for i in range(4):
        _note(f"/vault/deepwick/n{i}.md", "idea")
    data = notes_structure.structure()
    names = [s["name"] for s in data["suggestions"]]
    assert "deepwick" not in names


# ── the map, written where the next agent looks ───────────────────────────

def test_the_export_is_a_map_an_agent_can_read(env):
    _note("/vault/deepwick/a.md", "deepwick deepwick deepwick")
    _note("/vault/inbox/c.md", "a loose idea")
    text = notes_structure.export_markdown()
    assert "# Projects and ideas" in text
    assert "DeepWick" in text
    assert "Ideas (not attached to a project)" in text
    assert "inbox" in text


def test_the_map_is_written_to_the_agent_workspace(env, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    import ops_dashboard.library as lib

    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        written = notes_structure.write_agent_map()
    finally:
        lib.memory_workspace = original
    path = tmp_path / "workspace" / "PROJECTS.md"
    assert path.exists()
    assert written["path"].endswith("PROJECTS.md")
    assert "Projects and ideas" in path.read_text(encoding="utf-8")


def test_the_indexer_keeps_the_frontmatter_values(env, tmp_path):
    """A note that says "project: deepwick" has to keep saying it."""
    from ops_dashboard.indexers import obsidian

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text(
        "---\nproject: deepwick\ntags: [x]\n---\n\nbody here", encoding="utf-8")
    obsidian.index_path(vault / "note.md")
    with db.db() as conn:
        row = conn.execute("SELECT metadata FROM documents").fetchone()
    meta = json.loads(row["metadata"])
    assert meta["frontmatter"]["project"] == "deepwick"


# ── the scraper: what is gone must go ─────────────────────────────────────

def test_a_deleted_note_stops_answering(env, tmp_path):
    """A note the user deleted must leave the index."""
    from ops_dashboard.indexers import obsidian

    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# DeepWick\n\nsome notes\n", encoding="utf-8")
    obsidian.run(vault_root=vault)
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 1

    note.unlink()
    obsidian.run(vault_root=vault)
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0


def test_a_vault_that_is_not_there_leaves_the_index_alone(env, tmp_path):
    """A disk that is not mounted is not a vault with nothing in it."""
    from ops_dashboard.indexers import obsidian

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("hello", encoding="utf-8")
    obsidian.run(vault_root=vault)
    with db.db() as conn:
        before = conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    assert before == 1

    for f in vault.glob("*.md"):
        f.unlink()
    vault.rmdir()
    obsidian.run(vault_root=vault)
    with db.db() as conn:
        after = conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    assert after == 1, "a vault that is not there must not wipe the index"


def test_the_prune_only_touches_its_own_folder(env, tmp_path):
    from ops_dashboard import db as _db

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("a", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "b.md").write_text("b", encoding="utf-8")
    _db.upsert_document(source="obsidian", path=str(other / "b.md"), title="b",
                         content="b", metadata={}, mtime=1, size=1)
    _db.upsert_document(source="obsidian", path=str(vault / "a.md"), title="a",
                         content="a", metadata={}, mtime=1, size=1)

    removed = _db.prune_documents(str(vault), set())
    assert removed == 1
    with _db.db() as conn:
        paths = [r["path"] for r in conn.execute("SELECT path FROM documents")]
    assert paths == [str(other / "b.md")]
