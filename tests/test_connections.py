"""Guards the encrypted-credentials path of db_connections: secrets must be
Fernet-encrypted at rest and round-trip back to plaintext on read.

Needs cryptography; skipped where it isn't installed.
    .venv/bin/pytest tests/test_connections.py -v
"""
from __future__ import annotations

import pytest

pytest.importorskip("cryptography")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "conn.db"))
    # Keep the Fernet key in the file store under tmp, keychain off — the test
    # must never touch the real Keychain or the real ~.
    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "secrets"))
    import ops_dashboard.db as _db
    import ops_dashboard.db_connections as _conn  # noqa: F401
    _db.DB_PATH = tmp_path / "conn.db"  # type: ignore[attr-defined]
    _db.init_db()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(_conn.Path, "home", staticmethod(lambda: home))
    return _db


def test_credentials_encrypted_at_rest_and_roundtrip(db):
    cid = db.add_connection("github", "main", {"token": "secret123"})
    # Read-back decrypts transparently.
    got = db.get_connection_by_name("github", "main")
    assert got["credentials"] == {"token": "secret123"}
    assert db.get_connection_by_id(cid)["credentials"] == {"token": "secret123"}
    # The raw stored blob is encrypted — plaintext secret must not be on disk.
    with db.get_connection() as conn:
        raw = conn.execute("SELECT credentials FROM connections WHERE id = ?", (cid,)).fetchone()[0]
    assert raw.startswith("enc:")
    assert "secret123" not in raw


def test_missing_connection_returns_none(db):
    assert db.get_connection_by_name("github", "nope") is None
