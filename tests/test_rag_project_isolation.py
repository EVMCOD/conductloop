"""The RAG must not answer about one business with another business's files.

Until 2026-10-23 the ``documents`` table had no owner column and the
retrieval searched every document of every project, so a question about
Globex could answer with Acme's notes — and cite the wrong file.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def two_projects(monkeypatch):
    from ops_dashboard import projects

    monkeypatch.setattr(projects, "PROJECTS", [
        projects.Project(key="alpha", name="Alpha", aliases=["alpha"],
                         repo_paths=["/tmp/alpha"]),
        projects.Project(key="beta", name="Beta", aliases=["beta"],
                         repo_paths=["/tmp/beta"]),
    ])
    return projects


def _doc(source: str, path: str, title: str) -> int:
    from ops_dashboard import db
    db.upsert_document(source=source, path=path, title=title, content=title + " body",
                       metadata={}, mtime=1, size=10)
    with db.db() as conn:
        return conn.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()[0]


def test_document_is_attributed_to_its_project(two_projects):
    from ops_dashboard import db, projects

    db.init_db()
    alpha_id = _doc("repo", "/tmp/alpha/notes.md", "Alpha notes")
    with db.db() as conn:
        row = conn.execute("SELECT project_key FROM documents WHERE id = ?",
                           (alpha_id,)).fetchone()
    assert row["project_key"] == "alpha"
    assert projects.project_for_path("/tmp/beta/other.md") == "beta"
    assert projects.project_for_path("/elsewhere/unknown.md") is None


def test_retrieval_drops_documents_of_other_projects(two_projects):
    from ops_dashboard import brain, db

    db.init_db()
    alpha_id = _doc("repo", "/tmp/alpha/notes.md", "Alpha notes")
    beta_id = _doc("repo", "/tmp/beta/notes.md", "Beta notes")

    rows = [
        {"doc_id": alpha_id, "source": "repo", "path": "/tmp/alpha/notes.md",
         "title": "Alpha notes", "score": 0.9},
        {"doc_id": beta_id, "source": "repo", "path": "/tmp/beta/notes.md",
         "title": "Beta notes", "score": 0.8},
    ]

    kept = brain._filter_by_project(rows, "alpha")
    assert [r["doc_id"] for r in kept] == [alpha_id]
    assert beta_id not in [r["doc_id"] for r in kept]

    kept = brain._filter_by_project(rows, "beta")
    assert [r["doc_id"] for r in kept] == [beta_id]


def test_retrieval_of_an_unknown_project_is_empty_not_someone_elses_files(two_projects):
    """Better an honest "nothing found" than another business's notes."""
    from ops_dashboard import brain, db

    db.init_db()
    alpha_id = _doc("repo", "/tmp/alpha/notes.md", "Alpha notes")
    rows = [{"doc_id": alpha_id, "source": "repo", "path": "/tmp/alpha/notes.md",
             "title": "Alpha notes", "score": 0.9}]
    assert brain._filter_by_project(rows, "gamma") == []


def test_shared_documents_are_kept_for_every_project(two_projects):
    """A note that belongs to no business can answer for any of them."""
    from ops_dashboard import brain, db

    db.init_db()
    shared_id = _doc("obsidian", "/vault/general/notes.md", "General notes")
    with db.db() as conn:
        assert conn.execute(
            "SELECT project_key FROM documents WHERE id = ?", (shared_id,)
        ).fetchone()["project_key"] is None

    rows = [{"doc_id": shared_id, "source": "obsidian", "path": "/vault/general/notes.md",
             "title": "General notes", "score": 0.9}]
    assert brain._filter_by_project(rows, "alpha")
    assert brain._filter_by_project(rows, "beta")


def test_no_project_filter_keeps_everything(two_projects):
    from ops_dashboard import brain, db

    db.init_db()
    alpha_id = _doc("repo", "/tmp/alpha/notes.md", "Alpha notes")
    beta_id = _doc("repo", "/tmp/beta/notes.md", "Beta notes")
    rows = [{"doc_id": alpha_id, "score": 0.9}, {"doc_id": beta_id, "score": 0.8}]
    assert len(brain._filter_by_project(rows, None)) == 2


