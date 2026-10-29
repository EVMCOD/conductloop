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


@pytest.mark.posix_perms
def test_file_is_owner_only(store, tmp_path):
    store.set_secret("sekret", "value")
    f = tmp_path / ".sekret.secret"
    assert f.exists()
    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode & 0o077 == 0  # no group/other access
