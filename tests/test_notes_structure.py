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
        projects.Project(key="globex", name="Globex", aliases=["dw"]),
        projects.Project(key="acme", name="Acme", aliases=[]),
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
    _note("/vault/globex.md", "some notes", tags=["globex"])
    notes_structure.attribute_documents()
    assert _project_of() == "globex"


def test_the_frontmatter_says_it_outright(env):
    _note("/vault/notes/idea.md", "content", frontmatter={"project": "acme"})
    notes_structure.attribute_documents()
    assert _project_of() == "acme"


def test_a_link_points_at_the_project(env):
    _note("/vault/notes/idea.md", "see [[Globex]] for the rest")
    notes_structure.attribute_documents()
    assert _project_of() == "globex"


def test_the_title_names_the_project(env):
    _note("/vault/notes/acme-launch.md", "content")
    notes_structure.attribute_documents()
    assert _project_of() == "acme"


def test_one_word_in_passing_is_not_a_project(env):
    """A mention has to repeat before it counts."""
    _note("/vault/notes/random.md", "I thought about globex once.")
    notes_structure.attribute_documents()
    assert _project_of() is None


def test_a_note_that_repeats_it_does_belong(env):
    _note("/vault/notes/random.md",
          "globex globex globex — three times and it is about globex")
    notes_structure.attribute_documents()
    assert _project_of() == "globex"


def test_an_already_assigned_note_is_left_alone(env):
    """What the user (or the path) decided wins over what the text says."""
    _note("/vault/globex.md", "content", tags=["globex"])
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='acme'")
        conn.commit()
    notes_structure.attribute_documents()
    assert _project_of() == "acme"


def test_a_short_word_does_not_match_everything(env):
    """A needle of two letters would match half the vault."""
    monkey = projects.Project(key="ab", name="AB", aliases=[])
    projects.PROJECTS.insert(0, monkey)
    _note("/vault/notes/abc.md", "ab ab ab ab", tags=["ab"])
    notes_structure.attribute_documents()
    assert _project_of() is None


# ── the map ───────────────────────────────────────────────────────────────

def test_the_map_groups_the_notes_and_leaves_the_ideas(env):
    _note("/vault/globex/a.md", "globex globex globex")
    _note("/vault/globex/b.md", "more", tags=["globex"])
    _note("/vault/inbox/c.md", "a loose idea")
    _note("/vault/inbox/d.md", "another loose idea")

    data = notes_structure.structure()
    globex = next(p for p in data["projects"] if p["key"] == "globex")
    assert globex["notes"] == 2
    assert data["totals"]["attributed"] == 2
    assert data["totals"]["loose"] == 2
    ideas = {g["area"]: g["count"] for g in data["ideas"]}
    assert ideas.get("inbox") == 2


def test_a_folder_full_of_notes_suggests_a_project(env):
    for i in range(4):
        _note(f"/vault/sideproject/note{i}.md", "some idea")
    data = notes_structure.structure()
    names = [s["name"] for s in data["suggestions"]]
    assert "sideproject" in names


def test_a_tag_used_often_is_a_topic_not_a_project(env):
    """A tag says what a note is about; a folder says what the work is."""
    for i in range(3):
        _note(f"/vault/notes/n{i}.md", "idea", tags=["trading"])
    data = notes_structure.structure()
    assert "trading" not in [s["name"] for s in data["suggestions"]]
    assert "trading" in [t["name"] for t in data["topics"]]


def test_a_suggestion_never_repeats_a_registered_project(env):
    for i in range(4):
        _note(f"/vault/globex/n{i}.md", "idea")
    data = notes_structure.structure()
    names = [s["name"] for s in data["suggestions"]]
    assert "globex" not in names


# ── the map, written where the next agent looks ───────────────────────────

def test_the_export_is_a_map_an_agent_can_read(env):
    _note("/vault/globex/a.md", "globex globex globex")
    _note("/vault/inbox/c.md", "a loose idea")
    text = notes_structure.export_markdown()
    assert "# Projects and ideas" in text
    assert "Globex" in text
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
    """A note that says "project: globex" has to keep saying it."""
    from ops_dashboard.indexers import obsidian

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text(
        "---\nproject: globex\ntags: [x]\n---\n\nbody here", encoding="utf-8")
    obsidian.index_path(vault / "note.md")
    with db.db() as conn:
        row = conn.execute("SELECT metadata FROM documents").fetchone()
    meta = json.loads(row["metadata"])
    assert meta["frontmatter"]["project"] == "globex"


# ── the scraper: what is gone must go ─────────────────────────────────────

def test_a_deleted_note_stops_answering(env, tmp_path):
    """A note the user deleted must leave the index."""
    from ops_dashboard.indexers import obsidian

    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# Globex\n\nsome notes\n", encoding="utf-8")
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


# ── the agent is told to read the map ─────────────────────────────────────

def test_the_agent_instructions_point_at_the_map(env, tmp_path):
    """An agent that follows its startup list has to find the map."""
    import ops_dashboard.library as lib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(
        "# AGENTS.md\n\n## Session Startup\n\n1. Read SOUL.md\n2. Read MEMORY.md\n",
        encoding="utf-8")
    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        notes_structure.write_agent_map()
    finally:
        lib.memory_workspace = original
    text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "PROJECTS.md" in text
    assert notes_structure.POINTER_BEGIN in text
    # The user's own text is untouched.
    assert "Read SOUL.md" in text
    assert "Read MEMORY.md" in text


