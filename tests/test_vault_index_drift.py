"""The vault's own index, and where it stops agreeing with the vault.

The index is written by hand, so it drifts: a project note appears and
the index does not mention it, the index counts fewer active projects than
there are, and a link points at a note that was deleted.

These tests build a small vault with that shape and check that the drift
is reported honestly — the point is to tell the user what their hand-made
index got wrong, never to rewrite it.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def vault(tmp_path):
    """A vault with a hand-made index, one project and one idea."""
    root = tmp_path / "vault"
    (root / "projects").mkdir(parents=True)

    def note(rel: str, text: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    note("projects/_index.md", """---
title: Projects
type: hub
tags: [hub, project]
---

# Projects

## Active
- [[alpha]] — the first one
- [[beta]] — the second one
""")
    note("projects/alpha.md", """---
title: Alpha
type: project
status: active
---

# Alpha

Some notes about alpha.
""")
    note("projects/beta.md", """---
title: Beta
type: project
status: active
---

# Beta
""")
    note("projects/gamma.md", """---
title: Gamma
type: project
status: active
---

# Gamma

This one never made it into the index.
""")
    note("projects/idea-thing.md", """---
title: An idea
type: project
status: idea
---

# An idea
""")
    note("projects/alpha/architecture.md", """---
title: Alpha architecture
type: note
---

# Architecture

A note of alpha that its page never links.
""")
    note("daily/2026-10-01.md", """---
title: 2026-10-01
---

# Day one

Talked about alpha and gamma today.
""")
    return root


@pytest.fixture()
def roots(vault, monkeypatch, tmp_path):
    import ops_dashboard.library as lib
    from ops_dashboard import db

    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "drift.db"))
    db.DB_PATH = tmp_path / "drift.db"  # type: ignore[attr-defined]
    db.init_db()
    monkeypatch.setattr(lib, "index_folders", lambda: [vault])
    monkeypatch.setattr(lib, "memory_workspace", lambda: vault.parent / "workspace")
    return [vault]


# ── what the vault says ───────────────────────────────────────────────────

def test_a_project_is_a_note_that_says_so(roots):
    from ops_dashboard import notes_structure

    drift = notes_structure.index_drift(roots=roots)
    assert drift["projects"] == 4          # alpha, beta, gamma and the idea
    assert drift["by_status"]["active"] == 3
    assert drift["by_status"]["idea"] == 1


def test_the_index_is_found_and_read(roots):
    from ops_dashboard import notes_structure

    drift = notes_structure.index_drift(roots=roots)
    hubs = [h["stem"] for h in drift["hubs"]]
    assert "_index" in hubs


# ── where it disagrees ────────────────────────────────────────────────────

def test_a_project_that_is_not_in_the_index_is_reported(roots):
    from ops_dashboard import notes_structure

    drift = notes_structure.index_drift(roots=roots)
    assert "gamma" in drift["missing_from_index"]
    assert "alpha" not in drift["missing_from_index"]


def test_a_link_that_points_to_nothing_is_reported(roots):
    from ops_dashboard import notes_structure

    (roots[0] / "projects" / "_index.md").write_text(
        "---\ntype: hub\n---\n\n- [[alpha]]\n- [[deleted-note]]\n", encoding="utf-8")
    drift = notes_structure.index_drift(roots=roots)
    assert "deleted-note" in drift["links_to_nothing"]
    assert "alpha" not in drift["links_to_nothing"]


def test_the_ideas_are_counted_apart(roots):
    from ops_dashboard import notes_structure

    drift = notes_structure.index_drift(roots=roots)
    assert "idea-thing" in drift["ideas"]


def test_a_note_of_a_project_that_its_page_does_not_link(roots):
    from ops_dashboard import notes_structure

    rows = notes_structure.unlinked_notes(roots=roots)
    assert len(rows) == 1
    assert rows[0]["project"] == "alpha"
    assert "architecture" in rows[0]["orphans"]


def test_a_daily_note_that_mentions_a_project_is_not_one_of_its_notes(roots):
    """Mentioning a project is not belonging to it."""
    from ops_dashboard import notes_structure

    rows = notes_structure.unlinked_notes(roots=roots)
    assert all("2026-10-01" not in row["orphans"] for row in rows)


def test_the_map_carries_the_drift(roots):
    from ops_dashboard import notes_structure

    data = notes_structure.structure()
    assert "drift" in data
    assert "gamma" in data["drift"]["missing_from_index"]
    assert data["unlinked"]
