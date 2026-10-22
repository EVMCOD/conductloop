"""Guards the Obsidian memory bridge: export writes markdown notes to the
vault, import reads them back, and a round-trip preserves the value."""
from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "o.db"))
    monkeypatch.setenv("OPS_MEMORY_VAULT_DIR", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "o.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import obsidian_sync
    return _db, obsidian_sync, tmp_path


def test_export_writes_notes(env):
    db, sync, tmp = env
    db.memory_set("project:alpha", "Alpha uses SQLite + FTS")
    db.memory_set("owner", "Ana")
    out = sync.export_memory()
    assert out["written"] == 2
    d = tmp / "vault" / "ConductLoop Memory"
    files = list(d.glob("*.md"))
    assert len(files) == 2
    text = (d / "owner.md").read_text()
    assert "key: owner" in text and "Ana" in text


def test_roundtrip_export_then_import(env):
    db, sync, tmp = env
    db.memory_set("k", "original value")
    sync.export_memory()
    # Edit the note in the "vault" and import it back.
    note = tmp / "vault" / "ConductLoop Memory" / "k.md"
    note.write_text(note.read_text().replace("original value", "edited in obsidian"))
    r = sync.import_memory()
    assert r["imported"] == 1
    assert db.memory_get("k") == "edited in obsidian"


def test_status_reports_counts(env):
    db, sync, tmp = env
    db.memory_set("a", "1")
    s0 = sync.status()
    assert s0["memory_count"] == 1 and s0["folder_exists"] is False
    sync.export_memory()
    s1 = sync.status()
    assert s1["folder_exists"] is True and s1["note_count"] == 1


def test_export_without_vault_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "o.db"))
    monkeypatch.setenv("OPS_MEMORY_VAULT_DIR", str(tmp_path / "does-not-exist"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "o.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import obsidian_sync
    with pytest.raises(FileNotFoundError):
        obsidian_sync.export_memory()


def test_export_decisions(env):
    db, sync, tmp = env
    db.add_decision("Use SQLite", "Local-first, no server DB", project_key="proj", tags=["arch"])
    out = sync.export_decisions()
    assert out["written"] == 1
    notes = list((tmp / "vault" / "ConductLoop Decisions").glob("*.md"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "type: conductloop-decision" in text and "Local-first" in text


def test_export_all(env):
    db, sync, tmp = env
    db.memory_set("k", "v")
    db.add_decision("D", "did a thing")
    r = sync.export_all()
    assert r["memory"]["written"] == 1 and r["decisions"]["written"] == 1
