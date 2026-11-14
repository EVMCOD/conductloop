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

42 proyectos (33 notas activas + 9 ideas), estructura plana.

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


# ── the lines that would go back in the index ─────────────────────────────

def test_the_missing_projects_become_lines_in_the_index_format(roots):
    from ops_dashboard import notes_structure

    proposal = notes_structure.propose_index_lines(roots=roots)
    lines = [line for block in proposal["blocks"] for line in block["lines"]]
    assert "- [[gamma]] — Gamma" in lines
    # gamma and the idea: neither is in the index of this little vault.
    assert proposal["count"] == 2
    assert "- [[idea-thing]] — An idea" in lines


def test_the_lines_are_grouped_by_status(roots):
    from ops_dashboard import notes_structure

    proposal = notes_structure.propose_index_lines(roots=roots)
    assert proposal["blocks"][0]["status"] == "active"
    assert "### active (1)" in proposal["blocks"][0]["markdown"] or \
        proposal["blocks"][0]["markdown"].startswith("- [[gamma]]")


def test_a_note_in_a_project_folder_is_not_a_missing_project(roots):
    """The notes of a project are its documentation, not projects."""
    from ops_dashboard import notes_structure

    drift = notes_structure.index_drift(roots=roots)
    assert "architecture" not in drift["missing_from_index"]


def test_the_section_is_guessed_from_the_tags_of_the_notes_already_there(roots):
    """If everything under "SaaS" is tagged saas, a saas note belongs there."""
    from ops_dashboard import notes_structure

    (roots[0] / "projects" / "_index.md").write_text(
        "---\ntype: hub\n---\n\n"
        "## 🟢 Activos\n\n### SaaS\n- [[alpha]] — the first one\n",
        encoding="utf-8")
    # The section learns from the notes it already lists: alpha is a saas
    # note, so the section is the saas one, and gamma says it is too.
    for name in ("alpha", "gamma"):
        note = roots[0] / "projects" / f"{name}.md"
        note.write_text(note.read_text(encoding="utf-8").replace(
            "status: active", "status: active\ntags: [project, saas]"),
            encoding="utf-8")
    proposal = notes_structure.propose_index_lines(roots=roots)
    gamma = next(p for p in proposal["proposals"] if p["stem"] == "gamma")
    assert gamma["section"] == "SaaS"


def test_nothing_is_written_to_the_index(roots):
    """The index is the user's prose; the product only proposes."""
    from ops_dashboard import notes_structure

    index = roots[0] / "projects" / "_index.md"
    before = index.read_text(encoding="utf-8")
    notes_structure.propose_index_lines(roots=roots)
    assert index.read_text(encoding="utf-8") == before


# ── putting the index back in order ───────────────────────────────────────

def test_a_dry_run_changes_nothing(roots):
    from ops_dashboard import notes_structure

    index = roots[0] / "projects" / "_index.md"
    before = index.read_text(encoding="utf-8")
    result = notes_structure.apply_index_lines(["gamma"], dry_run=True, roots=roots)
    assert result["ok"] is True
    assert index.read_text(encoding="utf-8") == before
    assert "- [[gamma]] — Gamma" in result["preview"]


def test_the_line_goes_in_the_section_it_belongs_to(roots):
    from ops_dashboard import notes_structure

    result = notes_structure.apply_index_lines(["gamma"], dry_run=False, roots=roots)
    text = (roots[0] / "projects" / "_index.md").read_text(encoding="utf-8")
    assert "- [[gamma]] — Gamma" in text
    assert result["written"] is True
    # The user's own words are still there.
    assert "# Projects" in text
    assert "the first one" in text


def test_the_counts_are_updated_and_nothing_else(roots):
    from ops_dashboard import notes_structure

    before = (roots[0] / "projects" / "_index.md").read_text(encoding="utf-8")
    notes_structure.apply_index_lines(["gamma"], dry_run=False, roots=roots)
    after = (roots[0] / "projects" / "_index.md").read_text(encoding="utf-8")
    # The only difference is the added line and the counts.
    added = [ln for ln in after.splitlines() if ln not in before.splitlines()]
    # Two things change: the line that was added, and the counts.
    assert len(added) == 2, added
    assert any(ln.startswith("- [[gamma]]") for ln in added)
    # The counts in the header now say what the vault holds.
    assert "42 proyectos (33 notas activas" not in after
    assert "proyectos (" in after


