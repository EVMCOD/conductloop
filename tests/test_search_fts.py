"""Search: relevance, robustness, and staying inside the project.

Three layers feed the same answer: FTS5 for relevance and recall, LIKE for
exact substrings the index does not cover, and fuzzy matching for typos.
The order matters, and so does the project filter — the RAG is isolated per
business, and search is one of the ways that isolation is enforced.
"""
from __future__ import annotations

import pytest

from ops_dashboard import db


@pytest.fixture(autouse=True)
def vault(monkeypatch):
    """Documents owned by two throwaway projects, on the suite database.

    The repo's tests share one database; these keys are unique to this file,
    so the isolation assertions cannot be satisfied by somebody else's rows.
    """
    from ops_dashboard import projects

    monkeypatch.setattr(projects, "PROJECTS", [
        projects.Project(key="zzalpha", name="Alpha", aliases=["zzalpha"],
                         repo_paths=["/tmp/zzalpha"]),
        projects.Project(key="zzbeta", name="Beta", aliases=["zzbeta"],
                         repo_paths=["/tmp/zzbeta"]),
    ])
    db.init_db()
    _add("Alpha Momentum Indicators Strategy", "RSI and MACD entries for swing trades", "zzalpha")
    _add("Beta Grid Trading Notes", "averaging down a losing position", "zzbeta")
    _add("Beta Globex webhook trading", "order flow webhook zzbeta", "zzbeta")
    _add("Alpha Informe de decisiones", "la decisión tomada zzalpha", "zzalpha")
    yield
    _wipe()


def _wipe() -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM documents WHERE path LIKE '/zzvault/%'")
        conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")


_SEQ = {"n": 0}


def _add(title: str, content: str, project: str):
    """A document under its own path, carrying a unique token in the body.

    upsert_document keys on path, so a shared path would silently collapse
    every seeded document into the last one.
    """
    _SEQ["n"] += 1
    token = f"zzmark{_SEQ['n']}"
    db.upsert_document(source="obsidian", path=f"/zzvault/{project}/{token}.md",
                       title=title, content=f"{content} {token}", metadata={},
                       mtime=1700000000, size=10)
    return token


# ── the query builder ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "NOT (", '"', "AND OR NOT", "*", "()", "a AND", "-", "NEAR(a b)", "column:value",
    "  ", "\x00", "(((((", "'", "\\", "%", "_",
])
def test_a_hostile_query_never_raises(raw):
    """FTS5 has a query language; user input must not be able to break it."""
    db.search(raw, limit=5)


def test_fts_operators_in_the_query_are_treated_as_words():
    """Typing "not" must search for the word, not invert the query."""
    _add("Do not trade on Fridays", "a note zzabout not trading", "zzalpha")
    rows = db.search("zzabout", limit=20)
    assert any("Fridays" in (r["title"] or "") for r in rows), \
        [r["title"] for r in rows]


def test_a_plain_word_is_matched_as_a_prefix():
    """A query word in the singular still finds the plural."""
    _add("Plural Strategies handbook", "entries zzplural handbook", "zzalpha")
    rows = db.search("Strategie", limit=20)   # query singular, document plural
    assert any("Strategies" in (r["title"] or "") for r in rows), \
        [r["title"] for r in rows]


# ── relevance ──────────────────────────────────────────────────────────────

def test_a_title_hit_outranks_a_body_hit():
    rows = db.search("Momentum Indicators Strategy", limit=5)
    assert rows
    assert "Momentum" in (rows[0]["title"] or "")


def test_fts_finds_what_like_misses():
    """Two separate words: LIKE wants the exact phrase, FTS5 wants either."""
    rows = db.search("grid averaging", limit=10)
    assert any("Grid" in (r["title"] or "") for r in rows), rows


def test_results_carry_an_excerpt():
    rows = db.search("Momentum", limit=3)
    assert rows
    assert rows[0]["excerpt"]


def test_a_title_only_hit_still_carries_an_excerpt():
    """The match is in the title; showing no context at all reads as a bug."""
    _add("Zzsolo in the title only", "body text with nothing relevant", "zzalpha")
    rows = [r for r in db.search("Zzsolo", limit=20)
            if "Zzsolo in the title only" in (r["title"] or "")]
    assert rows
    assert rows[0]["excerpt"], "a title match must still produce a snippet"


def test_a_query_with_no_match_returns_nothing():
    assert db.search("zzzqqqxyznotpresent", limit=5) == []


def test_no_duplicate_rows_across_layers():
    """FTS5 and LIKE both matching must not list a document twice."""
    rows = db.search("trading", limit=20)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))


# ── project isolation ──────────────────────────────────────────────────────

def _paths_of(rows) -> set:
    with db.db() as conn:
        ids = [r["id"] for r in rows]
        if not ids:
            return set()
        q = ",".join("?" * len(ids))
        return {r["path"] for r in conn.execute(
            f"SELECT path FROM documents WHERE id IN ({q})", ids)}


def test_a_project_filter_keeps_the_other_project_out(vault):
    rows = db.search("zzmark", limit=20, project_key="zzbeta")
    assert rows
    assert all("/zzbeta/" in p for p in _paths_of(rows)), _paths_of(rows)


def test_without_the_filter_the_other_project_is_searchable(vault):
    """Isolation is a filter, not a wall: it only applies when asked."""
    paths = _paths_of(db.search("zzmark", limit=20))
    assert any("/zzalpha/" in p for p in paths)
    assert any("/zzbeta/" in p for p in paths)


def test_the_project_filter_applies_to_every_layer(vault):
    """A fuzzy or LIKE hit from another project must not leak in either."""
    rows = db.search("zzmark", limit=20, fuzzy=True, project_key="zzbeta")
    assert all("/zzbeta/" in p for p in _paths_of(rows)), _paths_of(rows)


def test_a_doc_of_another_project_is_never_returned_for_its_own_words(vault):
    rows = db.search("Globex webhook zzbeta", limit=20, project_key="zzalpha")
    assert _paths_of(rows) & {p for p in _paths_of(rows) if "/zzbeta/" in p} == set()
