"""Guards auto-memory in the chat flow (#3): brain.ask proposes durable facts
from the exchange and can auto-save them."""
from __future__ import annotations

import pytest


class _FactProvider:
    name = "fake"

    def available(self):
        return True

    def ask(self, system, messages):
        return "The ConductLoop engine uses SQLite for local-first storage."


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "am.db"))
    import ops_dashboard.db as db
    db.DB_PATH = tmp_path / "am.db"  # type: ignore[attr-defined]
    db.init_db()
    from ops_dashboard import brain
    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: _FactProvider())
    return db, brain


def test_ask_surfaces_memory_proposals(env):
    _, brain = env
    ans = brain.ask("what database does it use?")
    assert isinstance(ans.memory_proposals, list)
    assert any("sqlite" in p.lower() for p in ans.memory_proposals)


def test_auto_memory_save_persists(env, monkeypatch):
    db, brain = env
    monkeypatch.setenv("BRAIN_AUTO_MEMORY", "save")
    brain.ask("what database does it use?")
    assert any("sqlite" in m["value"].lower() for m in db.memory_list())


def test_no_save_by_default(env):
    db, brain = env
    brain.ask("what database does it use?")
    # proposals surfaced but not persisted unless BRAIN_AUTO_MEMORY=save
    assert db.memory_list() == []
