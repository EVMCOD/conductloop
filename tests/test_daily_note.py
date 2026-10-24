"""The daily note of the vault: written by the user, annotated by the product.

The daily note is not a report the product prints over: it is a note the
user writes in. The generated part lives in a marked block and only that
block is ever replaced — if the product ever replaced the whole file, the
user's work would be gone, and there is no second copy of a day.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """A vault with a daily note the user wrote."""
    from ops_dashboard import db

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    note = daily_dir / "2026-10-24.md"
    note.write_text(
        "## Claude Code — uso real de Deepwick\n\n- 131 activos, 108 un solo día\n",
        encoding="utf-8")
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "daily.db"))
    db.DB_PATH = tmp_path / "daily.db"  # type: ignore[attr-defined]
    db.init_db()
    return daily_dir, note


def test_the_users_note_is_never_destroyed(vault):
    """The whole point: his words are still there afterwards."""
    from ops_dashboard import daily

    daily_dir, note = vault
    before = note.read_text(encoding="utf-8")
    out = daily.write_daily("2026-10-24", out_dir=daily_dir)
    after = out.read_text(encoding="utf-8")
    assert before.strip() in after
    assert "131 activos" in after


def test_the_generated_part_lives_in_a_marked_block(vault):
    from ops_dashboard import daily

    daily_dir, note = vault
    out = daily.write_daily("2026-10-24", out_dir=daily_dir)
    text = out.read_text(encoding="utf-8")
    assert daily.BLOCK_BEGIN in text
    assert daily.BLOCK_END in text
    # The user's text is outside the block.
    assert text.index("131 activos") < text.index(daily.BLOCK_BEGIN)


def test_writing_it_twice_does_not_duplicate_anything(vault):
    from ops_dashboard import daily

    daily_dir, note = vault
    daily.write_daily("2026-10-24", out_dir=daily_dir)
    first = note.read_text(encoding="utf-8")
    daily.write_daily("2026-10-24", out_dir=daily_dir)
    second = note.read_text(encoding="utf-8")
    assert first == second
    assert second.count(daily.BLOCK_BEGIN) == 1


def test_a_note_that_is_only_generated_is_a_normal_note(vault):
    """Nothing of the user there: the note is just the generated part."""
    from ops_dashboard import daily

    daily_dir, _note = vault
    out = daily.write_daily("2026-10-25", out_dir=daily_dir)
    text = out.read_text(encoding="utf-8")
    assert daily.BLOCK_BEGIN in text
    assert "131 activos" not in text


def test_the_vault_half_says_what_moved(vault, monkeypatch):
    """The notes touched that day, and the projects they belong to."""
    from ops_dashboard import daily, db

    daily_dir, note = vault
    db.upsert_document(source="obsidian", path="/v/deepwick.md", title="Deepwick",
                       content="x", metadata={}, mtime=1780000000, size=1)
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='deepwick'")
        conn.commit()
    monkeypatch.setattr(daily, "vault_summary",
                        lambda day: {"notes": [{"title": "Deepwick", "path": "/v/deepwick.md",
                                                "project": "deepwick", "source": "obsidian"}],
                                     "projects": [("deepwick", 1)],
                                     "ideas": [{"title": "Research", "notes": 12}]})
    out = daily.write_daily("2026-10-24", out_dir=daily_dir)
    text = out.read_text(encoding="utf-8")
    assert "## 🗺️ El vault" in text
    assert "DeepWick" in text or "deepwick" in text
    assert "Research" in text


def test_the_daily_note_says_what_was_asked(vault, monkeypatch):
    """The project that moved, and the encargo of its newest handoff.

    "6 notes touched" says that something happened. The encargo says what
    the work is — and that is the line a new agent (or a Monday morning)
    needs.
    """
    from ops_dashboard import daily, db

    daily_dir, _note = vault
    db.upsert_document(source="obsidian", path="/v/deepwick.md", title="Deepwick",
                       content="x", metadata={}, mtime=1780000000, size=1)
    with db.db() as conn:
        conn.execute("UPDATE documents SET project_key='deepwick'")
        conn.commit()
    monkeypatch.setattr(daily, "vault_summary",
                        lambda day: {"notes": [{"title": "Deepwick", "path": "/v/deepwick.md",
                                                "project": "deepwick", "source": "obsidian"}],
                                     "projects": [("deepwick", 6)],
                                     "ideas": [],
                                     "handoffs": {"deepwick": {
                                         "title": "EVQL → Deepwick",
                                         "date": "2026-10-23",
                                         "encargo": "que todo pase a deepwick"}}})
    out = daily.write_daily("2026-10-24", out_dir=daily_dir)
    text = out.read_text(encoding="utf-8")
    assert "que todo pase a deepwick" in text