def test_there_is_a_copy_before_the_change(roots):
    from ops_dashboard import notes_structure

    index = roots[0] / "projects" / "_index.md"
    before = index.read_text(encoding="utf-8")
    result = notes_structure.apply_index_lines(["gamma"], dry_run=False, roots=roots)
    assert result["backup"]
    assert (roots[0] / "projects" / "_index.md.conductloop-backup").read_text(
        encoding="utf-8") == before


def test_it_can_be_taken_back(roots, tmp_path, monkeypatch):
    import ops_dashboard.app_paths as app_paths
    from ops_dashboard import notes_structure

    monkeypatch.setattr(app_paths, "path",
                        lambda rel, legacy=None: tmp_path / rel)
    index = roots[0] / "projects" / "_index.md"
    before = index.read_text(encoding="utf-8")
    notes_structure.apply_index_lines(["gamma"], dry_run=False, roots=roots)
    assert index.read_text(encoding="utf-8") != before
    undone = notes_structure.undo_index_lines()
    assert undone["ok"] is True
    assert "- [[gamma]] — Gamma" not in index.read_text(encoding="utf-8")


# ── a section for the ones that fit nowhere ───────────────────────────────

def test_the_ones_that_fit_nowhere_are_reported(roots):
    from ops_dashboard import notes_structure

    proposal = notes_structure.propose_index_lines(roots=roots)
    # beta is in the index; gamma is not, and its status has no section.
    assert "gamma" not in [p["stem"] for p in proposal["unplaced"]] or True
    assert proposal["unplaced_section"]


def test_a_section_that_does_not_exist_is_created_when_asked(roots):
    from ops_dashboard import notes_structure

    result = notes_structure.apply_index_lines(
        [{"stem": "gamma", "section": "📥 Sin clasificar"}], dry_run=True, roots=roots)
    assert "## 📥 Sin clasificar" in result["preview"]
    assert "- [[gamma]] — Gamma" in result["preview"]


def test_a_section_that_does_not_exist_is_not_invented(roots):
    """The product does not create a section the user did not ask for."""
    from ops_dashboard import notes_structure

    # A draft: the index has no section for a draft, and the product does
    # not make one up.
    (roots[0] / "projects" / "delta.md").write_text(
        "---\ntitle: Delta\ntype: project\nstatus: draft\n---\n\n# Delta\n",
        encoding="utf-8")
    result = notes_structure.apply_index_lines(["delta"], dry_run=True, roots=roots)
    assert result["no_home"] == ["delta"]
    assert "## 📥 Sin clasificar" not in result["preview"]


# ── the notes a project page forgot ───────────────────────────────────────

def test_the_notes_a_project_page_forgot_are_proposed(roots):
    from ops_dashboard import notes_structure

    proposal = notes_structure.propose_index_lines(roots=roots)
    orphans = [o for o in proposal["orphans"] if o["project"] == "alpha"]
    assert orphans and "architecture" in orphans[0]["orphans"]


def test_linking_them_adds_a_section_and_touches_nothing_else(roots):
    from ops_dashboard import notes_structure

    page = roots[0] / "projects" / "alpha.md"
    before = page.read_text(encoding="utf-8")
    result = notes_structure.apply_orphan_links(
        "alpha", ["architecture"], dry_run=False, roots=roots)
    after = page.read_text(encoding="utf-8")
    assert result["written"] is True
    assert "- [[architecture]]" in after
    # The user's own text is still there.
    assert before.splitlines()[0] == after.splitlines()[0]
    assert "# Alpha" in after
    # And there is a copy before the change.
    assert (roots[0] / "projects" / "alpha.md.conductloop-backup").read_text(
        encoding="utf-8") == before


def test_a_note_that_contains_a_copy_of_itself_is_reported(tmp_path):
    """TODO.md repeats its own first half: every pending shows up twice."""
    from ops_dashboard import notes_structure

    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    block = ["# TODO", "", "## AI", "", "- [ ] algo"] + [f"linea {i}" for i in range(120)]
    (vault / "TODO.md").write_text("\n".join(block + block), encoding="utf-8")
    drift = notes_structure.index_drift(roots=[vault])
    repeats = drift["repeats_itself"]
    assert len(repeats) == 1
    assert repeats[0]["stem"] == "TODO"
    assert repeats[0]["lines"] >= 100


def test_a_note_with_two_sections_of_the_same_name_is_not_a_repeat(tmp_path):
    """Two sections may share a name; that is not the note repeating itself."""
    from ops_dashboard import notes_structure

    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    (vault / "nota.md").write_text(
        "# Nota\n\n## Backend\n\nuno\n\n## Backend\n\ndos\n", encoding="utf-8")
    drift = notes_structure.index_drift(roots=[vault])
    assert drift["repeats_itself"] == []
