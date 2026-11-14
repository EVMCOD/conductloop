"""Guards the voice -> auto-file pipeline (#6)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "v.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "v.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import voice_autofile
    return _db, voice_autofile


def test_classify(env):
    _, vf = env
    assert vf.classify("remember that the key is in the keychain") == "memory"
    assert vf.classify("we decided to use SQLite") == "decision"
    assert vf.classify("add to inbox: call the bank") == "capture"
    assert vf.classify("just some observation about the weather") == "note"


def test_file_routes_by_intent(env):
    db, vf = env
    m = vf.file_transcript("remember that the app is local-first")
    assert m["kind"] == "memory"
    assert any("local-first" in x["value"] for x in db.memory_list())

    d = vf.file_transcript("we decided to ship on Friday")
    assert d["kind"] == "decision"
    assert any("ship on Friday" in x["decision"] for x in db.list_decisions())

    c = vf.file_transcript("add to inbox: renew the domain")
    assert c["kind"] == "capture"

    n = vf.file_transcript("the office was noisy today")
    assert n["kind"] == "note"


def test_empty_rejected(env):
    _, vf = env
    with pytest.raises(ValueError):
        vf.file_transcript("  ")
