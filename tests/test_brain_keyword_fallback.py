"""Fallo 1 de los evals (#28): without embeddings (lean desktop build) the Brain
must still retrieve vault notes by keyword instead of answering with no context."""
from __future__ import annotations

import pytest

from ops_dashboard import brain, embeddings


@pytest.fixture()
def vault_db(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "kw.db")
    db.init_db()
    for path, title, content in [
        ("decisions/pricing.md", "Decisión de precios del plan Pro", "Plan Pro a 29 € al mes."),
        ("architecture/local.md", "Arquitectura", "Ver [[Decisión de precios del plan Pro]]. SQLite en el Mac."),
        ("daily/2026-10-01.md", "2026-10-01", "Revisar tickets el viernes."),
    ]:
        db.upsert_document(source="obsidian", path=path, title=title, content=content,
                           metadata={}, mtime=1, size=len(content))
    return db


def _no_embeddings(monkeypatch):
    def boom(*a, **k):
        raise embeddings.EmbeddingsUnavailable("lean build")
    monkeypatch.setattr(embeddings, "embed", boom)


def test_keyword_terms_drop_stopwords_and_short_words():
    assert brain._keyword_terms("¿Qué decidimos sobre el precio del plan Pro?") == ["precio", "plan", "pro"]
    assert brain._keyword_terms("what is the plan?") == ["plan"]
    assert brain._keyword_terms("¿qué es eso?") == []


def test_lean_build_falls_back_to_keywords(vault_db, monkeypatch):
    _no_embeddings(monkeypatch)
    docs = brain._retrieve("¿Qué decidimos sobre el precio del plan Pro?")
    assert docs and docs[0]["path"] == "decisions/pricing.md"   # title match wins the tie
    assert docs[0]["score"] == 1.0 and "29 €" in docs[0]["content"]


def test_unrelated_question_still_gets_no_docs(vault_db, monkeypatch):
    _no_embeddings(monkeypatch)
    assert brain._retrieve("¿Qué escribí sobre la boda de mi prima en Sevilla?") == []


def test_semantic_results_skip_the_fallback(vault_db, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(embeddings, "search", lambda q, limit=15: [
        {"doc_id": 1, "source": "obsidian", "path": "decisions/pricing.md", "title": "x", "score": 0.9, "excerpt": ""}])
    called = []
    monkeypatch.setattr(db, "search", lambda *a, **k: called.append(a) or [])
    assert brain._retrieve("precio plan")[0]["path"] == "decisions/pricing.md"
    assert called == []


def test_ask_in_lean_build_sends_context_and_citations(vault_db, monkeypatch):
    _no_embeddings(monkeypatch)
    seen = {}

    class P:
        name = "fake"
        model = "fake"

        def available(self):
            return True

        def supports_tools(self):
            return False

        def ask(self, system, messages, **kw):
            seen["system"] = system
            return "29 € al mes [1]"

    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: P())
    ans = brain.ask("¿Cuál es el precio del plan Pro?")
    assert ans.citations and ans.citations[0]["path"] == "decisions/pricing.md"
    assert "29 € al mes" in seen["system"] and brain.NO_CONTEXT_GUARD not in seen["system"]
