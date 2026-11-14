"""Guards the Brain self-analytics rollup (#8)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "a.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "a.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import brain_analytics
    return _db, brain_analytics


def test_analytics_rollup(env):
    db, an = env
    db.log_usage(provider="anthropic", prompt_tokens=100, completion_tokens=50)
    db.log_usage(provider="ollama", prompt_tokens=20, completion_tokens=10)
    db.add_cost("anthropic", 500, period="2026-07", category="llm")
    db.memory_set("k", "v")
    db.add_decision("D", "did")

    a = an.analytics(days=30)
    assert a["llm"]["calls"] == 2
    assert a["llm"]["total_tokens"] == 180
    assert a["spend"]["total_cents"] == 500
    assert a["memory_entries"] == 1
    assert a["decisions"] == 1
    assert set(a) >= {"llm", "spend", "memory_entries", "decisions", "inbox"}


def test_analytics_empty_is_safe(env):
    _, an = env
    a = an.analytics()
    assert a["llm"]["calls"] == 0 and a["spend"]["total_cents"] == 0
