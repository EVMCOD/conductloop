"""Fallo 4 de los evals (#28): db.search(source=...) must filter content AND
title matches (missing parentheses), and fuzzy title matches too."""
from __future__ import annotations

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from ops_dashboard import db as _db
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "search.db")
    _db.init_db()
    for source, path, title, content in [
        ("obsidian", "notes/pricing.md", "Precios", "El plan Pro cuesta 29 € al mes."),
        ("repo", "repo/README.md", "README", "Instalar el plan Pro con make install."),
        ("obsidian", "notes/arch.md", "Arquitectura local-first", "SQLite en el Mac."),
        ("repo", "repo/docs/arch.md", "Arquitectura del servidor", "Rutas y registry."),
    ]:
        _db.upsert_document(source=source, path=path, title=title, content=content,
                            metadata={}, mtime=1, size=len(content))
    return _db


def _paths(rows):
    return sorted(r["path"] for r in rows)


def test_no_filter_returns_all_sources(db):
    assert _paths(db.search("plan Pro")) == ["notes/pricing.md", "repo/README.md"]


def test_source_filter_applies_to_content_matches(db):
    assert _paths(db.search("plan Pro", source="repo")) == ["repo/README.md"]
    assert _paths(db.search("plan Pro", source="obsidian")) == ["notes/pricing.md"]
    assert db.search("plan Pro", source="gmail") == []


def test_source_filter_applies_to_title_matches(db):
    assert _paths(db.search("Arquitectura", source="repo")) == ["repo/docs/arch.md"]


def test_source_filter_applies_to_fuzzy_matches(db):
    rows = db.search("Arquitectrua local-first", source="repo", fuzzy=True)
    assert all(r["source"] == "repo" for r in rows)
    assert "notes/arch.md" not in _paths(rows)
    rows = db.search("Arquitectrua local-first", source="obsidian", fuzzy=True)
    assert "notes/arch.md" in _paths(rows)