def test_the_pointer_is_refreshed_without_touching_the_rest(env, tmp_path):
    import ops_dashboard.library as lib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents = workspace / "AGENTS.md"
    agents.write_text(
        "# AGENTS.md\n\n## Session Startup\n\n1. Read SOUL.md\n",
        encoding="utf-8")
    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        # The first draw has nothing to compare against; the second settles
        # the summary. From then on the file is not touched again.
        notes_structure.write_agent_map()
        notes_structure.write_agent_map()
        first = agents.read_text(encoding="utf-8")
        notes_structure.write_agent_map()
        second = agents.read_text(encoding="utf-8")
    finally:
        lib.memory_workspace = original
    assert first == second, "writing it again must not change the block"
    assert second.count(notes_structure.POINTER_BEGIN) == 1


def test_the_map_is_not_written_into_the_workspace_unless_it_is_already_there(env, tmp_path):
    """The product does not start writing into the agent's workspace just
    because it happened to run."""
    import ops_dashboard.library as lib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        assert notes_structure.refresh_agent_map() is None
        assert not (workspace / "PROJECTS.md").exists()
    finally:
        lib.memory_workspace = original


# ── the map only moves when something moves ───────────────────────────────

def test_writing_the_map_twice_changes_nothing_the_second_time(env, tmp_path):
    """A file rewritten with the same thing every hour is a file that
    changes every hour."""
    import ops_dashboard.library as lib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        first = notes_structure.write_agent_map()
        second = notes_structure.write_agent_map()
    finally:
        lib.memory_workspace = original
    assert first["changed"] is True
    assert second["changed"] is False
    assert first["path"] == second["path"]


def test_the_agents_file_is_not_touched_when_nothing_changed(env, tmp_path):
    import ops_dashboard.library as lib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents = workspace / "AGENTS.md"
    agents.write_text("# AGENTS.md\n\n## Session Startup\n\n1. Read SOUL.md\n",
                      encoding="utf-8")
    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        # Twice: the first draw and the settling of the summary. After that
        # the file must not be touched again.
        notes_structure.write_agent_map()
        notes_structure.write_agent_map()
        before = agents.stat().st_mtime_ns
        notes_structure.write_agent_map()
        after = agents.stat().st_mtime_ns
    finally:
        lib.memory_workspace = original
    assert before == after, "the user's own file must not be touched every hour"


def test_the_pointer_says_what_changed(env, tmp_path):
    """The agent gets one line about what moved, not 39 KB to re-read."""
    import ops_dashboard.library as lib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        notes_structure.write_agent_map()
        _note("/vault/globex/new.md", "globex globex globex")
        notes_structure.write_agent_map()
    finally:
        lib.memory_workspace = original
    text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "Last change" in text
    assert "Globex" in text


def test_the_map_also_goes_where_the_notes_are(env, tmp_path):
    import ops_dashboard.library as lib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        written = notes_structure.write_agent_map(also_to=[vault])
    finally:
        lib.memory_workspace = original
    assert written["copies"] == [str(vault / "PROJECTS.md")]
    assert (vault / "PROJECTS.md").exists()
    assert "Projects and ideas" in (vault / "PROJECTS.md").read_text(encoding="utf-8")


def test_the_state_file_is_what_makes_the_change_knowable(env, tmp_path):
    import ops_dashboard.library as lib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        notes_structure.write_agent_map()
    finally:
        lib.memory_workspace = original
    state = json.loads((workspace / "PROJECTS.state.json").read_text(encoding="utf-8"))
    assert "fingerprint" in state
    assert state["fingerprint"]["projects"]


# ── the nightly pass ──────────────────────────────────────────────────────

def test_the_nightly_pass_writes_what_it_would_do(env, tmp_path, monkeypatch):
    """The user finds the proposal in the morning; the product proposes."""
    import ops_dashboard.library as lib
    from ops_dashboard import notes_structure

    # A vault with a project the index does not list.
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    (vault / "projects" / "_index.md").write_text(
        "---\ntype: hub\n---\n\n## Activos\n- [[alpha]]\n", encoding="utf-8")
    (vault / "projects" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: project\nstatus: active\n---\n\n# Alpha\n",
        encoding="utf-8")
    (vault / "projects" / "delta.md").write_text(
        "---\ntitle: Delta\ntype: project\nstatus: active\n---\n\n# Delta\n",
        encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_ws = lib.memory_workspace
    original_folders = lib.index_folders
    try:
        lib.memory_workspace = lambda: workspace
        lib.index_folders = lambda: [vault]
        result = notes_structure.write_proposal()
        assert result is not None
        assert result["pending"] >= 1
        text = (workspace / "PROPOSAL.md").read_text(encoding="utf-8")
        assert "What the product would do" in text
        assert "delta" in text
    finally:
        lib.memory_workspace = original_ws
        lib.index_folders = original_folders


def test_no_proposal_file_when_there_is_nothing_to_say(env, tmp_path, monkeypatch):
    import ops_dashboard.library as lib
    from ops_dashboard import notes_structure

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = lib.memory_workspace
    try:
        lib.memory_workspace = lambda: workspace
        assert notes_structure.write_proposal() is None
        assert not (workspace / "PROPOSAL.md").exists()
    finally:
        lib.memory_workspace = original
