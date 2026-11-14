"""Fallo 5 de los evals (#28): db.search must fold case for non-ASCII letters
("DECISIÓN" finds "decisión", "área" finds "ÁREA")."""
from __future__ import annotations

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from ops_dashboard import db as _db
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "unicode.db")
    _db.init_db()
    for path, title, content in [
        ("notes/precios.md", "Decisión de precios", "Tomamos la decisión de subir el plan Pro."),
        ("notes/soporte.md", "Soporte", "ÁREA DE SOPORTE: revisar la COLA ÚNICA de tickets."),
        ("notes/ascii.md", "Plain", "Nothing special here, just ASCII text."),
    ]:
        _db.upsert_document(source="obsidian", path=path, title=title, content=content,
                            metadata={}, mtime=1, size=len(content))
    return _db


def _paths(rows):
    return sorted(r["path"] for r in rows)


@pytest.mark.parametrize("q", ["DECISIÓN", "Decisión", "decisión", "DECISIÓN DE PRECIOS"])
def test_uppercase_accented_query_matches_lowercase_text(db, q):
    assert "notes/precios.md" in _paths(db.search(q))


@pytest.mark.parametrize("q", ["área de soporte", "Cola única", "ÚNICA"])
def test_lowercase_accented_query_matches_uppercase_text(db, q):
    assert _paths(db.search(q)) == ["notes/soporte.md"]


def test_excerpt_starts_at_the_unicode_match(db):
    rows = db.search("COLA ÚNICA")
    assert rows and rows[0]["excerpt"].startswith("COLA ÚNICA")


def test_ascii_queries_unchanged(db):
    assert _paths(db.search("ASCII TEXT")) == ["notes/ascii.md"]
    assert _paths(db.search("plan pro")) == ["notes/precios.md"]
    assert db.search("zzz") == []


def test_unicode_query_with_source_filter(db):
    assert _paths(db.search("DECISIÓN", source="obsidian")) == ["notes/precios.md"]
    assert db.search("DECISIÓN", source="repo") == []


def test_ascii_query_finds_text_that_only_matches_after_folding(db):
    db.upsert_document(source="obsidian", path="notes/de.md", title="Adresse",
                       content="Wir wohnen in der Hauptstraße 5.", metadata={}, mtime=1, size=33)
    rows = db.search("HAUPTSTRASSE")
    assert _paths(rows) == ["notes/de.md"]
    assert rows[0]["excerpt"].startswith("Hauptstraße")


def test_excerpt_offset_survives_length_changing_folds(db):
    text = "Straße — CAFÉ am Eck."
    db.upsert_document(source="obsidian", path="notes/cafe.md", title="Eck", content=text,
                       metadata={}, mtime=1, size=len(text))
    rows = db.search("café")
    assert rows and rows[0]["excerpt"].startswith("CAFÉ am Eck")
