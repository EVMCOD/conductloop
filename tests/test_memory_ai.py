"""Guards smart memory (#2): keyword-fallback recall + fact extraction."""
from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "m.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "m.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import memory_ai
    return _db, memory_ai


def test_recall_ranks_relevant_memory(env):
    db, mem = env
    db.memory_set("db_choice", "The engine uses SQLite with FTS5 and WAL")
    db.memory_set("owner", "Ana lives in Spain")
    # Words that overlap the target memory so both the semantic and the
    # keyword-fallback paths rank it first (CI has no embedding model).
    r = mem.recall("engine uses sqlite storage", limit=2)
    assert r, "recall returned nothing"
    assert r[0]["key"] == "db_choice"
    assert r[0]["method"] in ("semantic", "keyword")


def test_recall_empty(env):
    _, mem = env
    assert mem.recall("anything") == []


def test_propose_extracts_facts(env):
    _, mem = env
    text = (
        "Hey. The backend uses SQLite for storage. "
        "I had lunch. Ana prefers dark mode. "
        "Random short."
    )
    facts = mem.propose_from_text(text)
    joined = " ".join(facts).lower()
    assert "sqlite" in joined and "dark mode" in joined
    assert "i had lunch" not in joined  # no fact cue


def test_remember_proposals_persists(env):
    db, mem = env
    n = mem.remember_proposals(["The app is local-first"])
    assert n == 1
    assert any("local-first" in m["value"] for m in db.memory_list())


def test_recall_caches_embeddings(env, monkeypatch):
    """Second recall embeds only the query — cached memory vectors are reused."""
    db, mem = env
    from ops_dashboard import embeddings
    calls = {"n": 0}

    def fake_embed(text):
        calls["n"] += 1
        # tiny deterministic vector from the text
        return [float(len(text) % 7), 1.0, 0.0]

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    db.memory_set("a", "alpha memory")
    db.memory_set("b", "beta memory")

    mem.recall("query one")
    first = calls["n"]
    assert first == 3          # 2 memories indexed + 1 query
    mem.recall("query two")
    assert calls["n"] == first + 1   # only the new query embeds

    # editing a memory re-indexes just that one on the next recall. Bump
    # updated_at clearly past indexed_at (both land in the same second here).
    db.memory_set("a", "alpha memory edited")
    with db.get_connection() as conn:
        conn.execute("UPDATE brain_memory SET updated_at = updated_at + 100 WHERE key = 'a'")
        conn.commit()
    mem.recall("query three")
    assert calls["n"] == first + 1 + 2   # 1 re-embed (a) + 1 query
