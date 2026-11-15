"""Guards secrets_store on the file-fallback path (keychain forced off), which
is what CI/Linux and the Fernet-key store use."""
from __future__ import annotations

import stat

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path))
    from ops_dashboard import secrets_store
    return secrets_store


def test_set_get_roundtrip(store):
    assert store.get_secret("token") is None
    store.set_secret("token", "abc123")
    assert store.get_secret("token") == "abc123"


def test_get_or_create_is_stable(store):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return "generated-value"

    v1 = store.get_or_create_secret("k", factory)
    v2 = store.get_or_create_secret("k", factory)
    assert v1 == v2 == "generated-value"
    assert calls["n"] == 1  # factory ran once, then read from store


def test_a_name_with_a_colon_still_round_trips(store, tmp_path):
    """Windows forbids a colon in a file name: the file fallback must cope."""
    store.set_secret("site:credentials:1", "value")
    assert store.get_secret("site:credentials:1") == "value"
    assert (tmp_path / ".site-credentials-1.secret").exists()


def test_delete_removes_the_secret_from_the_file(store, tmp_path):
    store.set_secret("gone:1", "value")
    assert store.delete_secret("gone:1") is True
    assert store.get_secret("gone:1") is None
    assert not (tmp_path / ".gone-1.secret").exists()


@pytest.mark.posix_perms
def test_file_is_owner_only(store, tmp_path):
    store.set_secret("sekret", "value")
    f = tmp_path / ".sekret.secret"
    assert f.exists()
    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode & 0o077 == 0  # no group/other access
