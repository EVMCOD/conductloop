"""Guards the E2E encrypted sync bundle (#9): encrypted at rest, roundtrips
with the right passphrase, fails with the wrong one."""
from __future__ import annotations

import pytest

pytest.importorskip("cryptography")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "s.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "s.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import sync_bundle
    return _db, sync_bundle, tmp_path


def test_bundle_is_encrypted_at_rest(env):
    db, sb, tmp = env
    db.memory_set("secret", "the launch codes are 12345")
    out = tmp / "b.cbundle"
    sb.export_bundle("correct horse", out)
    raw = out.read_bytes()
    assert raw.startswith(b"CLBUNDLE1")
    assert b"launch codes" not in raw  # plaintext must not be on disk


def test_roundtrip_restores_memory(env):
    db, sb, tmp = env
    db.memory_set("k1", "value one")
    db.add_decision("Chose SQLite", "local-first")
    out = tmp / "b.cbundle"
    sb.export_bundle("pw", out)
    # wipe memory to simulate a fresh device
    with db.get_connection() as conn:
        conn.execute("DELETE FROM brain_memory")
        conn.commit()
    assert db.memory_get("k1") is None
    r = sb.import_bundle(out, "pw")
    assert r["memory"] == 1 and r["decisions"] == 1
    assert db.memory_get("k1") == "value one"


def test_wrong_passphrase_fails(env):
    db, sb, tmp = env
    db.memory_set("k", "v")
    out = tmp / "b.cbundle"
    sb.export_bundle("right", out)
    with pytest.raises(ValueError):
        sb.import_bundle(out, "wrong")
