"""The vault of secrets: every key of the app, and never in plain text.

The claim is simple: a key that the app holds is not readable by anyone
who can read the files of the app. These tests prove it on the three
places a key could leak — the database, a file on disk, and the API.
"""
from __future__ import annotations

import json

import pytest

from ops_dashboard import secret_vault


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A vault of its own: no keychain, its own directory, no leftovers."""
    from ops_dashboard import db

    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path))
    db.init_db()
    secret_vault.lock()
    yield tmp_path
    secret_vault.lock()
    with db.db() as conn:
        conn.execute("DELETE FROM media_providers")


@pytest.fixture()
def fake_keychain(monkeypatch):
    """A keychain that keeps everything in memory."""
    from ops_dashboard import secrets_store

    items: dict[str, str] = {}

    def _set(name: str, value: str) -> bool:
        items[name] = value
        return True

    monkeypatch.setattr(secrets_store, "keychain_available", lambda: True)
    monkeypatch.setattr(secrets_store, "keychain_get", lambda name: items.get(name))
    monkeypatch.setattr(secrets_store, "keychain_set", _set)
    monkeypatch.setattr(secrets_store, "keychain_delete", lambda name: items.pop(name, None) is None)
    return items


# ── the vault itself ───────────────────────────────────────────────────────

def test_a_secret_goes_to_the_keychain_when_there_is_one(fake_keychain):

    assert secret_vault.set("byok.minimax.default", "sk-secret") == "keychain"
    assert fake_keychain["byok.minimax.default"] == "sk-secret"
    assert secret_vault.get("byok.minimax.default") == "sk-secret"
    assert secret_vault.where("byok.minimax.default") == "keychain"


def test_without_a_keychain_the_secret_goes_to_an_encrypted_file(sandbox):
    """The file is there, and it does not contain the secret."""
    assert secret_vault.unlock("a passphrase of my own")
    assert secret_vault.set("media.stability.api_key", "sk-secret") == "encrypted"

    path = secret_vault.vault_path()
    assert path.exists()
    raw = path.read_bytes()
    assert b"sk-secret" not in raw
    # Even the passphrase is not in there.
    assert b"a passphrase of my own" not in raw


def test_the_vault_is_locked_until_somebody_opens_it(sandbox):
    """A locked vault says so instead of pretending the key is not there."""
    secret_vault.unlock("the passphrase")
    secret_vault.set("media.stability.api_key", "sk-secret")
    secret_vault.lock()

    assert secret_vault.backend() == "locked"
    with pytest.raises(secret_vault.VaultLocked):
        secret_vault.get("media.stability.api_key")
    with pytest.raises(secret_vault.VaultLocked):
        secret_vault.set("media.replicate.api_key", "sk-other")


def test_a_wrong_passphrase_does_not_open_the_vault(sandbox):
    secret_vault.unlock("the right one")
    secret_vault.set("media.stability.api_key", "sk-secret")
    secret_vault.lock()

    assert secret_vault.unlock("the wrong one") is False
    assert secret_vault.backend() == "locked"
    # And nothing was destroyed by the attempt.
    assert secret_vault.unlock("the right one") is True
    assert secret_vault.get("media.stability.api_key") == "sk-secret"


def test_the_passphrase_is_never_written_anywhere(sandbox):
    secret_vault.unlock("my passphrase")
    secret_vault.set("byok.minimax.default", "sk-secret")
    for path in sandbox.rglob("*"):
        if path.is_file():
            assert b"my passphrase" not in path.read_bytes()


def test_a_secret_can_be_removed(sandbox):
    secret_vault.unlock("a passphrase")
    secret_vault.set("byok.minimax.default", "sk-secret")
    assert secret_vault.delete("byok.minimax.default") is True
    assert secret_vault.get("byok.minimax.default") is None
    assert secret_vault.where("byok.minimax.default") == "nowhere"


# ── the media providers ────────────────────────────────────────────────────

def test_a_media_provider_key_is_not_in_the_database(sandbox):
    """The database is the thing people copy around: no key goes in it."""
    from ops_dashboard import db, media

    secret_vault.unlock("a passphrase")
    media.providers.save_config("stability", {"api_key": "sk-stability", "model": "sd3"})

    with db.db() as conn:
        row = conn.execute(
            "SELECT config_json FROM media_providers WHERE key='stability'").fetchone()
    blob = row["config_json"] if row else ""
    assert "sk-stability" not in (blob or "")
    assert secret_vault.media_key_name("stability") in (blob or "")

    # And the provider still gets its key.
    provider = media.providers.build("stability")
    assert provider.api_key == "sk-stability"


def test_the_migration_moves_a_plaintext_key_out_of_the_database(sandbox):
    """A key that was there before the vault existed goes into it."""
    from ops_dashboard import db, media

    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO media_providers (key, enabled, priority, config_json, updated_at)"
            " VALUES ('stability', 1, 100, ?, 0)",
            (json.dumps({"api_key": "sk-old-plaintext"}),))
        conn.commit()

    secret_vault.unlock("a passphrase")
    result = media.providers.migrate_plaintext_keys()
    assert "stability" in result["moved"]

    with db.db() as conn:
        blob = conn.execute(
            "SELECT config_json FROM media_providers WHERE key='stability'").fetchone()["config_json"]
    assert "sk-old-plaintext" not in blob
    assert secret_vault.get(secret_vault.media_key_name("stability")) == "sk-old-plaintext"


# ── the inventory and the API ──────────────────────────────────────────────

def test_the_inventory_never_returns_a_value(sandbox, monkeypatch):
    """What the panel shows is *where* a key is. Never the key."""
    from ops_dashboard import credentials

    secret_vault.unlock("a passphrase")
    secret_vault.set("media.stability.api_key", "sk-secret-value")
    blob = json.dumps(credentials.inventory())
    assert "sk-secret-value" not in blob


def test_the_api_never_hands_a_key_back(sandbox):
    """Putting a key in and reading the inventory back: no value comes out."""
    from ops_dashboard import credentials

    secret_vault.unlock("a passphrase")
    credentials.set_secret("media.stability.api_key", "sk-secret-value")
    blob = json.dumps(credentials.inventory())
    assert "sk-secret-value" not in blob
    assert secret_vault.get("media.stability.api_key") == "sk-secret-value"


# ── the API ────────────────────────────────────────────────────────────────

@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A real server on a real port: the routes are the contract."""
    import socket
    import threading
    from http.server import ThreadingHTTPServer

    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    db.init_db()

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        with db.db() as conn:
            conn.execute("DELETE FROM media_providers")