def test_backfill_fills_the_documents_that_had_no_owner(two_projects):
    from ops_dashboard import db, projects

    db.init_db()
    # A document inserted before the column existed.
    with db.db() as conn:
        conn.execute(
            "INSERT INTO documents (source, path, title, content, metadata, mtime, size, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("repo", "/tmp/beta/legacy.md", "Legacy", "body", "{}", 1, 10, 1),
        )
    updated = projects.backfill_document_project_keys()
    assert updated >= 1
    with db.db() as conn:
        row = conn.execute(
            "SELECT project_key FROM documents WHERE path = ?", ("/tmp/beta/legacy.md",)
        ).fetchone()
    assert row["project_key"] == "beta"


# ── the ideas belong to the question too ──────────────────────────────────

def _doc_with_metadata(source: str, path: str, title: str, metadata: dict) -> int:
    from ops_dashboard import db

    db.upsert_document(source=source, path=path, title=title, content=title + " body",
                       metadata=metadata, mtime=1, size=10)
    with db.db() as conn:
        return conn.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()[0]


def test_an_idea_naming_the_project_is_attributed_to_it(two_projects):
    """This is how the ideas reach the answer.

    A note that belongs to nobody but names the project in its title or its
    tags is attributed to it, so it answers questions about that project
    instead of floating free. The attribution is the thing; the filter
    only follows it.
    """
    from ops_dashboard import db, notes_structure

    db.init_db()
    idea_id = _doc_with_metadata("obsidian", "/vault/idea.md", "An idea for Alpha",
                                 {"tags": ["alpha"]})
    notes_structure.attribute_documents()
    with db.db() as conn:
        row = conn.execute("SELECT project_key FROM documents WHERE id = ?",
                           (idea_id,)).fetchone()
    assert row["project_key"] == "alpha"


def test_another_projects_note_stays_out_even_if_it_mentions_this_one(two_projects):
    """The line holds: a file of another business does not answer here."""
    from ops_dashboard import brain, db

    db.init_db()
    alpha_id = _doc("repo", "/tmp/alpha/notes.md", "Alpha notes")
    beta_id = _doc_with_metadata("repo", "/tmp/beta/other.md", "About Alpha",
                                 {"tags": ["alpha"]})
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key = 'beta' WHERE id = ?", (beta_id,))
        conn.commit()

    rows = [
        {"doc_id": alpha_id, "source": "repo", "path": "/tmp/alpha/notes.md",
         "title": "Alpha notes", "score": 1.0},
        {"doc_id": beta_id, "source": "repo", "path": "/tmp/beta/other.md",
         "title": "About Alpha", "score": 1.0},
    ]
    kept = brain._filter_by_project(rows, "alpha")
    paths = {r["path"] for r in kept}
    assert "/tmp/alpha/notes.md" in paths
    assert "/tmp/beta/other.md" not in paths, "another project's file must stay out"


def test_an_unfiled_note_is_welcome_everywhere(two_projects):
    """An idea with no project is an idea, and it is welcome anywhere."""
    from ops_dashboard import brain, db

    db.init_db()
    alpha_id = _doc("repo", "/tmp/alpha/notes.md", "Alpha notes")
    idea_id = _doc_with_metadata("obsidian", "/vault/idea.md", "Some idea",
                                 {"tags": []})
    rows = [
        {"doc_id": alpha_id, "source": "repo", "path": "/tmp/alpha/notes.md",
         "title": "Alpha notes", "score": 1.0},
        {"doc_id": idea_id, "source": "obsidian", "path": "/vault/idea.md",
         "title": "Some idea", "score": 1.0},
    ]
    kept = brain._filter_by_project(rows, "alpha")
    paths = {r["path"] for r in kept}
    assert "/tmp/alpha/notes.md" in paths
    assert "/vault/idea.md" in paths