def _get(base: str, path: str):
    import urllib.request

    with urllib.request.urlopen(base + path, timeout=20) as r:
        return r.status, json.loads(r.read())


def _post(base: str, path: str, payload: dict):
    import urllib.request

    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read())


def test_the_api_lists_the_credentials_without_their_values(server):
    """GET /api/credentials: the inventory, and not one secret in it."""
    _post(server, "/api/credentials/media.stability.api_key", {"value": "sk-secret-value"})
    status, data = _get(server, "/api/credentials")
    assert status == 200
    assert data["ok"] is True
    assert "sk-secret-value" not in json.dumps(data)
    assert data["total"] >= 1


def test_the_api_stores_and_removes_a_key(server):
    """POST and DELETE on a credential: the key goes in and comes out."""
    status, data = _post(server, "/api/credentials/media.stability.api_key",
                         {"value": "sk-secret-value"})
    assert status == 200
    assert secret_vault.get("media.stability.api_key") == "sk-secret-value"

    import urllib.request

    req = urllib.request.Request(
        server + "/api/credentials/media.stability.api_key", method="DELETE")
    with urllib.request.urlopen(req, timeout=20) as r:
        assert r.status == 200
    assert secret_vault.get("media.stability.api_key") is None


def test_the_api_refuses_a_name_that_is_not_a_credential(server):
    import urllib.error

    try:
        _post(server, "/api/credentials/not-a-credential", {"value": "x"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("the API should not store a key under an unknown name")
